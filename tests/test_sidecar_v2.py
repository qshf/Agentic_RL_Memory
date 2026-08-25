from __future__ import annotations

import json
import importlib.util
from types import SimpleNamespace
from pathlib import Path

from memory_sidecar.v2 import (
    V2MemoryState,
    compile_evidence,
    manager_money_repair_messages,
    manager_v2_messages,
    merge_money_repair_events,
    parse_manager_response,
    reconciler_v2_messages,
    resolve_time,
    uncovered_usd_amounts,
)


def _evidence():
    return compile_evidence([SimpleNamespace(
        unit_ordinal=118, session_id="s1", session_date="2023/05/05 (Fri) 06:24",
        role="user", content="I bought the helmet for $120.",
    ), SimpleNamespace(
        unit_ordinal=119, session_id="s1", session_date="2023/05/05 (Fri) 06:24",
        role="user", content="I also use Spotify.",
    )])


def test_v2_compiler_numbers_and_preserves_provenance():
    compiled = _evidence()
    assert "[e0][user]" in compiled.text
    assert compiled.evidence[0].unit_ordinal == 118
    assert len(compiled.evidence[0].content_sha256) == 64


def test_v2_batch_keeps_multiple_events_and_structures_time():
    compiled = _evidence()
    events = parse_manager_response(json.dumps({"events": [
        {"op": "ADD", "record_type": "fact", "key": "helmet.purchase", "semantic_status": "completed",
         "attributes": {"amount": 120, "currency": "USD"}, "time_expression": "last Saturday",
         "time_evidence_id": 0, "evidence_ids": [0]},
        {"op": "ADD", "record_type": "fact", "key": "music.service", "semantic_status": "active",
         "attributes": {"service": "Spotify"}, "time_expression": None, "time_evidence_id": None,
         "evidence_ids": [1]},
    ]}))
    state = V2MemoryState()
    routes = state.route_batch(events, compiled, 1)
    assert [route["route_status"] for route in routes] == ["applied", "applied"]
    assert len(state.current_records) == 2
    assert state.current_records[0]["temporal"]["normalized_date"] == "2023-04-29"


def test_v2_patch_merges_fields_and_rejects_conflicts():
    compiled = _evidence()
    state = V2MemoryState()
    first = {"op": "ADD", "record_type": "fact", "key": "helmet.purchase", "semantic_status": "completed",
             "attributes": {"item": "Bell"}, "time_expression": None, "time_evidence_id": None, "evidence_ids": [0]}
    state.route_batch([first], compiled, 1)
    ref = state.current_records[0]["record_ref"]
    patch = {"op": "PATCH", "record_type": "fact", "key": "helmet.purchase", "semantic_status": "completed",
             "attributes": {"amount": 120}, "time_expression": None, "time_evidence_id": None,
             "evidence_ids": [0], "target_ref": ref}
    assert state.route_batch([patch], compiled, 2)[0]["route_status"] == "applied"
    assert state.current_records[-1]["attributes"] == {"item": "Bell", "amount": 120}
    conflict = dict(patch, target_ref=state.current_records[-1]["record_ref"], attributes={"item": "Other"})
    assert state.route_batch([conflict], compiled, 3)[0]["route_status"] == "rejected_patch_conflict"


def test_v2_patch_can_enrich_legacy_null_amount_but_new_null_amount_is_rejected():
    compiled = _evidence()
    state = V2MemoryState()
    state.records.append({
        "record_ref": "r-legacy", "record_type": "fact", "key": "helmet.purchase",
        "semantic_status": "completed", "attributes": {"item": "Bell", "amount": None},
        "temporal": {}, "source_refs": [], "field_provenance": {}, "lifecycle": "current",
        "prior_record_ref": None, "superseded_by_record_ref": None,
    })
    patch = {"op": "PATCH", "record_type": "fact", "key": "helmet.purchase", "semantic_status": "completed",
             "attributes": {"amount": 120}, "time_expression": None, "time_evidence_id": None,
             "evidence_ids": [0], "target_ref": "r-legacy"}
    assert state.route_batch([patch], compiled, 2)[0]["route_status"] == "applied"
    assert state.current_records[0]["attributes"]["amount"] == 120
    invalid = dict(patch, op="ADD", target_ref=None, attributes={"amount": None})
    rejected = parse_manager_response(json.dumps({"events": [invalid]}))
    assert "attributes.amount must be a finite number" in rejected[0]["_error"]


