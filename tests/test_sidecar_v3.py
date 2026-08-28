from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from memory_sidecar.v3 import (
    V3MemoryState,
    answer_v3_messages,
    compactor_v3_messages,
    compile_evidence,
    fit_answer_v3_context,
    is_truncated_json,
    manager_v3_messages,
    parse_compactor_response,
    parse_manager_response,
    render_compactor_v3_input,
    split_chunk_at_turn_boundary,
    state_sha256_v3,
)
from memory_sidecar.protocol import answer_messages
from utils.client import CallResult
from utils.store import TrajectoryStore


class TinyTokenizer:
    def __init__(self, *args, **kwargs):
        del args, kwargs

    def count(self, text: str) -> int:
        return len(text.split())

    def encode(self, text: str) -> list[int]:
        return list(range(self.count(text)))

    def decode(self, token_ids: list[int]) -> str:
        return " ".join(f"t{index}" for index in token_ids)


def _messages():
    return [
        SimpleNamespace(unit_ordinal=10, session_id="s1", session_date="2023/05/05", role="user", content="I paid $120."),
        SimpleNamespace(unit_ordinal=11, session_id="s1", session_date="2023/05/05", role="assistant", content="Noted."),
        SimpleNamespace(unit_ordinal=12, session_id="s2", session_date="2023/05/06", role="user", content="I use Spotify."),
    ]


def _event(action: str, key: str, value, evidence_ids: list[int], **extra):
    return {
        "action": action, "memory_type": extra.pop("memory_type", "fact"), "key": key,
        "value": value, "status": extra.pop("status", "active"), "event_date": None,
        "source": {"evidence_ids": evidence_ids}, "confidence": None, "qualifier": None, **extra,
    }


def _items(events):
    return parse_manager_response(json.dumps({"events": events}), compile_evidence(_messages()))


def test_v3_routes_three_independent_events_and_preserves_each_source_ref():
    state = V3MemoryState()
    items = _items([
        _event("ADD", "expense.helmet", {"amount": 120, "currency": "USD"}, [0]),
        _event("ADD", "music.service", "Spotify", [2]),
        _event("ADD", "profile.city", "Shanghai", [0, 2]),
    ])
    routes = state.route_batch(items, batch_ordinal=1)
    assert [route["route_status"] for route in routes] == ["applied"] * 3
    assert [record["event_id"] for record in state.records] == ["b1-i0", "b1-i1", "b1-i2"]
    assert state.records[2]["source"] == {"source_refs": [
        {"evidence_id": 0, "session_id": "s1", "unit_ordinal": 10},
        {"evidence_id": 2, "session_id": "s2", "unit_ordinal": 12},
    ]}


def test_v3_manager_prompt_requires_integer_evidence_ids():
    prompt = manager_v3_messages('{"version":3}', compile_evidence(_messages()))[1]["content"]
    assert '"e0"/"e3" strings' in prompt
    assert '"evidence_ids":[0,3]' in prompt
    assert "never combine a target and an observation" in prompt
    assert "evidence explicitly" in prompt
    assert "states the new total" in prompt


def test_v3_reuses_the_v1_answer_prompt_without_additional_instructions():
    args = ('{"version":3,"records":[]}', 'recent tail', '2023/01/01', 'Question?')
    assert answer_v3_messages(*args) == answer_messages(*args)


def test_v3_compactor_input_keeps_record_ids_and_response_is_summary_only():
    state = V3MemoryState()
    state.route_batch(_items([_event("ADD", "profile.city", "Shanghai", [0])]), batch_ordinal=2)
    memory, record_ids = render_compactor_v3_input(state)
    assert record_ids == ["b2-i0"]
    assert json.loads(memory)["records"][0]["event_id"] == "b2-i0"
    prompt = compactor_v3_messages(memory)[1]["content"]
    assert "recent user update" not in prompt
    assert "separate recent raw tail" in prompt
    assert parse_compactor_response('{"summary_text":"Lives in Shanghai."}') == "Lives in Shanghai."
    with pytest.raises(ValueError, match="only summary_text"):
        parse_compactor_response('{"summary_text":"x","delete":[]}')


def test_v3_duplicate_key_rejects_whole_batch_and_invalid_evidence_is_item_local():
    state = V3MemoryState()
    items = _items([
        _event("ADD", "profile.city", "Shanghai", [0]),
        _event("ADD", "profile.city", "Beijing", [2]),
        _event("ADD", "music.service", "Spotify", [999]),
        _event("ADD", "profile.name", "Ada", [0]),
    ])
    routes = state.route_batch(items, batch_ordinal=4)
    assert [route["route_status"] for route in routes] == [
        "rejected_duplicate_key_in_batch", "rejected_duplicate_key_in_batch",
        "rejected_invalid_evidence", "applied",
    ]
    assert [record["key"] for record in state.records] == ["profile.name"]


