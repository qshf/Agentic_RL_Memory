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
    over_budget: bool
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

    def _emit(self, event: str, detail: dict | None = None) -> None:
        if self.on_state is not None:
            self.on_state(event, self.state, detail or {})

    def _header_tokens(self, message: HistoryMessage) -> int:
        # 一条消息前的 session header 行（与 render_history 的格式一致）。
        header = f"## Session {message.session_index + 1} — {message.session_date}"
        return self.tokenizer.count(header)

    def _message_tokens(self, message: HistoryMessage) -> int:
        # 单条消息的增量 token：内容 +（若开启新 session）header。O(1)。
        tokens = self.tokenizer.count(message.rendered)
        if len(self.state.tail) == 1 or self.state.tail[-2].session_index != message.session_index:
            tokens += self._header_tokens(message)
        return tokens

    def _tail_tokens(self, tail: Sequence[HistoryMessage]) -> int:
        text = render_history(tail)
        return self.tokenizer.count(text) if text else 0

    def _legal_cuts(self) -> list[int]:
        # A cut is a prefix length. It can only be zero or after assistant.
        return [0, *(index for index, message in enumerate(self.state.tail, 1) if message.role == "assistant")]

    def _find_cut(self) -> tuple[int, int]:
        """Binary-search the earliest legal cut whose suffix fits the raw budget."""
        cuts = self._legal_cuts()
        if len(cuts) == 1:
            return 0, 0

        calls = 0

        def suffix_tokens(cut: int) -> int:
            nonlocal calls
            calls += 1
            return self._tail_tokens(self.state.tail[cut:])

        # Token count of a suffix decreases as cut moves right. Find the first
        # legal cut satisfying the raw-tail budget, with a final exact check.
        if suffix_tokens(cuts[-1]) > self.budgets.raw_tail_budget_tokens:
            return cuts[-1], calls
        lo, hi = 0, len(cuts) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if suffix_tokens(cuts[mid]) <= self.budgets.raw_tail_budget_tokens:
                hi = mid
            else:
                lo = mid + 1
        cut = cuts[lo]
        suffix_tokens(cut)
        return cut, calls

    def _compress(self, reason: str) -> None:
        # 压缩流程：1) 二分找出最早合法切割点；2) 前缀被驱逐并交给 summarize 总结；
        # 3) 用「新 summary + 保留 tail」重建状态并记录压缩档案。
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
        # 压缩后仍超预算（summary 或整体上下文超限）则标记 terminal_issue。
        over_budget = summary_tokens > self.budgets.summary_budget_tokens or history_tokens > self.budgets.final_context_budget_tokens
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
            over_budget=over_budget,
            cut_index=cut,
            candidate_tokenize_calls=tokenize_calls,
        )
        self.compressions.append(compression)
        self._emit(reason, {"compression": compression})
        if over_budget:
            self.state.terminal_issue = "memory_budget_exceeded"

    def ingest(self, message: HistoryMessage) -> None:
        # 增量维护：只对新增这条做本地 count（含新 session 的 header），O(1)；
        # 不再每次全量重算 tail，避免 O(n²) 渲染/编码。压缩只在 assistant
        # 消息到达后触发，保证切割点落在完整的 assistant 回合之后。
        self.state.tail.append(message)
        self.state.tail_tokens += self._message_tokens(message)
        self.state.history_tokens = self.state.summary_tokens + self.state.tail_tokens
        self._emit("ingest", {"unit_ordinal": message.unit_ordinal})
        # A user message may make the stream temporarily too large. Compression
        # is only legal once its corresponding assistant response has arrived.
        if message.role == "assistant" and self.state.history_tokens > self.budgets.rolling_trigger_tokens:
            self._compress("rolling_compression")

    def finalize(self) -> RollingState:
        # 收尾：若整体仍超最终上下文预算则强制压缩；若末尾是未配对的 user
        # 消息则记录 terminal_issue（该回合无法被安全切割）。
        if self.state.history_tokens > self.budgets.final_context_budget_tokens:
            self._compress("final_flush")
        if self.state.tail and self.state.tail[-1].role == "user":
            self.state.terminal_issue = self.state.terminal_issue or "terminal_unpaired_user"
        self._emit("final", {})
        return self.state