def test_v2_duplicate_target_is_rejected_as_a_batch():
    compiled = _evidence()
    state = V2MemoryState()
    add = {"op": "ADD", "record_type": "fact", "key": "x", "semantic_status": "active", "attributes": {"v": 1},
           "time_expression": None, "time_evidence_id": None, "evidence_ids": [0]}
    state.route_batch([add], compiled, 1)
    ref = state.current_records[0]["record_ref"]
    patch = dict(add, op="PATCH", target_ref=ref, attributes={"new": 2})
    routes = state.route_batch([patch, patch], compiled, 2)
    assert [route["route_status"] for route in routes] == ["rejected_duplicate_target_in_batch"] * 2
    assert len(state.current_records) == 1


def test_v2_undated_repeated_events_wait_for_reconciliation():
    compiled = _evidence()
    event = {"op": "ADD", "record_type": "event", "key": "social.break", "semantic_status": "completed",
             "attributes": {"duration_days": 7}, "time_expression": None, "time_evidence_id": None, "evidence_ids": [0]}
    state = V2MemoryState()
    state.route_batch([event], compiled, 1)
    state.route_batch([event], compiled, 2)
    assert len(state.current_records) == 2
    assert state.reconcile([{"record_refs": [r["record_ref"] for r in state.current_records]}])[0]["route_status"] == "applied"
    assert len(state.current_records) == 1


def test_v2_reconciler_projection_keeps_refs_but_answer_projection_hides_them():
    compiled = _evidence()
    state = V2MemoryState()
    event = {"op": "ADD", "record_type": "event", "key": "social.break", "semantic_status": "completed",
             "attributes": {"duration_days": 7}, "time_expression": None, "time_evidence_id": None,
             "evidence_ids": [0]}
    state.route_batch([event], compiled, 1)
    ref = state.current_records[0]["record_ref"]

    assert ref not in state.render_for_answer()
    reconciler_prompt = reconciler_v2_messages(state.render_for_reconciler())
    assert ref in reconciler_prompt[1]["content"]


def test_v2_manager_prompt_requires_atomic_monetary_records_and_null_time_pairing():
    prompt = manager_v2_messages('{"records":[]}', _evidence())[1]["content"]
    assert "separate atomic expense record" in prompt
    assert "Never use null for an explicit monetary amount" in prompt
    assert "time_expression is null, time_evidence_id must also be null" in prompt


def test_v2_explicit_dollar_amount_requires_matching_event_and_repair_prompt_names_it():
    compiled = _evidence()
    missing = uncovered_usd_amounts([], compiled)
    assert missing == [{"evidence_id": 0, "amount": "120", "text": "$120"}]
    event = {"op": "ADD", "record_type": "fact", "key": "helmet.purchase", "semantic_status": "completed",
             "attributes": {"item": "helmet", "amount": 120, "currency": "USD"},
             "time_expression": None, "time_evidence_id": None, "evidence_ids": [0]}
    assert uncovered_usd_amounts([event], compiled) == []
    decimal_compiled = compile_evidence([SimpleNamespace(
        unit_ordinal=120, session_id="s1", session_date="2023/05/05 (Fri) 06:24",
        role="user", content="The cable cost $1.50.",
    )])
    assert uncovered_usd_amounts([dict(event, attributes={"amount": 1.5})], decimal_compiled) == []
    repair_prompt = manager_money_repair_messages('{"records":[]}', compiled, '{"events":[]}', missing)
    assert "e0 contains $120" in repair_prompt[1]["content"]
    assert "ONLY the corrective" in repair_prompt[1]["content"]