def test_v3_update_replaces_whole_snapshot_and_current_projection_excludes_superseded():
    state = V3MemoryState()
    assert state.route_batch(_items([_event("ADD", "mortgage.wells_fargo.preapproval", {"amount": 350000}, [0])]), batch_ordinal=1)[0]["route_status"] == "applied"
    route = state.route_batch(_items([_event("UPDATE", "mortgage.wells_fargo.preapproval", {"amount": 400000}, [2])]), batch_ordinal=2)[0]
    assert route["route_status"] == "applied"
    assert [record["status"] for record in state.records] == ["superseded", "active"]
    assert json.loads(state.render_for_answer())["records"] == [{
        "key": "mortgage.wells.fargo.preapproval", "memory_type": "fact", "status": "active", "value": {"amount": 400000},
    }]
    assert state_sha256_v3(state) == state_sha256_v3(V3MemoryState.from_json(state.to_json()))


def test_v3_update_requires_exact_existing_key_and_compatible_type():
    state = V3MemoryState()
    state.route_batch(_items([_event("ADD", "person.rachel.location", "Chicago", [0])]), batch_ordinal=1)
    drift = state.route_batch(_items([_event("UPDATE", "person.rachel.current_location", "Tampa", [2])]), batch_ordinal=2)[0]
    mismatch = state.route_batch(_items([_event("UPDATE", "person.rachel.location", "Tampa", [2], memory_type="plan")]), batch_ordinal=3)[0]
    assert drift["route_status"] == "rejected_key_drift"
    assert drift["key_drift_candidate"]["candidate_key"] == "person.rachel.location"
    assert mismatch["route_status"] == "rejected_update_type_mismatch"
    assert state.records[0]["value"] == "Chicago"


def _start_store(path: Path) -> tuple[TrajectoryStore, int]:
    store = TrajectoryStore(path)
    store.start_run("v3", {"method": "memory_sidecar_v3", "config_fingerprint": "fp", "model": {"model": "fake"}})
    sample_id = store.start_sample("v3", "q", dataset_index=1, question_type="x", config_fingerprint="fp", code_version="test")
    return store, sample_id


def test_v3_store_is_atomic_and_reads_by_explicit_batch_item_order(tmp_path):
    store, sample_id = _start_store(tmp_path / "trajectory.sqlite3")
    good = {
        "event_id": "b2-i0", "memory_type": "fact", "key": "x", "value": 1, "status": "active",
        "event_date": None, "source": {"source_refs": [{"evidence_id": 0}]}, "source_unit_ordinals": [1], "confidence": None,
    }
    bad = {**good, "event_id": "not-a-v3-id", "key": "bad"}
    with pytest.raises(ValueError, match="event_id"):
        store.record_v3_batch(sample_id, batch_ordinal=2, source_unit_ordinals=[1], input_text="x", memory_before_json="{}",
                              raw_response="{}", parse_status="ok", items=[], records=[good, bad], state_json="{}", state_sha256="x")
    assert store.conn.execute("SELECT count(*) FROM sidecar_batches").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM sidecar_memory").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM sidecar_states").fetchone()[0] == 0
    second = {**good, "event_id": "b2-i1", "key": "second", "value": 2}
    store.record_v3_batch(sample_id, batch_ordinal=2, source_unit_ordinals=[1], input_text="x", memory_before_json="{}",
                          raw_response="{}", parse_status="ok", items=[], records=[good, second], state_json="{}", state_sha256="x")
    assert [record["event_id"] for record in store.load_sidecar_memory_v3(sample_id)] == ["b2-i0", "b2-i1"]
    store.close()


def test_v3_store_persists_compaction_snapshot_for_auditable_reuse(tmp_path):
    store, sample_id = _start_store(tmp_path / "trajectory.sqlite3")
    store.record_v3_compaction(
        sample_id, compaction_run_id="compact-a", memory_snapshot_sha256="snapshot", input_memory_json='{"records":[]}',
        input_raw_tail="recent tail", input_raw_tail_sha256="tail-hash", input_record_ids=["b1-i0"],
        model="fake", prompt_version="compact-v1", raw_response='{"summary_text":"x"}',
        parse_status="ok", summary_text="x", input_tokens=4, output_tokens=1, latency_ms=2,
    )
    saved = store.load_v3_compaction(run_id="v3", question_id="q", compaction_run_id="compact-a")
    assert saved is not None
    assert saved["memory_snapshot_sha256"] == "snapshot"
    assert saved["input_record_ids"] == ["b1-i0"]
    assert saved["input_raw_tail"] == "recent tail"
    assert saved["input_raw_tail_sha256"] == "tail-hash"
    assert saved["summary_text"] == "x"
    assert saved["raw_response"] == '{"summary_text":"x"}'
    store.close()


