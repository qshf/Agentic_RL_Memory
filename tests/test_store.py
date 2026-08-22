"""Plan check 6: trajectory schema, idempotent keys, append-only, export."""
from __future__ import annotations

import json
import sqlite3

import pytest
from test_rolling import make_message

from rolling_summary.rolling import RollingState
from rolling_summary.store import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_NOT_RUNNABLE,
    TrajectoryStore,
)

CONFIG = {
    "method": "rolling_summary",
    "prompt_version": "rolling-summary-v1",
    "config_fingerprint": "fp-a",
    "model": {"model": "fake-model"},
    "manifest": {"path": "m.csv", "sha256": "abc"},
    "service": {"max_model_len": 262144},
}


@pytest.fixture
def store(tmp_path) -> TrajectoryStore:
    store = TrajectoryStore(tmp_path / "trajectory.sqlite3")
    store.start_run("run-1", CONFIG)
    yield store
    store.close()


def new_sample(store: TrajectoryStore, question_id: str = "q1") -> int:
    return store.start_sample(
        "run-1",
        question_id,
        dataset_index=3,
        question_type="multi-session",
        config_fingerprint="fp-a",
        code_version="deadbeef",
    )


def test_required_tables_exist(store):
    names = {
        row[0]
        for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert names >= {"runs", "samples", "calls", "states"}
    assert "units" not in names
    assert "messages" not in names


def test_foreign_keys_are_enforced(store):
    assert store.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("INSERT INTO calls (sample_id, call_ordinal, attempt, kind, status, created_at) "
                           "VALUES (999,1,1,'summary','ok',0)")


def test_journal_mode_is_wal(tmp_path):
    store = TrajectoryStore(tmp_path / "t.sqlite3")
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    store.close()


def test_repeated_samples_get_new_attempts_rather_than_overwriting(store):
    first = new_sample(store)
    store.finish_sample(first, STATUS_FAILED, error="boom")
    second = new_sample(store)

    rows = store.conn.execute(
        "SELECT attempt, status FROM samples WHERE question_id='q1' ORDER BY attempt"
    ).fetchall()
    assert [(row["attempt"], row["status"]) for row in rows] == [(1, STATUS_FAILED), (2, "running")]
    assert first != second


def test_call_ordinal_and_attempt_form_the_idempotency_key(store):
    sample_id = new_sample(store)
    kwargs = dict(kind="summary", status="ok", request_params={"model": "m"})
    store.record_call(sample_id, call_ordinal=1, attempt=1, **kwargs)
    store.record_call(sample_id, call_ordinal=1, attempt=2, **kwargs)
    with pytest.raises(sqlite3.IntegrityError):
        store.record_call(sample_id, call_ordinal=1, attempt=2, **kwargs)


def test_states_keep_summary_snapshots_without_overwriting(store):
    sample_id = new_sample(store)
    before = RollingState(summary="", summary_tokens=0, tail=[make_message(0, 5), make_message(1, 5)])
    first = store.record_state(
        sample_id, step_ordinal=1, parent_step_id=None, event="ingest", state=before, summary_text=None
    )
    after = RollingState(summary="memory v1", summary_tokens=2, tail=[make_message(1, 5)])
    second = store.record_state(
        sample_id,
        step_ordinal=2,
        parent_step_id=first,
        event="rolling_compression",
        state=after,
        summary_text=after.summary,
        raw_text=None,
        detail={"evicted_ordinals": [0]},
    )

    rows = store.conn.execute(
        "SELECT * FROM states WHERE sample_id=? ORDER BY step_ordinal", (sample_id,)
    ).fetchall()
    assert len(rows) == 2, "the pre-compression state must survive"
    assert json.loads(rows[1]["detail"])["evicted_ordinals"] == [0]
    assert rows[0]["summary_text"] is None and rows[1]["summary_text"] == "memory v1"
    assert rows[0]["raw_text"] is None
    assert rows[1]["raw_text"] is None
    assert second != first


def test_completed_samples_are_skipped_only_on_a_matching_fingerprint(store):
    done = new_sample(store, "q1")
    store.finish_sample(done, STATUS_COMPLETED, hypothesis="answer")
    skipped = new_sample(store, "q2")
    store.finish_sample(skipped, STATUS_NOT_RUNNABLE, error="too long")
    failed = new_sample(store, "q3")
    store.finish_sample(failed, STATUS_FAILED, error="boom")

    assert store.completed_question_ids("run-1", "fp-a") == {"q1", "q2"}
    assert store.completed_question_ids("run-1", "fp-b") == set()


def test_hypothesis_export_matches_the_official_evaluator_shape(store, tmp_path):
    for question_id, hypothesis in [("q1", "first"), ("q2", "second")]:
        sample_id = new_sample(store, question_id)
        store.finish_sample(sample_id, STATUS_COMPLETED, hypothesis=hypothesis)
    unfinished = new_sample(store, "q3")
    store.finish_sample(unfinished, STATUS_FAILED, error="boom")

    output = tmp_path / "hypotheses.jsonl"
    assert store.export_hypotheses("run-1", output) == 2
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert records == [
        {"question_id": "q1", "hypothesis": "first"},
        {"question_id": "q2", "hypothesis": "second"},
    ]


def test_export_takes_the_latest_completed_attempt(store, tmp_path):
    first = new_sample(store, "q1")
    store.finish_sample(first, STATUS_COMPLETED, hypothesis="stale")
    second = new_sample(store, "q1")
    store.finish_sample(second, STATUS_COMPLETED, hypothesis="fresh")

    output = tmp_path / "hypotheses.jsonl"
    assert store.export_hypotheses("run-1", output) == 1
    assert json.loads(output.read_text(encoding="utf-8"))["hypothesis"] == "fresh"


def test_run_statistics_counts_the_latest_attempt_per_question(store):
    first = new_sample(store, "q1")
    store.finish_sample(first, STATUS_FAILED, error="boom", call_count=1)
    retry = new_sample(store, "q1")
    store.finish_sample(
        retry,
        STATUS_COMPLETED,
        hypothesis="ok",
        call_count=3,
        total_input_tokens=100,
        total_output_tokens=20,
        compression_count=2,
        latency_ms=500,
    )

    statistics = store.run_statistics("run-1")
    assert statistics["sample_count"] == 1
    assert statistics["status_counts"] == {STATUS_COMPLETED: 1}
    assert statistics["total_calls"] == 3
    assert statistics["total_compressions"] == 2


def test_finish_sample_rejects_column_names_it_did_not_define(store):
    sample_id = new_sample(store)
    with pytest.raises(ValueError, match="invalid column names"):
        store.finish_sample(sample_id, STATUS_COMPLETED, **{"hypothesis=1; DROP TABLE runs": "x"})
