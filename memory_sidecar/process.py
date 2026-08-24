"""Memory Sidecar 单样本执行流程。

Manager 按时间顺序处理历史 chunk，并为每个 chunk 输出一个结构化事件；规则化的
``MemoryState`` 在本地应用该事件，随后 Answer Model 使用最终状态和只读近期 tail 回答。
"""
from __future__ import annotations

import json
import time
from argparse import Namespace
from pathlib import Path
from typing import Any

from utils.client import ApiError, QwenClient
from utils.config import ModelConfig
from utils.history import build_message_stream, chronological_sessions
from utils.local_tokenizer import LocalQwenTokenizer
from utils.store import STATUS_COMPLETED, STATUS_FAILED, STATUS_NOT_RUNNABLE, TrajectoryStore
from utils.trajectory import record_call
from .budget import chunks, history_token_count, manager_prompt
from .data import load_baseline_tail, record_sidecar_context
from .protocol import MemoryState, answer_messages, chunk_text, parse_event, state_sha256


def _record_manager_event(
    store: TrajectoryStore,
    sample_id: int,
    ordinal: int,
    current_chunk: tuple[Any, ...],
    before: str,
    raw_response: str | None,
    parse_status: str,
    parsed: dict[str, Any] | None,
    route_status: str,
    route: dict[str, Any],
    error: str | None,
    state: MemoryState,
) -> None:
    # event 行记录 manager 的输入与原始输出；state 行记录规则路由后的确定性状态。
    # 两者同时保留，解析或路由失败时无需重跑即可审计。
    source_units = [message.unit_ordinal for message in current_chunk]
    store.record_sidecar_event(
        sample_id,
        event_ordinal=ordinal,
        chunk_start_ordinal=source_units[0],
        chunk_end_ordinal=source_units[-1],
        source_unit_ordinals=source_units,
        input_text=chunk_text(current_chunk),
        memory_before_json=before,
        raw_response=raw_response,
        parse_status=parse_status,
        parsed_event=parsed,
        route_status=route_status,
        route_result=route,
        error=error,
    )
    store.record_sidecar_state(
        sample_id,
        event_ordinal=ordinal,
        state_json=state.to_json(),
        state_sha256=state_sha256(state),
        active_record_count=len(state.active_records),
    )
    # 数据库保存完整记忆库；上面的 manager prompt 仍可只携带窗口内的工作视图。
    store.sync_sidecar_memory(sample_id, state.records)


def _run_manager_chunk(
    client: QwenClient,
    store: TrajectoryStore,
    sample_id: int,
    ordinal: int,
    state: MemoryState,
    current_chunk: tuple[Any, ...],
    tokenizer: LocalQwenTokenizer,
    args: Namespace,
) -> tuple[int, int]:
    # ``before`` 与原始响应一起持久化，方便审计 manager 没有使用本次事件产生的新状态。
    source_units = [message.unit_ordinal for message in current_chunk]
    before, prompt, prompt_tokens = manager_prompt(
        state,
        current_chunk,
        source_units,
        tokenizer,
        args.manager_context_budget_tokens,
        args.active_memory_budget_tokens,
        args.update_ledger_budget_tokens,
    )
    # 模型遗漏或写坏 source 时，使用当前 chunk 作为保守兜底，不能中断 provenance。
    source_default = {
        "session_id": current_chunk[0].session_id,
        "message_indices": source_units,
    }
    try:
        result = client.chat(prompt, max_tokens=args.manager_max_tokens)
        record_call(store, sample_id, ordinal, "sidecar_manager", result)
        raw_response = result.content
        input_tokens, output_tokens = result.input_tokens, result.output_tokens
        try:
            event = parse_event(raw_response, default_source=source_default)
            route = state.apply(event, event_id=f"event-{ordinal}", source_unit_ordinals=source_units)
            parse_status = "ok"
            parsed = event.as_dict()
            route_status = route["route_status"]
            error = None
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            # 解析拒绝不改变 ``state``，但仍是一次正常记录的 manager 尝试，而非样本致命失败。
            parse_status = "error"
            parsed = None
            route_status = "rejected_parse"
            route = {"route_status": route_status, "changed": False}
            error = f"{type(exc).__name__}: {exc}"
        route["manager_prompt_tokens"] = prompt_tokens
    except ApiError as exc:
        route = {"route_status": "manager_error", "changed": False, "manager_prompt_tokens": prompt_tokens}
        _record_manager_event(
            store, sample_id, ordinal, current_chunk, before, None, "not_run", None,
            "manager_error", route, str(exc), state,
        )
        record_call(store, sample_id, ordinal, "sidecar_manager", None, str(exc))
        raise

    _record_manager_event(
        store, sample_id, ordinal, current_chunk, before, raw_response,
        parse_status, parsed, route_status, route, error, state,
    )
    return input_tokens, output_tokens


def _model_config(args: Namespace) -> ModelConfig:
    return ModelConfig(
        base_url=args.base_url.rstrip("/"),
        model=args.model,
        temperature=0.0,
        enable_thinking=False,
        summary_max_tokens=args.manager_max_tokens,
        answer_max_tokens=args.answer_max_tokens,
        timeout_seconds=args.timeout_seconds,
    )