def test_v3_truncation_detection_split_and_shared_tail_budget():
    assert is_truncated_json('{"events":[')
    assert not is_truncated_json('{"events":[bad]}')
    messages = [
        SimpleNamespace(role="user", session_index=0), SimpleNamespace(role="assistant", session_index=0),
        SimpleNamespace(role="user", session_index=0), SimpleNamespace(role="assistant", session_index=0),
    ]
    split = split_chunk_at_turn_boundary(messages)
    assert split is not None and [len(part) for part in split] == [2, 2]
    memory = json.dumps({"version": 3, "records": [{"key": "x", "value": "current", "status": "active"}]})
    blocks = [
        {"text": "User: old one"}, {"text": "Assistant: old reply"},
        {"text": "User: recent one"}, {"text": "Assistant: recent reply"},
    ]
    prompt, tail, _tokens, trimmed = fit_answer_v3_context(memory, "\n".join(block["text"] for block in blocks), "2023", "Q", TinyTokenizer(), 113, 2, blocks)
    assert prompt is not None
    assert "recent one" in tail and "old one" not in tail
    assert trimmed > 0


def test_v3_process_splits_truncated_parent_then_commits_children(tmp_path, monkeypatch):
    from memory_sidecar import process_v3

    baseline = tmp_path / "baseline.sqlite3"
    baseline_store, baseline_sample_id = _start_store(baseline)
    baseline_store.conn.execute(
        "INSERT INTO states (sample_id,step_ordinal,event,created_at,summary_text,raw_text,summary_tokens,raw_tail_tokens,history_tokens,detail) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (baseline_sample_id, 1, "final", 0.0, "", None, 0, 0, 0, "{}"),
    )
    baseline_store.conn.commit()
    baseline_store.close()

    responses = iter([
        ('{"events":[', "length"),
        (json.dumps({"events": [_event("ADD", "fact.first", "one", [0])]}), "stop"),
        (json.dumps({"events": [_event("ADD", "fact.second", "two", [0])]}), "stop"),
        ("answer", "stop"),
    ])

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.max_model_len = 10000

        def chat(self, messages, *, max_tokens=None):
            del messages, max_tokens
            content, finish_reason = next(responses)
            return CallResult(content=content, finish_reason=finish_reason, input_tokens=1, output_tokens=1,
                              latency_ms=1, attempts=1, request_params={}, prompt_sha256="p", response_sha256="r")

    monkeypatch.setattr(process_v3, "QwenClient", FakeClient)
    monkeypatch.setattr(process_v3, "LocalQwenTokenizer", TinyTokenizer)
    args = SimpleNamespace(
        base_url="http://fake/v1", model="fake", manager_max_tokens=64, answer_max_tokens=16,
        timeout_seconds=1, tokenizer_path=None, run_id="v3-run", baseline_db=baseline,
        baseline_run_id="v3", chunk_budget_tokens=100, debug_max_chunks=0, shared_context_budget_tokens=1000,
    )
    config = {"config_fingerprint": "fp", "code_version": "test"}
    row = {
        "question_id": "q", "question_date": "2023/01/01", "question": "What?", "dataset_index": 1,
        "question_type": "x", "haystack_dates": ["2023/01/01 (Sun) 00:00"], "haystack_session_ids": ["s"],
        "haystack_sessions": [[
            {"role": "user", "content": "first"}, {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "second"}, {"role": "assistant", "content": "ok"},
        ]],
    }
    output = tmp_path / "v3.sqlite3"
    store = TrajectoryStore(output)
    store.start_run("v3-run", {"method": "memory_sidecar_v3", "config_fingerprint": "fp", "model": {"model": "fake"}})
    store.close()
    assert process_v3.process_one_v3(row, args=args, config=config, db_path=output, api_key="x")["status"] == "completed"
    store = TrajectoryStore(output)
    metrics = json.loads(store.conn.execute("SELECT metrics_json FROM sidecar_v3_metrics").fetchone()[0])
    assert metrics["terminal_leaf_manager_batches"] == 2
    assert metrics["effective_leaf_chunk_count"] == 2
    assert metrics["truncated_parent_requests"] == 1
    assert metrics["manager_completion_requests"] == 3
    assert metrics["answer_completions"] == 1
    sample_id = store.conn.execute("SELECT id FROM samples").fetchone()[0]
    assert [record["key"] for record in store.load_sidecar_memory_v3(sample_id)] == ["fact.first", "fact.second"]
    store.close()


