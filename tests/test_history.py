"""Continuous stream normalization and provenance checks."""
from __future__ import annotations

from conftest import FakeClient, make_row

from rolling_summary.history import (
    build_message_stream,
    chronological_sessions,
    parse_session_date,
    render_session,
)


def test_sessions_are_ordered_oldest_first_regardless_of_file_order():
    row = make_row(session_count=3, dates=[
        "2023/05/22 (Mon) 09:00", "2023/05/20 (Sat) 02:21", "2023/05/21 (Sun) 23:59"
    ])
    sessions = chronological_sessions(row)
    assert [session.date for session in sessions] == [
        "2023/05/20 (Sat) 02:21", "2023/05/21 (Sun) 23:59", "2023/05/22 (Mon) 09:00"
    ]
    assert [session.original_index for session in sessions] == [1, 2, 0]


def test_equal_dates_keep_original_file_order():
    same = "2023/05/20 (Sat) 02:21"
    assert [s.original_index for s in chronological_sessions(make_row(session_count=3, dates=[same] * 3))] == [0, 1, 2]


def test_parse_session_date_ignores_weekday():
    assert parse_session_date("2023/05/20 (Sat) 02:21").isoformat() == "2023-05-20T02:21:00"


def test_same_role_messages_merge_with_source_range():
    row = make_row(session_count=1, messages_per_session=4)
    row["haystack_sessions"][0][1]["role"] = "user"
    stream = build_message_stream(chronological_sessions(row))
    assert [(m.role, m.first_message_index, m.last_message_index, m.source_message_indices) for m in stream.messages] == [
        ("user", 0, 2, (0, 1, 2)), ("assistant", 3, 3, (3,))
    ]
    assert "\n\ns0m1w0" in stream.messages[0].content


def test_sessions_are_one_continuous_stream_with_rendered_headers():
    stream = build_message_stream(chronological_sessions(make_row(session_count=2, messages_per_session=2)))
    assert len(stream.messages) == 4
    assert stream.legal_cuts == (0, 2, 4)
    assert stream.text.count("## Session") == 2
    assert stream.text.index("s0m1w0") < stream.text.index("s1m0w0")


def test_render_session_preserves_message_order_and_ignores_gold_fields():
    session = chronological_sessions(make_row(session_count=1, messages_per_session=4))[0]
    rendered = render_session(session)
    assert rendered.startswith("## Session 1 — 2023/05/20 (Sat) 02:21")
    assert [rendered.index(f"s0m{index}w0") for index in range(4)] == sorted(
        rendered.index(f"s0m{index}w0") for index in range(4)
    )
    assert "has_answer" not in rendered


def test_normalization_does_not_need_token_budget_or_client():
    row = make_row(session_count=1, messages_per_session=1, words_per_message=200)
    stream = build_message_stream(chronological_sessions(row), FakeClient(), 60)
    assert len(stream.messages) == 1
    assert not stream.messages[0].is_split_degraded
