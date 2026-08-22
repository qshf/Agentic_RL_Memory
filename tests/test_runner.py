"""Plan checks 4 and 5: leakage boundary, retries, resume, and JSONL output."""
from __future__ import annotations

import json
import sqlite3

import pytest
import requests
from conftest import FakeClient, make_row

from rolling_summary.client import ApiError, QwenClient
from rolling_summary.config import METHOD_FULL_CONTEXT, METHOD_ROLLING_SUMMARY, ModelConfig
from rolling_summary import prompts
from rolling_summary.runner import process_sample, run
from rolling_summary.store import STATUS_COMPLETED, STATUS_FAILED, STATUS_NOT_RUNNABLE, TrajectoryStore

GOLD_MARKERS = ("SECRETGOLDANSWER", "SECRETQUESTION", "has_answer", "answer_session_ids")


@pytest.fixture
def store(tmp_path) -> TrajectoryStore:
    store = TrajectoryStore(tmp_path / "trajectory.sqlite3")
    store.start_run(
        "run-1",
        {
            "method": METHOD_ROLLING_SUMMARY,
            "prompt_version": "v1",
            "config_fingerprint": "fp-a",
            "model": {"model": "fake-model"},
            "manifest": {"path": "m.csv", "sha256": "abc"},
            "service": {"max_model_len": 100_000},
        },
    )
    yield store
    store.close()


def start(store: TrajectoryStore, question_id: str = "q1") -> int:
    return store.start_sample(
        "run-1",
        question_id,
        dataset_index=0,
        question_type="single-session-user",
        config_fingerprint="fp-a",
        code_version="test",
    )


def run_sample(store, budgets, client=None, **row_kwargs):
    client = client or FakeClient(summary_max_tokens=budgets.summary_budget_tokens)
    sample_id = start(store)
    outcome = process_sample(
        make_row(**row_kwargs),
        client=client,
        budgets=budgets,
        store=store,
        sample_id=sample_id,
        method=row_kwargs.pop("method", METHOD_ROLLING_SUMMARY),
    )
    return client, sample_id, outcome


# ── leakage boundary ─────────────────────────────────────────────────────
def test_summary_requests_never_contain_the_question_or_gold_fields(store, budgets):
    client, _sample_id, _outcome = run_sample(
        store, budgets, session_count=12, messages_per_session=6, words_per_message=9
    )

    assert client.summary_requests, "this fixture must actually trigger compression"
    for request in client.summary_requests:
        body = "\n".join(message["content"] for message in request["messages"])
        for marker in GOLD_MARKERS:
            assert marker not in body
        assert "2023/06/01" not in body, "the question date must not reach the compressor"


def test_the_question_appears_only_in_the_final_answer_request(store, budgets):
    client, _sample_id, _outcome = run_sample(
        store, budgets, session_count=12, messages_per_session=6, words_per_message=9
    )

    with_question = [
        index
        for index, request in enumerate(client.requests)
        if "SECRETQUESTION" in "\n".join(m["content"] for m in request["messages"])
    ]
    assert with_question == [len(client.requests) - 1]


def test_final_answer_prompt_ends_with_the_question(store, budgets):
    client, _sample_id, _outcome = run_sample(store, budgets, session_count=3)
    final_prompt = client.requests[-1]["messages"][1]["content"]
    assert final_prompt.rstrip().endswith("SECRETQUESTION what did I say about the thing?")


def test_prompts_require_atomic_facts_and_direct_factual_answers():
    summary_prompt = prompts.summary_messages("old memory", "older history", 100)[1]["content"]
    assert "Atomic facts" in summary_prompt
    assert "assistant-sourced" in summary_prompt
    assert "Never replace an exact value with a range" in summary_prompt

    answer_prompt = prompts.answer_messages("memory", "tail", "2023/06/01", "What is the count?")[1]["content"]
    assert "exact final conclusion first" in answer_prompt
    assert "never give a conclusion that contradicts" in answer_prompt
    assert answer_prompt.rstrip().endswith("What is the count?")


def test_summary_requests_are_capped_at_the_summary_budget(store, budgets):
    client, _sample_id, _outcome = run_sample(
        store, budgets, session_count=12, messages_per_session=6, words_per_message=9
    )
    assert all(request["max_tokens"] == budgets.summary_budget_tokens for request in client.summary_requests)


