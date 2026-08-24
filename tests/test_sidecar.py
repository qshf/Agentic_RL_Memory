"""Structured Sidecar protocol and trajectory persistence tests."""
from __future__ import annotations

import json

import pytest

from memory_sidecar.budget import chunks, trim_tail, turn_units
from memory_sidecar.protocol import MemoryState, parse_event, state_sha256
from utils.store import STATUS_COMPLETED, TrajectoryStore


def test_parse_event_accepts_json_code_fence_and_preserves_exact_values() -> None:
    event = parse_event(
        "```json\n"
        '{"action":"ADD","memory_type":"fact","key":"money.total",'
        '"value":"$400,000","status":"active","event_date":"2023-08-20",'
        '"source":{"session_id":"s1","message_indices":[7]},"confidence":0.9}\n'
        "```",
        default_source={"session_id": "fallback", "message_indices": [0]},
    )
    assert event.key == "money.total"
    assert event.value == "$400,000"
    assert event.source["session_id"] == "s1"


def test_state_update_replaces_old_record_and_keeps_history() -> None:
    state = MemoryState()
    first = parse_event(
        json.dumps({
            "action": "ADD",
            "memory_type": "fact",
            "key": "mortgage.preapproval",
            "value": "$350,000",
            "status": "active",
        }),
        default_source={"session_id": "s1"},
    )
    second = parse_event(
        json.dumps({
                "action": "UPDATE",
            "memory_type": "fact",
            "key": "mortgage.preapproval",
            "value": "$400,000",
            "status": "active",
        }),
        default_source={"session_id": "s2"},
    )
    assert state.apply(first, event_id="event-1", source_unit_ordinals=[1])["route_status"] == "applied"
    route = state.apply(second, event_id="event-2", source_unit_ordinals=[2])
    assert route["superseded_event_ids"] == ["event-1"]
    assert [record["status"] for record in state.records] == ["superseded", "active"]
    assert state.active_records[0]["value"] == "$400,000"
    assert state_sha256(state) == state_sha256(MemoryState.from_json(state.to_json()))


def test_manager_render_includes_active_records_and_update_ledger() -> None:
    state = MemoryState()
    for index, value in enumerate(("old", "new"), 1):
        event = parse_event(
            json.dumps({
                "action": "ADD" if index == 1 else "UPDATE",
                "memory_type": "fact",
                "key": "profile.value",
                "value": value,
                "status": "active",
            }),
            default_source={},
        )
        state.apply(event, event_id=f"event-{index}", source_unit_ordinals=[index])
    rendered = json.loads(state.render_for_manager(max_active_records=8, updates_per_key=2))
    assert rendered["active_records"][0]["value"] == "new"
    assert [item["value"] for item in rendered["update_ledger"]] == ["old"]
    assert "event_id" not in rendered["active_records"][0]
    assert "confidence" not in rendered["active_records"][0]
    assert "source_unit_ordinals" not in rendered["active_records"][0]
    assert json.loads(state.render_for_manager(max_active_records=0, updates_per_key=0))["active_records"] == []


def test_answer_render_projects_internal_record_to_compact_fact() -> None:
    state = MemoryState(records=[{
        "event_id": "event-2",
        "memory_type": "fact",
        "key": "user.last_name",
        "value": "Thompson",
        "status": "active",
        "event_date": None,
        "source": {"session_id": "4", "message_indices": [20]},
        "source_unit_ordinals": [20, 21, 22],
        "confidence": 1.0,
    }])
    rendered = json.loads(state.render_for_answer())
    assert rendered == {
        "version": 1,
        "records": [{
            "key": "user.last_name",
            "memory_type": "fact",
            "source": {"message_indices": [20], "session_id": "4"},
            "status": "active",
            "value": "Thompson",
        }],
    }


def test_conflicting_add_is_rejected_without_mutating_state() -> None:
    state = MemoryState()
    add = parse_event(
        json.dumps({
            "action": "ADD",
            "memory_type": "fact",
            "key": "coin.count",
            "value": 37,
            "status": "active",
        }),
        default_source={},
    )
    conflict = parse_event(
        json.dumps({
            "action": "ADD",
            "memory_type": "fact",
            "key": "coin.count",
            "value": 38,
            "status": "active",
        }),
        default_source={},
    )
    state.apply(add, event_id="event-1", source_unit_ordinals=[1])
    route = state.apply(conflict, event_id="event-2", source_unit_ordinals=[2])
    assert route["route_status"] == "rejected_add_conflict"
    assert len(state.records) == 1
    assert state.records[0]["value"] == 37


def test_supersede_action_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid sidecar action"):
        parse_event('{"action":"SUPERSEDE"}', default_source={})


