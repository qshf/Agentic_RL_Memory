"""Memory Sidecar 的 token 预算工具。

本模块所有估算均使用本地 Qwen tokenizer；历史分块、manager prompt 定长和近期 tail
裁剪均不访问模型服务。
"""
from __future__ import annotations

from typing import Any, Sequence

from utils.history import HistoryStream
from utils.local_tokenizer import LocalQwenTokenizer
from utils.tokenizer import Tokenizer, count_messages
from .protocol import MemoryState, chunk_text, manager_messages


def turn_units(messages: Sequence[Any]) -> list[tuple[Any, ...]]:
    """将同一 session 中相邻的 ``user -> assistant`` 作为不可拆分的 turn。"""
    result: list[tuple[Any, ...]] = []
    index = 0
    while index < len(messages):
        first = messages[index]
        second = messages[index + 1] if index + 1 < len(messages) else None
        if (
            second is not None
            and first.role == "user"
            and second.role == "assistant"
            and first.session_index == second.session_index
        ):
            result.append((first, second))
            index += 2
        else:
            # 历史开头 assistant、末尾未配对 user 和异常 role 顺序均作为单条 unit 保留。
            result.append((first,))
            index += 1
    return result


def chunks(messages: tuple[Any, ...], token_budget: int, tokenizer: Tokenizer) -> list[tuple[Any, ...]]:
    """在不超过本地 token 预算的前提下，按完整 turn 累积为 chunk。"""
    if token_budget < 1:
        raise ValueError("chunk token budget must be >= 1")
    result: list[tuple[Any, ...]] = []
    current: list[Any] = []
    for unit in turn_units(messages):
        candidate = tuple(current + list(unit))
        # 超长 turn 仍保持完整；在这里拆分会破坏 Manager 理解问答关系和 source provenance。
        if current and tokenizer.count(chunk_text(candidate)) > token_budget:
            result.append(tuple(current))
            current = list(unit)
        else:
            current.extend(unit)
    if current:
        result.append(tuple(current))
    return result


def history_token_count(stream: HistoryStream, tokenizer: Tokenizer) -> int:
    return tokenizer.count(stream.text) if stream.text else 0


def trim_tail(text: str, budget_tokens: int, tokenizer: Tokenizer) -> tuple[str, int, bool]:
    """使用本地 encode/decode 保留最后 ``budget_tokens`` 个 token。"""
    full_tokens = tokenizer.count(text) if text else 0
    if not text or budget_tokens <= 0 or full_tokens <= budget_tokens:
        return text, full_tokens, False
    token_ids = tokenizer.encode(text)
    return tokenizer.decode(token_ids[-budget_tokens:]), full_tokens, True


def manager_prompt(
    state: MemoryState,
    current_chunk: tuple[Any, ...],
    source_units: list[int],
    tokenizer: Tokenizer,
    budget: int,
    active_budget: int,
    ledger_budget: int,
) -> tuple[str, list[dict[str, str]], int]:
    """渲染受限的 manager 上下文，并返回本地 token 估算值。"""
    # 记录数上限用于降低候选搜索成本；完整渲染后的 prompt 再由本地 tokenizer 计数，
    # 后者才是 manager 预算的实际判定。
    max_records = max(0, active_budget // 80)
    max_updates = max(0, ledger_budget // 160)
    candidates = [
        (max_records, max_updates),
        (max_records // 2, max_updates),
        (max_records // 4, max_updates),
        (max_records // 8, max_updates),
        (max_records // 16, max_updates // 2),
        (0, 0),
    ]
    last: tuple[str, list[dict[str, str]], int] | None = None
    for active_limit, updates_per_key in candidates:
        memory = state.render_for_manager(
            max_active_records=active_limit,
            updates_per_key=updates_per_key,
        )
        prompt = manager_messages(memory, chunk_text(current_chunk), source_units)
        prompt_tokens = count_messages(prompt, tokenizer)
        last = (memory, prompt, prompt_tokens)
        if prompt_tokens <= budget:
            return last
    assert last is not None
    return last


def default_tokenizer() -> LocalQwenTokenizer:
    """构造供 Sidecar runner 及其 worker 副本使用的 tokenizer。"""
    return LocalQwenTokenizer()