def test_v3_compactor_on_passes_only_saved_summary_to_answer(tmp_path, monkeypatch):
    from memory_sidecar import process_v3

    baseline = tmp_path / "baseline.sqlite3"
    baseline_store, baseline_sample_id = _start_store(baseline)
    baseline_store.conn.execute(
        "INSERT INTO states (sample_id,step_ordinal,event,created_at,summary_text,raw_text,summary_tokens,raw_tail_tokens,history_tokens,detail) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (baseline_sample_id, 1, "final", 0.0, "", "RECENT RAW TAIL", 0, 0, 0, "{}"),
    )
    baseline_store.conn.execute(
        "INSERT INTO states (sample_id,step_ordinal,event,created_at,summary_text,raw_text,summary_tokens,raw_tail_tokens,history_tokens,detail) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (baseline_sample_id, 2, "ingest", 0.0, "", "RECENT RAW TAIL", 0, 0, 0, '{"unit_ordinal":1}'),
    )
    baseline_store.conn.commit()
    baseline_store.close()

    prompts = []
    responses = iter([
        (json.dumps({"events": [_event("ADD", "profile.city", "Shanghai", [0])]}), "stop"),
        ('{"summary_text":"User lives in Shanghai."}', "stop"),
        ("Shanghai", "stop"),
    ])

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.max_model_len = 10000

        def chat(self, messages, *, max_tokens=None):
            prompts.append(messages)
            content, finish_reason = next(responses)
            return CallResult(content=content, finish_reason=finish_reason, input_tokens=1, output_tokens=1,
                              latency_ms=1, attempts=1, request_params={}, prompt_sha256="p", response_sha256="r")

    monkeypatch.setattr(process_v3, "QwenClient", FakeClient)
    monkeypatch.setattr(process_v3, "LocalQwenTokenizer", TinyTokenizer)
    args = SimpleNamespace(
        base_url="http://fake/v1", model="fake", manager_max_tokens=64, answer_max_tokens=16,
        compactor_max_tokens=32, compactor="on", compaction_run_id="compact-a", timeout_seconds=1,
        tokenizer_path=None, run_id="v3-run", baseline_db=baseline, baseline_run_id="v3",
        chunk_budget_tokens=100, debug_max_chunks=0, shared_context_budget_tokens=1000,
    )
    config = {"config_fingerprint": "fp", "code_version": "test"}
    row = {
        "question_id": "q", "question_date": "2023/01/01", "question": "Where?", "dataset_index": 1,
        "question_type": "x", "haystack_dates": ["2023/01/01 (Sun) 00:00"], "haystack_session_ids": ["s"],
        "haystack_sessions": [[["unused"]]],
    }
    row["haystack_sessions"] = [[{"role": "user", "content": "I live in Shanghai."}]]
    output = tmp_path / "v3.sqlite3"
    store = TrajectoryStore(output)
    store.start_run("v3-run", {"method": "memory_sidecar_v3", "config_fingerprint": "fp", "model": {"model": "fake"}})
    store.close()
    assert process_v3.process_one_v3(row, args=args, config=config, db_path=output, api_key="x")["status"] == "completed"
    answer_prompt = prompts[-1]
    answer_user = answer_prompt[1]["content"]
    assert "User lives in Shanghai." in answer_user
    assert "RECENT RAW TAIL" in answer_user
    assert '"records"' not in answer_user
    assert "RECENT RAW TAIL" not in prompts[1][1]["content"]
    store = TrajectoryStore(output)
    compaction = store.load_v3_compaction(run_id="v3-run", question_id="q", compaction_run_id="compact-a")
    assert compaction is not None and compaction["summary_text"] == "User lives in Shanghai."
    store.close()


