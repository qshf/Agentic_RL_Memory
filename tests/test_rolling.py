"""Rolling trigger, assistant cut boundaries, and binary-search selection."""
from __future__ import annotations

from conftest import make_row

from rolling_summary.config import BudgetConfig
from rolling_summary.history import HistoryMessage, build_message_stream, chronological_sessions
from rolling_summary.rolling import RollingSummaryEngine


class RecordingSummarizer:
    def __init__(self, words: int = 10) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.words = words

    def __call__(self, memory, history, reason):
        self.calls.append((memory, history, reason))
        return " ".join(f"summary{len(self.calls)}word{index}" for index in range(self.words))


def make_message(ordinal: int, tokens: int, role: str = "assistant") -> HistoryMessage:
    return HistoryMessage(
        unit_ordinal=ordinal,
        session_index=0,
        session_id="s0",
        session_date="2023/05/20 (Sat) 02:21",
        role=role,
        content=" ".join(f"m{ordinal}w{i}" for i in range(tokens)),
    )


def build_engine(budgets, summarizer, tokenizer):
    return RollingSummaryEngine(summarize=summarizer, tokenizer=tokenizer, budgets=budgets)


def test_user_overflow_does_not_compress_until_assistant_arrives(budgets, tokenizer):
    summarizer = RecordingSummarizer()
    engine = build_engine(budgets, summarizer, tokenizer)
    for ordinal in range(5):
        engine.ingest(make_message(ordinal, 40, "user" if ordinal == 4 else "assistant"))
    assert summarizer.calls == []
    engine.ingest(make_message(5, 40, "assistant"))
    assert len(summarizer.calls) == 1


def test_compression_prefix_ends_after_assistant_and_tail_is_contiguous(budgets, tokenizer):
    summarizer = RecordingSummarizer()
    engine = build_engine(budgets, summarizer, tokenizer)
    for ordinal in range(12):
        engine.ingest(make_message(ordinal, 20, "user" if ordinal % 2 == 0 else "assistant"))
    assert len(summarizer.calls) == 1
    _memory, history, reason = summarizer.calls[0]
    assert reason == "rolling_compression"
    assert history.index("m0w0") < history.index("m1w0")
    # 触发发生在 ordinal 9（assistant）累计超线：驱逐前缀直到累计达到
    # compress_prefix(80)，即 ordinal 0..3；后续 10、11 继续 ingest，tail 连续为 [4..11]。
    assert engine.state.tail_ordinals == list(range(4, 12))
    assert engine.compressions[0].cut_index == 4


def test_repeated_compression_feeds_previous_summary_back(budgets, tokenizer):
    summarizer = RecordingSummarizer()
    engine = build_engine(budgets, summarizer, tokenizer)
    for ordinal in range(80):
        engine.ingest(make_message(ordinal, 20, "user" if ordinal % 2 == 0 else "assistant"))
    engine.finalize()
    assert len(summarizer.calls) >= 3
    for index, (memory, _history, _reason) in enumerate(summarizer.calls[1:], start=1):
        assert memory.startswith(f"summary{index}word0")
    assert all(c.candidate_tokenize_calls <= 8 for c in engine.compressions)


def test_finalize_does_not_compress_below_trigger(budgets, tokenizer):
    summarizer = RecordingSummarizer()
    engine = build_engine(budgets, summarizer, tokenizer)
    for ordinal in range(4):
        engine.ingest(make_message(ordinal, 20, "user" if ordinal % 2 == 0 else "assistant"))
    assert summarizer.calls == []
    state = engine.finalize()
    # 压缩只由 rolling_trigger 触发，finalize 不做兜底压缩
    assert summarizer.calls == []
    assert state.tail_ordinals == [0, 1, 2, 3]


def test_terminal_user_is_kept_and_marked():
    budgets = BudgetConfig(rolling_trigger_tokens=200, summary_budget_tokens=20, compress_prefix_tokens=80)
    from tests.conftest import FakeClient
    from rolling_summary.client import TokenCounter
    engine = RollingSummaryEngine(summarize=RecordingSummarizer(), tokenizer=TokenCounter(FakeClient()), budgets=budgets)
    engine.ingest(make_message(0, 5, "user"))
    state = engine.finalize()
    assert state.terminal_issue == "terminal_unpaired_user"
    assert state.tail_ordinals == [0]


def test_stream_end_to_end_uses_message_boundaries(tokenizer):
    budgets = BudgetConfig(rolling_trigger_tokens=400, summary_budget_tokens=20, compress_prefix_tokens=100)
    stream = build_message_stream(chronological_sessions(make_row(session_count=12, messages_per_session=6, words_per_message=9)))
    engine = build_engine(budgets, RecordingSummarizer(), tokenizer)
    for message in stream.messages:
        engine.ingest(message)
    state = engine.finalize()
    # 压缩后状态 = summary + 保留末尾（不设上限），应低于触发线 + summary 预算
    assert state.history_tokens <= budgets.rolling_trigger_tokens + budgets.summary_budget_tokens


def test_default_budgets_match_frozen_protocol():
    budgets = BudgetConfig()
    assert budgets.rolling_trigger_tokens == 80 * 1024
    assert budgets.summary_budget_tokens == 8_192
    assert budgets.compress_prefix_tokens == 80 * 1024