def test_v2_money_guardrail_ignores_assistant_restatements():
    compiled = compile_evidence([
        SimpleNamespace(unit_ordinal=1, session_id="s", session_date="2023/05/05 (Fri) 00:00",
                        role="user", content="I can redeem a $5 discount."),
        SimpleNamespace(unit_ordinal=2, session_id="s", session_date="2023/05/05 (Fri) 00:00",
                        role="assistant", content="Your $5 discount is available."),
        SimpleNamespace(unit_ordinal=3, session_id="s", session_date="2023/05/05 (Fri) 00:00",
                        role="assistant", content="The table lists a $5 discount."),
    ])
    assert uncovered_usd_amounts([], compiled) == [
        {"evidence_id": 0, "amount": "5", "text": "$5"},
    ]


def test_v2_money_repair_merges_incrementally_without_dropping_first_events():
    initial = [
        {"op": "ADD", "record_type": "fact", "key": "store.points", "semantic_status": "active",
         "attributes": {"store": "SmartMart"}, "time_expression": None, "time_evidence_id": None,
         "evidence_ids": [0]},
        {"op": "PATCH", "record_type": "fact", "key": "helmet.purchase", "semantic_status": "completed",
         "attributes": {"item": "helmet", "location": "downtown"}, "time_expression": None, "time_evidence_id": None,
         "evidence_ids": [0], "target_ref": "r-2-0"},
    ]
    repair = [
        {"op": "PATCH", "record_type": "fact", "key": "helmet.purchase", "semantic_status": "completed",
         "attributes": {"amount": 120, "currency": "USD"}, "time_expression": None,
         "time_evidence_id": None, "evidence_ids": [0], "target_ref": "r-2-0"},
    ]
    merged = merge_money_repair_events(initial, repair)
    assert len(merged) == 2
    assert merged[0] == initial[0]
    assert merged[1]["attributes"] == {
        "item": "helmet", "location": "downtown", "amount": 120, "currency": "USD",
    }


def test_v2_money_repair_normalizes_targetless_replace_only_for_prior_add():
    initial = [{
        "op": "ADD", "record_type": "fact", "key": "loyalty_smartmart", "semantic_status": "active",
        "attributes": {"program": "SmartMart", "current_points": 500},
        "time_expression": None, "time_evidence_id": None, "evidence_ids": [40],
    }]
    targetless_replace = {"_error": "ValueError: REPLACE requires target_ref", "_raw": {
        "op": "REPLACE", "record_type": "fact", "key": "loyalty_smartmart", "semantic_status": "active",
        "attributes": {"program": "SmartMart", "amount": 5, "currency": "USD"},
        "time_expression": None, "time_evidence_id": None, "evidence_ids": [40],
    }}
    merged = merge_money_repair_events(initial, [targetless_replace])
    assert merged == [{
        "op": "ADD", "record_type": "fact", "key": "loyalty_smartmart", "semantic_status": "active",
        "attributes": {"program": "SmartMart", "current_points": 500, "amount": 5, "currency": "USD"},
        "time_expression": None, "time_evidence_id": None, "evidence_ids": [40],
    }]


def test_v2_time_resolver_keeps_unknown_expression_unresolved():
    assert resolve_time("sometime during spring", "2023/05/05 (Fri) 06:24") is None


def test_v2_runner_defaults_to_larger_turn_preserving_chunks():
    path = Path(__file__).parents[1] / "scripts" / "run_memory_sidecar_strong.py"
    spec = importlib.util.spec_from_file_location("run_memory_sidecar_strong", path)
    assert spec and spec.loader
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    args = SimpleNamespace(protocol="v2", chunk_budget_tokens=None, manager_max_tokens=None)
    runner.apply_protocol_defaults(args)
    assert (args.chunk_budget_tokens, args.manager_max_tokens) == (8192, 4096)
