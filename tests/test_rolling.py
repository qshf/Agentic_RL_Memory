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
        first_message_index=ordinal,
        last_message_index=ordinal,
        role=role,
        content=" ".join(f"m{ordinal}w{i}" for i in range(tokens)),
        source_message_indices=(ordinal,),
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
    assert engine.state.tail_ordinals == list(range(10, 12))
    assert engine.compressions[0].cut_index == 10


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


def test_final_flush_runs_when_history_exceeds_final_budget(budgets, tokenizer):
    summarizer = RecordingSummarizer()
    engine = build_engine(budgets, summarizer, tokenizer)
    for ordinal in range(4):
        engine.ingest(make_message(ordinal, 20, "user" if ordinal % 2 == 0 else "assistant"))
    assert summarizer.calls == []
    state = engine.finalize()
    assert summarizer.calls[-1][2] == "final_flush"
    assert state.tail_tokens <= budgets.raw_tail_budget_tokens


def test_over_budget_summary_is_recorded(budgets, tokenizer):
    summarizer = RecordingSummarizer(words=budgets.summary_budget_tokens + 5)
    engine = build_engine(budgets, summarizer, tokenizer)
    for ordinal in range(4):
        engine.ingest(make_message(ordinal, 20, "user" if ordinal % 2 == 0 else "assistant"))
    engine.finalize()
    assert engine.compressions[0].over_budget
    assert engine.state.terminal_issue == "memory_budget_exceeded"


def test_terminal_user_is_kept_and_marked():
    budgets = BudgetConfig(rolling_trigger_tokens=200, summary_budget_tokens=20, raw_tail_budget_tokens=40, final_context_budget_tokens=60)
    from tests.conftest import FakeClient
    from rolling_summary.client import TokenCounter
    engine = RollingSummaryEngine(summarize=RecordingSummarizer(), tokenizer=TokenCounter(FakeClient()), budgets=budgets)
    engine.ingest(make_message(0, 5, "user"))
    state = engine.finalize()
    assert state.terminal_issue == "terminal_unpaired_user"
    assert state.tail_ordinals == [0]


def test_stream_end_to_end_uses_message_boundaries(tokenizer):
    budgets = BudgetConfig(rolling_trigger_tokens=400, summary_budget_tokens=20, raw_tail_budget_tokens=60, final_context_budget_tokens=100)
    stream = build_message_stream(chronological_sessions(make_row(session_count=12, messages_per_session=6, words_per_message=9)))
    engine = build_engine(budgets, RecordingSummarizer(), tokenizer)
    for message in stream.messages:
        engine.ingest(message)
    state = engine.finalize()
    assert state.history_tokens <= budgets.final_context_budget_tokens or state.terminal_issue


def test_default_budgets_match_frozen_protocol():
    budgets = BudgetConfig()
    assert budgets.rolling_trigger_tokens == 102_400
    assert budgets.summary_budget_tokens == 8_192
    assert budgets.raw_tail_budget_tokens == 16_384
    assert budgets.final_context_budget_tokens == 24_576