def test_superseded_status_is_reserved_for_router() -> None:
    with pytest.raises(ValueError, match="invalid event status"):
        parse_event(
            '{"action":"ADD","memory_type":"fact","key":"x","value":"y","status":"superseded"}',
            default_source={},
        )


def test_store_persists_raw_event_and_materialized_state(tmp_path) -> None:
    store = TrajectoryStore(tmp_path / "trajectory.sqlite3")
    store.start_run(
        "run-1",
        {
            "method": "memory_sidecar_strong",
            "config_fingerprint": "fp-a",
            "model": {"model": "fake-model"},
        },
    )
    sample_id = store.start_sample(
        "run-1",
        "sidecar-q",
        dataset_index=1,
        question_type="multi-session",
        config_fingerprint="fp-a",
        code_version="test",
    )
    event_id = store.record_sidecar_event(
        sample_id,
        event_ordinal=1,
        chunk_start_ordinal=4,
        chunk_end_ordinal=5,
        source_unit_ordinals=[4, 5],
        input_text="User: I moved.",
        memory_before_json='{"version":1,"records":[]}',
        raw_response='{"action":"ADD"}',
        parse_status="ok",
        parsed_event={"action": "ADD"},
        route_status="applied",
        route_result={"changed": True},
    )
    state_id = store.record_sidecar_state(
        sample_id,
        event_ordinal=1,
        state_json='{"version":1,"records":[]}',
        state_sha256="abc",
        active_record_count=0,
    )
    store.sync_sidecar_memory(
        sample_id,
        [{
            "event_id": "event-1",
            "memory_type": "fact",
            "key": "profile.city",
            "value": "Shanghai",
            "status": "active",
            "event_date": None,
            "source": {"session_id": "s1"},
            "source_unit_ordinals": [4],
            "confidence": 0.9,
        }],
    )
    store.record_sidecar_context(
        sample_id,
        baseline_sample_id=9,
        baseline_final_step=20,
        baseline_compression_step=15,
        baseline_cut_index=100,
        raw_tail_sha256="tail-hash",
        raw_tail_tokens=16,
        raw_tail_full_tokens=24,
        raw_tail_trimmed=True,
        tail_source_ordinals=[101, 102],
    )
    store.finish_sample(sample_id, STATUS_COMPLETED, hypothesis="ok")
    event = store.conn.execute("SELECT * FROM sidecar_events WHERE id=?", (event_id,)).fetchone()
    snapshot = store.conn.execute("SELECT * FROM sidecar_states WHERE id=?", (state_id,)).fetchone()
    context = store.conn.execute("SELECT * FROM sidecar_context WHERE sample_id=?", (sample_id,)).fetchone()
    memory = store.conn.execute("SELECT * FROM sidecar_memory WHERE sample_id=?", (sample_id,)).fetchone()
    assert event["raw_response"] == '{"action":"ADD"}'
    assert json.loads(event["route_result_json"])["changed"] is True
    assert snapshot["state_sha256"] == "abc"
    assert context["baseline_sample_id"] == 9
    assert context["raw_tail_trimmed"] == 1
    assert json.loads(memory["value_json"]) == "Shanghai"
    restored = store.load_sidecar_memory(sample_id)
    assert restored[0]["key"] == "profile.city"
    assert restored[0]["value"] == "Shanghai"
    store.close()


class TinyTokenizer:
    """Deterministic local-tokenizer stand-in for budget tests."""

    def count(self, text: str) -> int:
        return len(text.split())

    def encode(self, text: str) -> list[int]:
        return list(range(self.count(text)))

    def decode(self, token_ids: list[int]) -> str:
        return " ".join(f"t{index}" for index in token_ids)


def test_sidecar_chunks_use_injected_local_tokenizer() -> None:
    messages = tuple(
        type(
            "Message",
            (),
            {"session_index": 0, "session_header": "# S", "rendered": text, "role": role},
        )()
        for role, text in (("user", "one two"), ("assistant", "three four"), ("user", "five six"))
    )
    packed = chunks(messages, token_budget=6, tokenizer=TinyTokenizer())
    assert len(packed) == 2
    assert [len(item) for item in packed] == [2, 1]


def test_turn_units_keep_user_assistant_pair_and_do_not_cross_sessions() -> None:
    messages = tuple(
        type("Message", (), {"role": role, "session_index": session})()
        for role, session in (("user", 0), ("assistant", 0), ("user", 0), ("assistant", 1))
    )
    units = turn_units(messages)
    assert [len(unit) for unit in units] == [2, 1, 1]
    assert [message.role for message in units[0]] == ["user", "assistant"]


def test_sidecar_tail_trim_uses_local_encode_decode() -> None:
    tail, full_tokens, trimmed = trim_tail("one two three four", 2, TinyTokenizer())
    assert (tail, full_tokens, trimmed) == ("t2 t3", 4, True)