def test_v3_replay_reuses_saved_summary_without_manager_or_compactor_calls(tmp_path, monkeypatch):
    from memory_sidecar import process_v3

    source_db = tmp_path / "source.sqlite3"
    source_store, source_sample_id = _start_store(source_db)
    source_record = {
        "event_id": "b1-i0", "memory_type": "fact", "key": "profile.city", "value": "Shanghai",
        "status": "active", "event_date": None, "source": {"source_refs": [{"evidence_id": 0}]},
        "source_unit_ordinals": [1], "confidence": None,
    }
    source_store.record_v3_batch(
        source_sample_id, batch_ordinal=1, source_unit_ordinals=[1], input_text="x", memory_before_json='{"version":3,"records":[]}',
        raw_response="{}", parse_status="ok", items=[], records=[source_record],
        state_json=json.dumps({"version": 3, "records": [source_record]}), state_sha256="state",
    )
    source_store.finish_sample(source_sample_id, "completed")
    source_state = V3MemoryState(records=[source_record])
    source_memory, source_record_ids = render_compactor_v3_input(source_state)
    from utils.config import sha256_text
    source_tail = ""
    source_snapshot = sha256_text(source_memory)
    source_store.record_v3_compaction(
        source_sample_id, compaction_run_id="compact-a", memory_snapshot_sha256=source_snapshot,
        input_memory_json=source_memory, input_raw_tail=source_tail, input_raw_tail_sha256=sha256_text(source_tail),
        input_record_ids=source_record_ids, model="fake", prompt_version="compact-v1",
        raw_response='{"summary_text":"User lives in Shanghai."}', parse_status="ok", summary_text="User lives in Shanghai.",
    )
    source_store.close()

    baseline = tmp_path / "baseline.sqlite3"
    baseline_store, baseline_sample_id = _start_store(baseline)
    baseline_store.conn.execute(
        "INSERT INTO states (sample_id,step_ordinal,event,created_at,summary_text,raw_text,summary_tokens,raw_tail_tokens,history_tokens,detail) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (baseline_sample_id, 1, "final", 0.0, "", "TAIL MUST NOT REACH ANSWER", 0, 0, 0, "{}"),
    )
    baseline_store.conn.commit()
    baseline_store.close()

    prompts = []
    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.max_model_len = 10000

        def chat(self, messages, *, max_tokens=None):
            prompts.append(messages)
            return CallResult(content="Shanghai", finish_reason="stop", input_tokens=1, output_tokens=1,
                              latency_ms=1, attempts=1, request_params={}, prompt_sha256="p", response_sha256="r")

    monkeypatch.setattr(process_v3, "QwenClient", FakeClient)
    monkeypatch.setattr(process_v3, "LocalQwenTokenizer", TinyTokenizer)
    args = SimpleNamespace(
        base_url="http://fake/v1", model="fake", manager_max_tokens=64, answer_max_tokens=16,
        compactor_max_tokens=32, compactor="reuse", compaction_run_id="compact-a", timeout_seconds=1,
        tokenizer_path=None, run_id="replay-target", baseline_db=baseline, baseline_run_id="v3",
        chunk_budget_tokens=100, debug_max_chunks=0, shared_context_budget_tokens=1000,
        compaction_source_db=source_db, compaction_source_run_id="v3",
        replay_source_db=source_db, replay_source_run_id="v3",
    )
    config = {"config_fingerprint": "fp", "code_version": "test"}
    row = {
        "question_id": "q", "question_date": "2023/01/01", "question": "Where?", "dataset_index": 1,
        "question_type": "x", "haystack_dates": ["2023/01/01 (Sun) 00:00"], "haystack_session_ids": ["s"],
        "haystack_sessions": [[{"role": "user", "content": "different source should not be routed"}]],
    }
    target = tmp_path / "target.sqlite3"
    target_store = TrajectoryStore(target)
    target_store.start_run("replay-target", {"method": "memory_sidecar_v3", "config_fingerprint": "fp", "model": {"model": "fake"}})
    target_store.close()
    assert process_v3.process_one_v3(row, args=args, config=config, db_path=target, api_key="x")["status"] == "completed"
    assert len(prompts) == 1
    assert "User lives in Shanghai." in prompts[0][1]["content"]
    assert "TAIL MUST NOT REACH ANSWER" not in prompts[0][1]["content"]
    target_store = TrajectoryStore(target)
    metrics = json.loads(target_store.conn.execute("SELECT metrics_json FROM sidecar_v3_metrics").fetchone()[0])
    assert metrics["manager_completion_requests"] == 0
    assert metrics["compactor_completions"] == 0
    assert metrics["compactor_reused"] == 1
    assert metrics["manager_trajectory_reused"] == 1
    target_store.close()


def test_v3_runner_defaults_to_8192_chunk_and_4096_output():
    import importlib.util

    path = Path(__file__).parents[1] / "scripts" / "run_memory_sidecar_strong.py"
    spec = importlib.util.spec_from_file_location("run_memory_sidecar_strong_v3", path)
    assert spec and spec.loader
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    args = SimpleNamespace(protocol="v3", chunk_budget_tokens=None, manager_max_tokens=None)
    runner.apply_protocol_defaults(args)
    assert (args.chunk_budget_tokens, args.manager_max_tokens) == (8192, 4096)