def test_the_answer_request_sets_no_output_cap(store, budgets):
    client, _sample_id, _outcome = run_sample(store, budgets, session_count=3)
    assert client.requests[-1]["max_tokens"] is None


def test_no_gold_string_is_ever_written_to_the_trajectory(store, budgets, tmp_path):
    run_sample(store, budgets, session_count=12, messages_per_session=6, words_per_message=9)
    store.conn.commit()

    dump = "\n".join(sqlite3.connect(store.db_path).iterdump())
    assert "SECRETGOLDANSWER" not in dump
    assert "has_answer" not in dump
    assert "answer_session_ids" not in dump


# ── trajectory content ───────────────────────────────────────────────────
def test_calls_and_states_are_persisted(store, budgets):
    _client, sample_id, outcome = run_sample(
        store, budgets, session_count=12, messages_per_session=6, words_per_message=9
    )

    def count(table: str) -> int:
        return store.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE sample_id=?", (sample_id,)
        ).fetchone()[0]

    assert count("calls") == outcome["call_count"] == outcome["compression_count"] + 1
    assert count("states") >= count("calls")


def test_state_log_repeats_summary_and_stores_one_raw_message(store, budgets):
    _client, sample_id, _outcome = run_sample(
        store, budgets, session_count=12, messages_per_session=6, words_per_message=9
    )
    rows = store.conn.execute(
        "SELECT step_ordinal, event, summary_text, raw_text FROM states "
        "WHERE sample_id=? ORDER BY step_ordinal", (sample_id,)
    ).fetchall()
    compression_index = next(index for index, row in enumerate(rows) if row["event"] == "rolling_compression")
    summary = rows[compression_index]["summary_text"]
    assert summary
    assert rows[compression_index]["raw_text"] is None
    following_ingest = rows[compression_index + 1]
    assert following_ingest["event"] == "ingest"
    assert following_ingest["summary_text"] == summary
    assert following_ingest["raw_text"].startswith(("## Session", "User:", "Assistant:"))
    assert all(row["raw_text"] is None for row in rows if row["event"] != "ingest")


def test_summary_calls_record_their_outputs(store, budgets):
    _client, sample_id, _outcome = run_sample(
        store, budgets, session_count=12, messages_per_session=6, words_per_message=9
    )
    rows = store.conn.execute("SELECT response_text FROM calls WHERE kind='summary' ORDER BY call_ordinal").fetchall()
    assert rows
    assert all(row["response_text"] for row in rows)


def test_short_history_produces_one_call_and_no_compression(store, budgets):
    _client, _sample_id, outcome = run_sample(store, budgets, session_count=1, messages_per_session=2)
    assert outcome["compression_count"] == 0
    assert outcome["call_count"] == 1
    assert outcome["status"] == STATUS_COMPLETED


# ── over-length handling ─────────────────────────────────────────────────
def test_a_prompt_at_max_model_len_is_recorded_as_not_runnable(store, budgets):
    client = FakeClient(max_model_len=50)
    sample_id = start(store)
    outcome = process_sample(
        make_row(session_count=8, messages_per_session=6, words_per_message=9),
        client=client,
        budgets=budgets,
        store=store,
        sample_id=sample_id,
        method=METHOD_FULL_CONTEXT,
    )

    assert outcome["status"] == STATUS_NOT_RUNNABLE
    assert outcome["hypothesis"] is None
    assert "max_model_len" in outcome["error"]
    assert outcome["answer_input_tokens"] > 50
    row = store.conn.execute("SELECT * FROM calls WHERE kind='answer'").fetchone()
    assert row["status"] == "not_runnable"


def test_full_context_runs_when_the_history_fits(store, budgets):
    client = FakeClient(max_model_len=100_000)
    sample_id = start(store)
    outcome = process_sample(
        make_row(session_count=2, messages_per_session=2),
        client=client,
        budgets=budgets,
        store=store,
        sample_id=sample_id,
        method=METHOD_FULL_CONTEXT,
    )

    assert outcome["status"] == STATUS_COMPLETED
    assert outcome["compression_count"] == 0
    assert len(client.requests) == 1


