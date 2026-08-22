"""Query-independent rolling compression over a continuous message stream."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from .config import BudgetConfig, sha256_text
from .history import HistoryMessage, Tokenizer, render_history


@dataclass
class RollingState:
    summary: str = ""
    summary_tokens: int = 0
    tail: list[HistoryMessage] = field(default_factory=list)
    tail_tokens: int = 0
    history_tokens: int = 0
    terminal_issue: str | None = None

    @property
    def tail_ordinals(self) -> list[int]:
        return [message.unit_ordinal for message in self.tail]

    @property
    def summary_sha256(self) -> str:
        return sha256_text(self.summary)

    def render_tail(self) -> str:
        return render_history(self.tail)


@dataclass(frozen=True)
class Compression:
    reason: str
    evicted_ordinals: tuple[int, ...]
    evicted_tokens: int
    previous_summary_sha256: str
    previous_summary_tokens: int
    new_summary_tokens: int
    cut_index: int
    candidate_tokenize_calls: int


SummarizeFn = Callable[[str, str, str], str]
StateHook = Callable[[str, RollingState, dict], None]


class RollingSummaryEngine:
    """Maintain ``summary + continuous raw tail`` without seeing the query."""

    def __init__(
        self,
        *,
        summarize: SummarizeFn,
        tokenizer: Tokenizer,
        budgets: BudgetConfig,
        on_state: StateHook | None = None,
    ) -> None:
        self.summarize = summarize
        self.tokenizer = tokenizer
        self.budgets = budgets
        self.on_state = on_state
        self.state = RollingState()
        self.compressions: list[Compression] = []
        self._last_session_index: int | None = None

    def _emit(self, event: str, detail: dict | None = None) -> None:
        if self.on_state is not None:
            self.on_state(event, self.state, detail or {})

    def _header_tokens(self, message: HistoryMessage) -> int:
        # 一条消息前的 session header 行（与 render_history 的格式一致）。
        header = f"## Session {message.session_index + 1} — {message.session_date}"
        return self.tokenizer.count(header)

    def _message_token_delta(self, message: HistoryMessage) -> int:
        # 仅维护 rolling trigger 所需的总量，不寻找压缩切点。单条消息的
        # 增量 token 为内容 +（若开启新 session）header，复杂度 O(1)。
        tokens = self.tokenizer.count(message.rendered)
        if len(self.state.tail) == 1 or self.state.tail[-2].session_index != message.session_index:
            tokens += self._header_tokens(message)
        return tokens

    def _tail_tokens(self, tail: Sequence[HistoryMessage]) -> int:
        text = render_history(tail)
        return self.tokenizer.count(text) if text else 0

    def _legal_cuts(self) -> list[int]:
        """Return prefix lengths that end at the stream start or an assistant."""
        return [
            0,
            *(index for index, message in enumerate(self.state.tail, 1) if message.role == "assistant"),
        ]

    def _find_cut(self) -> tuple[int, int]:
        """Find the earliest legal assistant cut whose prefix reaches budget."""
        tail = self.state.tail
        n = len(tail)
        if n <= 1:
            return 0, 0
        budget = self.budgets.compress_prefix_tokens
        legal_cuts = self._legal_cuts()
        calls = 0

        def prefix_tokens(cut: int) -> int:
            nonlocal calls
            calls += 1
            return self._tail_tokens(tail[:cut])

        lo, hi = 1, len(legal_cuts) - 1
        found: int | None = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if prefix_tokens(legal_cuts[mid]) >= budget:
                found = legal_cuts[mid]
                hi = mid - 1
            else:
                lo = mid + 1
        if found is not None:
            return found, calls

        earlier = [cut for cut in legal_cuts[1:] if cut < n]
        return (earlier[-1] if earlier else 0), calls

    def _compress(self, reason: str) -> None:
        # 压缩流程：1) 找切点（保留最后一个完整回合）；2) 前缀被驱逐并交给
        # summarize 总结；3) 用「新 summary + 保留 tail」重建状态并记录压缩档案。
        cut, tokenize_calls = self._find_cut()
        if cut == 0:
            self.state.terminal_issue = "no_legal_assistant_cut"
            return
        evicted = self.state.tail[:cut]
        kept = self.state.tail[cut:]
        previous_summary = self.state.summary
        previous_tokens = self.state.summary_tokens
        summary = self.summarize(previous_summary, render_history(evicted), reason).strip()
        summary_tokens = self.tokenizer.count(summary) if summary else 0
        tail_tokens = self._tail_tokens(kept)
        history_tokens = summary_tokens + tail_tokens
        self.state = RollingState(
            summary=summary,
            summary_tokens=summary_tokens,
            tail=list(kept),
            tail_tokens=tail_tokens,
            history_tokens=history_tokens,
        )
        compression = Compression(
            reason=reason,
            evicted_ordinals=tuple(message.unit_ordinal for message in evicted),
            evicted_tokens=self._tail_tokens(evicted),
            previous_summary_sha256=sha256_text(previous_summary),
            previous_summary_tokens=previous_tokens,
            new_summary_tokens=summary_tokens,
            cut_index=cut,
            candidate_tokenize_calls=tokenize_calls,
        )
        self.compressions.append(compression)
        self._emit(reason, {"compression": compression})

    def ingest(self, message: HistoryMessage) -> None:
        # 先增量维护 trigger 计数；这里不是切点搜索，也不扫描已有 tail。
        # 真正触发后，_compress() 再仅在合法 assistant 边界上做二分查找。
        self.state.tail.append(message)
        self.state.tail_tokens += self._message_token_delta(message)
        self.state.history_tokens = self.state.summary_tokens + self.state.tail_tokens
        raw_text = message.rendered
        if self._last_session_index != message.session_index:
            raw_text = f"{message.session_header}\n{raw_text}"
        self._last_session_index = message.session_index
        self._emit("ingest", {"unit_ordinal": message.unit_ordinal, "raw_text": raw_text})
        # A user message may make the stream temporarily too large. Compression
        # is only legal once its corresponding assistant response has arrived.
        if message.role == "assistant" and self.state.history_tokens > self.budgets.rolling_trigger_tokens:
            self._compress("rolling_compression")

    def finalize(self) -> RollingState:
        # 收尾：压缩只由 rolling_trigger 触发，这里不再做任何兜底压缩。
        # 若末尾是未配对的 user 消息则记录 terminal_issue（该回合无法被安全切割）。
        if self.state.tail and self.state.tail[-1].role == "user":
            self.state.terminal_issue = self.state.terminal_issue or "terminal_unpaired_user"
        self._emit("final", {})
        return self.state