def process_one(
    row: dict[str, Any],
    *,
    args: Namespace,
    config: dict[str, Any],
    db_path: Path,
    api_key: str,
) -> dict[str, Any]:
    """Run manager chunks, deterministic routing, and final answer for one sample."""
    model = _model_config(args)
    client = QwenClient(model, api_key)
    tokenizer_path = getattr(args, "tokenizer_path", None)
    tokenizer = LocalQwenTokenizer(tokenizer_path=str(tokenizer_path) if tokenizer_path else None)
    store = TrajectoryStore(db_path)
    question_id = row["question_id"]
    sample_id = store.start_sample(
        args.run_id,
        question_id,
        dataset_index=int(row["dataset_index"]) if row.get("dataset_index") else None,
        question_type=row.get("question_type"),
        config_fingerprint=config["config_fingerprint"],
        code_version=config["code_version"],
    )
    started = time.monotonic()
    call_ordinal = 0
    manager_events = 0
    input_tokens = 0
    output_tokens = 0
    state = MemoryState()
    try:
        # 这是实验输入依赖，不是对 Rolling Summary 的 Python 代码依赖：只读其 SQLite 中
        # 已完成的 V1 行。
        tail = load_baseline_tail(
            args.baseline_db,
            args.baseline_run_id,
            question_id,
            args.recent_tail_budget_tokens,
            tokenizer,
        )
        # 只把 tail 的 provenance 与 hash 写入 sidecar_context，不复制正文（正文仍在基线库，
        # 可按 tail_source_ordinals 复现），用于审计与校验基线未变。
        record_sidecar_context(store, sample_id, tail)
        # 按时间排序并展平为连续 logical stream，作为 manager 逐 chunk 抽取记忆的唯一输入源。
        stream = build_message_stream(chronological_sessions(row))
        all_chunks = chunks(stream.messages, args.chunk_budget_tokens, tokenizer)
        full_history_tokens = history_token_count(stream, tokenizer)

        # 单样本内的 manager 调用必须串行，因为每个事件都可能改变下一 chunk 看到的状态。
        for chunk_index, current_chunk in enumerate(all_chunks, start=1):
            call_ordinal += 1
            manager_input, manager_output = _run_manager_chunk(
                client, store, sample_id, call_ordinal, state, current_chunk, tokenizer, args,
            )
            input_tokens += manager_input
            output_tokens += manager_output
            manager_events += 1
            if args.debug_max_chunks and chunk_index >= args.debug_max_chunks:
                break

        # Answer 只从 sample 级长期记忆表恢复，不直接读取进程内 state。
        # 这样 prompt 窗口如何裁剪、worker 是否重启，都不会改变最终记忆事实源。
        answer_state = MemoryState(records=store.load_sidecar_memory(sample_id))
        answer_memory = answer_state.render_for_answer()
        answer_prompt = answer_messages(
            answer_memory,
            tail["raw_tail"],
            row["question_date"],
            row["question"],
        )
        # 所有分块和预算都使用本地 tokenizer。这里唯一的服务端计数仅用于在发送较贵的
        # completion 前，核验服务端 chat template 下的实际上下文窗口是否可用。
        answer_input_tokens = client.tokenize_messages(answer_prompt)
        if answer_input_tokens >= client.max_model_len:
            reason = f"answer prompt {answer_input_tokens} >= max_model_len {client.max_model_len}"
            call_ordinal += 1
            store.record_call(
                sample_id,
                call_ordinal=call_ordinal,
                attempt=0,
                kind="answer",
                status="not_runnable",
                request_params={"model": args.model, "max_tokens": args.answer_max_tokens},
                prompt_tokens_estimated=answer_input_tokens,
                error=reason,
            )
            store.finish_sample(
                sample_id,
                STATUS_NOT_RUNNABLE,
                full_history_tokens=full_history_tokens,
                summary_tokens=tokenizer.count(answer_memory),
                raw_tail_tokens=tail["raw_tail_tokens"],
                answer_input_tokens=answer_input_tokens,
                total_input_tokens=input_tokens,
                total_output_tokens=output_tokens,
                call_count=call_ordinal,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=reason,
            )
            return {"question_id": question_id, "status": STATUS_NOT_RUNNABLE, "error": reason}

        call_ordinal += 1
        try:
            answer_result = client.chat(answer_prompt, max_tokens=args.answer_max_tokens)
        except ApiError as exc:
            record_call(store, sample_id, call_ordinal, "answer", None, str(exc))
            store.finish_sample(sample_id, STATUS_FAILED, error=str(exc), call_count=call_ordinal)
            return {"question_id": question_id, "status": STATUS_FAILED, "error": str(exc)}
        input_tokens += answer_result.input_tokens
        output_tokens += answer_result.output_tokens
        record_call(store, sample_id, call_ordinal, "answer", answer_result)
        store.finish_sample(
            sample_id,
            STATUS_COMPLETED,
            full_history_tokens=full_history_tokens,
            summary_tokens=tokenizer.count(answer_memory),
            raw_tail_tokens=tail["raw_tail_tokens"],
            answer_input_tokens=answer_result.input_tokens,
            answer_output_tokens=answer_result.output_tokens,
            total_input_tokens=input_tokens,
            total_output_tokens=output_tokens,
            call_count=call_ordinal,
            latency_ms=int((time.monotonic() - started) * 1000),
            hypothesis=answer_result.content.strip(),
        )
        return {
            "question_id": question_id,
            "status": STATUS_COMPLETED,
            "manager_events": manager_events,
            "answer_input_tokens": answer_result.input_tokens,
            "hypothesis": answer_result.content.strip(),
        }
    except Exception as exc:  # noqa: BLE001
        store.finish_sample(sample_id, STATUS_FAILED, error=f"{type(exc).__name__}: {exc}")
        return {"question_id": question_id, "status": STATUS_FAILED, "error": repr(exc)}
    finally:
        store.close()