def test_a_summary_prompt_with_no_generation_headroom_fails_loudly(store, budgets):
    client = FakeClient(max_model_len=60, summary_max_tokens=budgets.summary_budget_tokens)
    sample_id = start(store)
    with pytest.raises(ApiError, match="headroom"):
        process_sample(
            make_row(session_count=12, messages_per_session=6, words_per_message=9),
            client=client,
            budgets=budgets,
            store=store,
            sample_id=sample_id,
            method=METHOD_ROLLING_SUMMARY,
        )


# ── transport retries ────────────────────────────────────────────────────
class FlakyTransport:
    def __init__(self, failures: int, status: int = 503) -> None:
        self.failures = failures
        self.status = status
        self.calls = 0

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise requests.ConnectionError("connection reset")
        return _Response(200, {"count": 3, "max_model_len": 262144, "tokens": [1, 2, 3]})


class _Response:
    def __init__(self, status_code: int, payload: dict, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> dict:
        return self._payload


def test_transient_failures_are_retried_up_to_three_attempts(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    transport = FlakyTransport(failures=2)
    client = QwenClient(ModelConfig(base_url="http://fake/v1", max_attempts=3), "key", transport)

    assert client.tokenize_text("a b c") == 3
    assert transport.calls == 3


def test_a_fourth_failure_is_not_attempted(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    transport = FlakyTransport(failures=5)
    client = QwenClient(ModelConfig(base_url="http://fake/v1", max_attempts=3), "key", transport)

    with pytest.raises(ApiError):
        client.tokenize_text("a b c")
    assert transport.calls == 3


def test_client_errors_are_not_retried(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    class BadRequest:
        calls = 0

        def post(self, url, json=None, headers=None, timeout=None):
            BadRequest.calls += 1
            return _Response(400, {}, text="bad template kwarg")

    client = QwenClient(ModelConfig(base_url="http://fake/v1", max_attempts=3), "key", BadRequest())
    with pytest.raises(ApiError, match="HTTP 400"):
        client.tokenize_text("a")
    assert BadRequest.calls == 1


def test_tokenize_endpoint_sits_at_the_service_root_not_under_v1():
    seen: list[str] = []

    class Recorder:
        def post(self, url, json=None, headers=None, timeout=None):
            seen.append(url)
            return _Response(200, {"count": 1, "max_model_len": 262144, "tokens": [1]})

    client = QwenClient(ModelConfig(base_url="http://fake/v1"), "key", Recorder())
    client.tokenize_text("a")
    assert seen == ["http://fake/tokenize"]


# ── resume and exports ───────────────────────────────────────────────────
def write_fixture_manifest(tmp_path, question_ids):
    manifest = tmp_path / "manifest.csv"
    lines = ["dataset_index,question_id,question_type"]
    lines += [f"{index},{qid},single-session-user" for index, qid in enumerate(question_ids)]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps([make_row(question_id=qid, session_count=2, messages_per_session=2) for qid in question_ids]),
        encoding="utf-8",
    )
    return manifest, source


class PreflightingFakeClient(FakeClient):
    def preflight(self) -> dict:
        return {
            "served_models": ["fake-model"],
            "max_model_len": self.max_model_len,
            "probe_prompt_tokens": 6,
            "probe_output": "OK",
            "probe_finish_reason": "stop",
            "chat_template_kwargs": {"enable_thinking": False},
        }


def test_run_writes_all_four_artifacts(tmp_path, budgets):
    manifest, source = write_fixture_manifest(tmp_path, ["q1", "q2"])
    statistics = run(
        manifest_path=manifest,
        run_id="r1",
        source_path=source,
        results_root=tmp_path / "results",
        budgets=budgets,
        client=PreflightingFakeClient(),
    )
    run_dir = tmp_path / "results" / "r1"

    assert {path.name for path in run_dir.iterdir()} >= {
        "config.json",
        "trajectory.sqlite3",
        "hypotheses.jsonl",
        "run_summary.json",
    }
    records = [json.loads(line) for line in (run_dir / "hypotheses.jsonl").read_text().splitlines()]
    assert [record["question_id"] for record in records] == ["q1", "q2"]
    assert all(set(record) == {"question_id", "hypothesis"} and record["hypothesis"] for record in records)
    assert statistics["status_counts"] == {STATUS_COMPLETED: 2}
    assert statistics["hypotheses_exported"] == 2


def test_config_json_records_the_frozen_protocol(tmp_path, budgets):
    manifest, source = write_fixture_manifest(tmp_path, ["q1"])
    run(
        manifest_path=manifest,
        run_id="r1",
        source_path=source,
        results_root=tmp_path / "results",
        budgets=budgets,
        client=PreflightingFakeClient(),
    )
    config = json.loads((tmp_path / "results" / "r1" / "config.json").read_text())

    assert config["budgets"]["rolling_trigger_tokens"] == budgets.rolling_trigger_tokens
    assert config["model"]["temperature"] == 0.0
    assert config["model"]["enable_thinking"] is False
    assert config["prompt_version"] and config["code_version"] and config["config_fingerprint"]
    assert config["manifest"]["sha256"]
    assert config["execution"] == {"max_concurrency": 1}
    assert "api_key" not in json.dumps(config).lower().replace("api_key_env", "")


def test_run_rejects_invalid_concurrency(tmp_path, budgets):
    manifest, source = write_fixture_manifest(tmp_path, ["q1"])
    with pytest.raises(ValueError, match="max_concurrency"):
        run(
            manifest_path=manifest,
            run_id="r1",
            source_path=source,
            results_root=tmp_path / "results",
            budgets=budgets,
            client=PreflightingFakeClient(),
            max_concurrency=0,
        )


def test_rerunning_the_same_run_id_skips_completed_samples(tmp_path, budgets):
    manifest, source = write_fixture_manifest(tmp_path, ["q1", "q2"])
    kwargs = dict(
        manifest_path=manifest,
        run_id="r1",
        source_path=source,
        results_root=tmp_path / "results",
        budgets=budgets,
    )
    first = PreflightingFakeClient()
    run(client=first, **kwargs)
    second = PreflightingFakeClient()
    statistics = run(client=second, **kwargs)

    assert first.requests, "the first pass must actually call the model"
    assert second.requests == [], "a completed sample must not be re-requested"
    assert statistics["sample_count"] == 2
    assert statistics["hypotheses_exported"] == 2


def test_a_changed_fingerprint_refuses_to_reuse_the_run_id(tmp_path, budgets):
    manifest, source = write_fixture_manifest(tmp_path, ["q1"])
    kwargs = dict(
        manifest_path=manifest,
        run_id="r1",
        source_path=source,
        results_root=tmp_path / "results",
    )
    run(budgets=budgets, client=PreflightingFakeClient(), **kwargs)

    changed = type(budgets)(
        rolling_trigger_tokens=budgets.rolling_trigger_tokens,
        summary_budget_tokens=budgets.summary_budget_tokens,
        compress_prefix_tokens=budgets.compress_prefix_tokens - 1,
    )
    with pytest.raises(ValueError, match="Use a new --run-id"):
        run(budgets=changed, client=PreflightingFakeClient(), **kwargs)


def test_one_failing_sample_does_not_abort_the_run(tmp_path, budgets):
    manifest, source = write_fixture_manifest(tmp_path, ["q1", "q2"])

    class FailsOnFirstQuestion(PreflightingFakeClient):
        def chat(self, messages, *, max_tokens=None):
            body = "\n".join(message["content"] for message in messages)
            if "s0m0w0" in body and not self.requests:
                self.requests.append({"messages": list(messages), "max_tokens": max_tokens})
                raise ApiError("service exploded", retryable=False)
            return super().chat(messages, max_tokens=max_tokens)

    statistics = run(
        manifest_path=manifest,
        run_id="r1",
        source_path=source,
        results_root=tmp_path / "results",
        budgets=budgets,
        client=FailsOnFirstQuestion(),
    )

    assert statistics["status_counts"] == {STATUS_FAILED: 1, STATUS_COMPLETED: 1}
    assert statistics["hypotheses_exported"] == 1


def test_a_failed_sample_is_retried_on_the_next_pass(tmp_path, budgets):
    manifest, source = write_fixture_manifest(tmp_path, ["q1"])
    kwargs = dict(
        manifest_path=manifest,
        run_id="r1",
        source_path=source,
        results_root=tmp_path / "results",
        budgets=budgets,
    )

    class AlwaysFails(PreflightingFakeClient):
        def chat(self, messages, *, max_tokens=None):
            raise ApiError("down", retryable=False)

    run(client=AlwaysFails(), **kwargs)
    statistics = run(client=PreflightingFakeClient(), **kwargs)

    assert statistics["status_counts"] == {STATUS_COMPLETED: 1}
    assert statistics["hypotheses_exported"] == 1
