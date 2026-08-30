from __future__ import annotations

import json
from types import SimpleNamespace

from memory_sidecar.v3 import compile_evidence
from memory_sidecar.v4 import V4GraphState, classify_v4_question, manager_v4_messages, normalize_v4_claim, parse_v4_manager_response, render_v4_graph_all, render_v4_manager_state, render_v4_numeric_projection, render_v4_query_projection
from utils.store import TrajectoryStore


def _compiled():
    return compile_evidence([
        SimpleNamespace(unit_ordinal=10, session_id="s1", session_date="2023/05/27", role="user", content="I bought a chain for $25 on 2023-05-20 from Bike Shop. Previous Saturday I was there."),
        SimpleNamespace(unit_ordinal=11, session_id="s2", session_date="2023/05/28", role="user", content="I still live in Chicago at home and changed Chicago to Tampa."),
    ])


def _claim(**overrides):
    value = {
        "subject_text": "I",
        "relation": "bought",
        "object_text": "bike chain replacement",
        "evidence_ids": [0],
    }
    value.update(overrides)
    return value


def test_minimal_claim_does_not_require_claim_text_or_hints():
    claims = parse_v4_manager_response(json.dumps({"claims": [_claim()]}), _compiled())
    assert len(claims) == 1
    assert claims[0].claim_text is None
    normalized = normalize_v4_claim(claims[0], _compiled(), ordinal=0)
    assert normalized.parse_status == "normalized"
    assert normalized.attributes["amount"] == 25.0
    assert normalized.time_json["value"] == "2023-05-20"


def test_unknown_relation_is_raw_not_quarantined():
    claims = parse_v4_manager_response(json.dumps({"claims": [_claim(relation="talked about")]}, ensure_ascii=False), _compiled())
    normalized = normalize_v4_claim(claims[0], _compiled(), ordinal=0)
    assert normalized.parse_status == "unknown_relation"
    state = V4GraphState()
    route = state.route(normalized)
    assert route["route_status"] == "raw_unknown_relation"
    assert not state.edges and len(state.raw_claims) == 1


def test_missing_core_field_is_quarantined():
    claims = parse_v4_manager_response(json.dumps({"claims": [_claim(object_text="")] }), _compiled())
    normalized = normalize_v4_claim(claims[0], _compiled(), ordinal=0)
    assert normalized.parse_status == "incomplete"
    state = V4GraphState()
    assert state.route(normalized)["route_status"] == "quarantined"
    assert not state.edges


def test_non_user_evidence_is_quarantined():
    compiled = compile_evidence([SimpleNamespace(
        unit_ordinal=10, session_id="s", session_date="2023/05/27", role="assistant", content="I bought a chain for $25.",
    )])
    claims = parse_v4_manager_response(json.dumps({"claims": [_claim(evidence_ids=[0])]}), compiled)
    normalized = normalize_v4_claim(claims[0], compiled, ordinal=0)
    assert normalized.parse_status == "incomplete"
    assert normalized.model_claim["_invalid_reason"] == "evidence_must_be_user"


def test_invalid_optional_fields_do_not_change_normalization():
    compiled = compile_evidence([SimpleNamespace(
        unit_ordinal=10, session_id="s", session_date="2023/05/27", role="user", content="I bought a chain.",
    )])
    claim = parse_v4_manager_response(json.dumps({"claims": [_claim(
        claim_text="I bought a car for $999 on 2020-01-01.",
        hints={"amount_text": "$999", "time_text": "2020-01-01", "provider_text": "car dealer"},
    )]}), compiled)[0]
    normalized = normalize_v4_claim(claim, compiled, ordinal=0)
    assert normalized.attributes["amount"] is None
    assert normalized.time_json["parse_status"] == "unparsed"
    assert {action["field"] for action in normalized.normalization_actions if action["kind"] == "invalid_optional_field"} == {
        "claim_text", "hints.amount_text", "hints.time_text", "hints.provider_text",
    }


def test_unparsed_time_is_bounded_and_not_canonical():
    text = "I bought a snack. " + ("This is unrelated context. " * 40)
    compiled = compile_evidence([SimpleNamespace(
        unit_ordinal=10, session_id="s", session_date="", role="user", content=text,
    )])
    claim = parse_v4_manager_response(json.dumps({"claims": [_claim(object_text="snack")]}), compiled)[0]
    normalized = normalize_v4_claim(claim, compiled, ordinal=0)
    assert normalized.time_json["parse_status"] == "unparsed"
    assert normalized.time_json["value"] is None
    assert len(normalized.time_json["raw"]) <= 256


def test_provider_hint_selects_nearest_amount_when_sentence_has_multiple_amounts():
    compiled = compile_evidence([SimpleNamespace(
        unit_ordinal=10, session_id="s", session_date="2023/05/27", role="user",
        content="I am buying a $325,000 house, and got pre-approved for $350,000 from Wells Fargo.",
    )])
    claim = _claim(relation="uses", object_text="Wells Fargo", evidence_ids=[0], hints={"provider_text": "Wells Fargo"})
    normalized = normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [claim]}), compiled)[0], compiled, ordinal=0)
    assert normalized.attributes["amount"] == 350000.0


def test_occurrence_key_uses_normalized_fields_and_deduplicates():
    compiled = _compiled()
    first = normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [_claim(hints={"time_text": "2023-05-20"})]}), compiled)[0], compiled, ordinal=0)
    second = normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [_claim(claim_text="I purchased a bike chain replacement on May 20, 2023.", hints={"time_text": "2023-05-20"})]}), compiled)[0], compiled, ordinal=1)
    state = V4GraphState()
    assert state.route(first)["route_status"] == "applied"
    assert state.route(second)["route_status"] == "deduplicated_merged"
    assert len(state.edges) == 1


def test_duplicate_occurrence_merges_late_amount_and_provenance():
    compiled = compile_evidence([
        SimpleNamespace(unit_ordinal=10, session_id="s1", session_date="2023/05/27", role="user", content="I bought a Bell Zephyr helmet from the local bike shop downtown last month."),
        SimpleNamespace(unit_ordinal=11, session_id="s1", session_date="2023/05/27", role="user", content="The Bell Zephyr helmet from the local bike shop downtown last month cost $120."),
    ])
    first_payload = _claim(object_text="Bell Zephyr helmet", hints={"time_text": "last month", "provider_text": "local bike shop downtown"}, evidence_ids=[0])
    second_payload = _claim(object_text="Bell Zephyr helmet", hints={"time_text": "last month", "provider_text": "local bike shop downtown", "amount_text": "$120"}, evidence_ids=[1])
    first = normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [first_payload]}), compiled)[0], compiled, ordinal=0)
    second = normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [second_payload]}), compiled)[0], compiled, ordinal=1)
    state = V4GraphState()
    # The source contains no amount, so the first route creates an incomplete edge.
    assert state.route(first)["route_status"] == "applied"
    route = state.route(second)
    assert route["route_status"] == "deduplicated_merged"
    assert "attributes.amount" in route["merged_fields"]
    assert "attributes.currency" in route["merged_fields"]
    assert state.edges[0]["attributes"]["amount"] == 120.0


def test_occurrence_without_discriminator_is_kept_as_ambiguous():
    compiled = compile_evidence([SimpleNamespace(unit_ordinal=10, session_id="s1", session_date="", role="user", content="I bought a chain.")])
    claims = parse_v4_manager_response(json.dumps({"claims": [_claim()]}), compiled)
    first = normalize_v4_claim(claims[0], compiled, ordinal=0)
    second = normalize_v4_claim(claims[0], compiled, ordinal=1)
    state = V4GraphState()
    assert state.route(first)["route_status"] == "applied_ambiguous_occurrence"
    assert state.route(second)["route_status"] == "applied_ambiguous_occurrence"
    assert len(state.edges) == 2


def test_day_only_occurrence_merges_same_source_but_not_distinct_sources():
    compiled = compile_evidence([
        SimpleNamespace(unit_ordinal=10, session_id="s", session_date="2023/05/27", role="user", content="I bought a chain on 2023-05-20."),
        SimpleNamespace(unit_ordinal=20, session_id="s", session_date="2023/05/27", role="user", content="I bought another chain on 2023-05-20."),
    ])
    first_claim = _claim(object_text="chain", hints={"time_text": "2023-05-20"}, evidence_ids=[0])
    second_claim = _claim(object_text="chain", hints={"time_text": "2023-05-20"}, evidence_ids=[1])
    first = normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [first_claim]}), compiled)[0], compiled, ordinal=0)
    second = normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [second_claim]}), compiled)[0], compiled, ordinal=1)
    state = V4GraphState()
    assert state.route(first)["route_status"] == "applied"
    route = state.route(second)
    assert route["route_status"] == "applied_ambiguous_occurrence"
    assert route["dedupe_rule"] == "ambiguous_day"
    assert len(state.edges) == 2


def test_observed_wake_time_alias_is_canonicalized():
    claim = parse_v4_manager_response(json.dumps({"claims": [_claim(relation="observed wake time", object_text="7:30 am")]}), _compiled())[0]
    normalized = normalize_v4_claim(claim, _compiled(), ordinal=0)
    assert normalized.predicate == "OBSERVED_WAKE_TIME"


def test_uses_alias_is_canonicalized_and_prompt_allows_it():
    compiled = _compiled()
    claim = parse_v4_manager_response(json.dumps({"claims": [_claim(relation="uses", object_text="Scribd app")]}), compiled)[0]
    normalized = normalize_v4_claim(claim, compiled, ordinal=0)
    assert normalized.predicate == "USES"
    assert "uses" in manager_v4_messages(V4GraphState(), compiled)[1]["content"]


def test_count_parser_supports_discrete_entities_and_ignores_range_upper_bound():
    compiled = compile_evidence([SimpleNamespace(
        unit_ordinal=10, session_id="s", session_date="2023/05/30", role="user",
        content="I own 3 plants.",
    ), SimpleNamespace(
        unit_ordinal=11, session_id="s", session_date="2023/05/30", role="user",
        content="I planned a 7-10 day trip.",
    )])
    plant = _claim(relation="observed", object_text="3 plants", evidence_ids=[0])
    trip = _claim(relation="plans", object_text="a 7-10 day trip", evidence_ids=[1])
    plant_normalized = normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [plant]}), compiled)[0], compiled, ordinal=0)
    trip_normalized = normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [trip]}), compiled)[0], compiled, ordinal=1)
    assert plant_normalized.attributes["count"] == 3
    assert trip_normalized.attributes["count"] is None


def test_temporal_projection_keeps_observed_wake_time_without_wake_in_object():
    compiled = compile_evidence([SimpleNamespace(
        unit_ordinal=10, session_id="s", session_date="2023/05/30", role="user", content="I wake at 7:30 am.",
    )])
    claim = _claim(relation="observed wake time", object_text="7:30 am", evidence_ids=[0])
    state = V4GraphState()
    state.route(normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [claim]}), compiled)[0], compiled, ordinal=0))
    context, meta = render_v4_query_projection(state, "What time do I wake up?")
    assert "7:30 am" in context
    assert meta["selected_edge_count"] == 1


def test_count_projection_falls_back_to_occurrences_when_no_typed_count():
    compiled = compile_evidence([SimpleNamespace(
        unit_ordinal=10, session_id="s", session_date="2023/05/30", role="user",
        content="I attended my nephew's graduation ceremony and my friend's graduation ceremony.",
    )])
    state = V4GraphState()
    for ordinal, object_text in enumerate(("nephew graduation ceremony", "friend graduation ceremony")):
        claim = _claim(relation="attended", object_text=object_text, evidence_ids=[0])
        state.route(normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [claim]}), compiled)[0], compiled, ordinal=ordinal))
    context, meta = render_v4_query_projection(state, "How many graduation ceremonies have I attended?")
    assert meta["count_projection_mode"] == "occurrence_fallback"
    assert meta["selected_edge_count"] == 2
    assert context.count("ATTENDED") == 2


def test_temporal_classifier_precedes_generic_count_for_day_difference():
    assert classify_v4_question("How many days passed between two museum visits?") == "temporal"


def test_relative_time_uses_evidence_session_date():
    compiled = _compiled()
    claims = parse_v4_manager_response(json.dumps({"claims": [_claim(hints={"time_text": "previous Saturday"})]}), compiled)
    normalized = normalize_v4_claim(claims[0], compiled, ordinal=0)
    assert normalized.time_json["value"] == "2023-05-20"
    assert normalized.time_json["relative_to"]["reference_date"] == "2023-05-27"


def test_relative_time_parses_week_month_and_last_saturday_intervals():
    compiled = compile_evidence([SimpleNamespace(
        unit_ordinal=10, session_id="s", session_date="2023/05/15", role="user", content="I bought a thing last month, last week, the week before last, and last Saturday.",
    )])
    expectations = {
        "last month": ("2023-04-01", "2023-04-30", "month"),
        "last week": ("2023-05-08", "2023-05-14", "week"),
        "the week before last": ("2023-05-01", "2023-05-07", "week"),
        "last Saturday": ("2023-05-13", None, "day"),
    }
    for ordinal, (expression, expected) in enumerate(expectations.items()):
        claim = parse_v4_manager_response(json.dumps({"claims": [_claim(hints={"time_text": expression})]}), compiled)[0]
        normalized = normalize_v4_claim(claim, compiled, ordinal=ordinal)
        assert (normalized.time_json["value"], normalized.time_json["interval_end"], normalized.time_json["granularity"]) == expected


def test_today_resolves_against_evidence_session_date():
    compiled = compile_evidence([SimpleNamespace(
        unit_ordinal=10, session_id="s", session_date="2023/06/03", role="user",
        content="I visited the museum today.",
    )])
    claim = parse_v4_manager_response(json.dumps({"claims": [_claim(
        relation="attended", object_text="museum", hints={"time_text": "today"},
    )]}), compiled)[0]
    normalized = normalize_v4_claim(claim, compiled, ordinal=0)
    assert normalized.time_json["value"] == "2023-06-03"
    assert normalized.time_json["relative_to"]["reference_date"] == "2023-06-03"


def test_provider_suffix_alias_merges_same_evidence_numeric_occurrence():
    compiled = compile_evidence([SimpleNamespace(
        unit_ordinal=10, session_id="s", session_date="2023/05/15", role="user",
        content="I placed an online order with Thrive Market last month and spent $150.",
    )])
    hints = {"provider_text": "Thrive Market", "amount_text": "$150", "time_text": "last month"}
    first = normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [_claim(object_text="organic and sustainable products", hints=hints)]}), compiled)[0], compiled, ordinal=0)
    second = normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [_claim(object_text="organic and sustainable products from Thrive Market", hints=hints)]}), compiled)[0], compiled, ordinal=1)
    state = V4GraphState()
    assert state.route(first)["route_status"] == "applied"
    assert state.route(second)["route_status"] == "deduplicated_alias"
    assert len(state.edges) == 1


def test_provider_suffix_alias_merges_nearby_units_in_same_session():
    compiled = compile_evidence([
        SimpleNamespace(unit_ordinal=271, session_id="s", session_date="2023/05/26", role="user", content="I placed an order from Thrive Market last week and spent $150."),
        SimpleNamespace(unit_ordinal=281, session_id="s", session_date="2023/05/26", role="user", content="The same organic and sustainable products order was from Thrive Market last week and cost $150."),
    ])
    hints = {"provider_text": "Thrive Market", "amount_text": "$150", "time_text": "last week"}
    first = normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [_claim(object_text="organic and sustainable products", hints=hints, evidence_ids=[0])]}), compiled)[0], compiled, ordinal=0)
    second = normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [_claim(object_text="organic and sustainable products from Thrive Market", hints=hints, evidence_ids=[1])]}), compiled)[0], compiled, ordinal=1)
    state = V4GraphState()
    assert state.route(first)["route_status"] == "applied"
    assert state.route(second)["route_status"] == "deduplicated_alias_merged"
    assert len(state.edges) == 1


def test_functional_unknown_update_is_conflict_but_explicit_replace_supersedes():
    compiled = compile_evidence([
        SimpleNamespace(unit_ordinal=10, session_id="s", session_date="2023/05/27", role="user", content="I live in Chicago at home."),
        SimpleNamespace(unit_ordinal=11, session_id="s", session_date="2023/05/28", role="user", content="I live in Tampa at home."),
        SimpleNamespace(unit_ordinal=12, session_id="s", session_date="2023/05/29", role="user", content="I changed Chicago to Tampa at home."),
    ])
    first_claim = _claim(relation="lives in", object_text="Chicago", hints={"scope_text": "home"}, evidence_ids=[0])
    second_claim = _claim(relation="lives in", object_text="Tampa", hints={"scope_text": "home"}, claim_text="I live in Tampa at home.", evidence_ids=[1])
    first = normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [first_claim]}), compiled)[0], compiled, ordinal=0)
    second = normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [second_claim]}), compiled)[0], compiled, ordinal=1)
    state = V4GraphState()
    assert state.route(first)["route_status"] == "applied"
    assert state.route(second)["route_status"] == "conflict"
    assert all(edge["status"] == "contradicted" for edge in state.edges)

    replacement_claim = _claim(relation="lives in", object_text="Tampa", hints={"scope_text": "home"}, claim_text="I changed Chicago to Tampa at home.", evidence_ids=[2])
    replacement = normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [replacement_claim]}), compiled)[0], compiled, ordinal=2)
    assert replacement.update_intent == "replace"
    assert state.route(replacement)["route_status"] == "superseded"


def test_v4_store_persists_batch_edges_and_is_idempotent(tmp_path):
    store = TrajectoryStore(tmp_path / "trajectory.sqlite3")
    config = {"method": "memory_sidecar_v4", "model": {"model": "fake"}, "config_fingerprint": "fp"}
    store.start_run("v4", config)
    sample_id = store.start_sample("v4", "q", dataset_index=1, question_type="x", config_fingerprint="fp", code_version="test")
    compiled = _compiled()
    claim = parse_v4_manager_response(json.dumps({"claims": [_claim(hints={"time_text": "2023-05-20"})]}), compiled)[0]
    normalized = normalize_v4_claim(claim, compiled, ordinal=0)
    state = V4GraphState()
    route = state.route(normalized)
    claim_row = {
        "claim_id": normalized.claim_id,
        "model_claim": normalized.model_claim,
        "normalized_claim": {"predicate": normalized.predicate, "object": normalized.object},
        "parse_status": normalized.parse_status,
        "route_status": route["route_status"],
        "route_result": route,
        "source_refs": list(normalized.source_refs),
        "normalization_actions": list(normalized.normalization_actions),
    }
    batch_id = store.record_v4_batch(
        sample_id, batch_ordinal=1, input_hash="input-1", source_unit_ordinals=[10], input_text=compiled.text,
        memory_before_json="{}", raw_response='{"claims":[]}', parse_status="ok", claims=[claim_row],
        edges=state.edges, raw_claims=state.raw_claims, quarantine_claims=state.quarantine_claims,
    )
    assert len(store.load_v4_edges(sample_id)) == 1
    assert store.record_v4_batch(
        sample_id, batch_ordinal=1, input_hash="input-1", source_unit_ordinals=[10], input_text=compiled.text,
        memory_before_json="{}", raw_response='{"claims":[]}', parse_status="ok", claims=[claim_row],
        edges=state.edges, raw_claims=state.raw_claims, quarantine_claims=state.quarantine_claims,
    ) == batch_id
    assert store.conn.execute("SELECT count(*) FROM sidecar_v4_batches").fetchone()[0] == 1
    assert store.conn.execute("SELECT count(*) FROM sidecar_v4_edges").fetchone()[0] == 1
    store.record_v4_projection(sample_id, projection_ordinal=1, projection_kind="graph-all", question_signature=None, filter_spec={}, content="graph", input_edge_ids=[state.edges[0]["edge_id"]])
    assert store.conn.execute("SELECT content FROM sidecar_v4_projections WHERE sample_id=?", (sample_id,)).fetchone()[0] == "graph"
    store.close()


def test_graph_all_merges_duplicate_relations_and_inlines_attributes():
    compiled = _compiled()
    payload = {"claims": [
        _claim(hints={"time_text": "2023-05-20", "provider_text": "Bike Shop"}),
        _claim(claim_text="I bought a chain for $25 on 2023-05-20 from Bike Shop.", hints={"time_text": "2023-05-20", "provider_text": "Bike Shop"}),
    ]}
    claims = parse_v4_manager_response(json.dumps(payload), compiled)
    state = V4GraphState()
    for index, claim in enumerate(claims):
        state.route(normalize_v4_claim(claim, compiled, ordinal=index))
    context, meta = render_v4_graph_all(state)
    assert meta["merged_edge_count"] == 1
    assert "PURCHASED" in context and "provider=bike shop" in context
    assert context.count("bike chain replacement") == 1


def test_numeric_projection_emits_auditable_unfiltered_aggregates():
    compiled = compile_evidence([SimpleNamespace(
        unit_ordinal=10, session_id="s", session_date="2023/05/15", role="user", content="I completed 12 courses on Coursera.",
    )])
    claim = parse_v4_manager_response(json.dumps({"claims": [_claim(relation="completed", object_text="12 courses on Coursera", hints={"count_text": "12 courses", "provider_text": "Coursera", "time_text": "last month"})]}), compiled)[0]
    state = V4GraphState()
    state.route(normalize_v4_claim(claim, compiled, ordinal=0))
    context, meta = render_v4_numeric_projection(state)
    assert "SUM_COUNT COMPLETED; provider=coursera; total=12" in context
    assert meta["numeric_edge_count"] == 1
    assert meta["aggregates"][0]["input_edge_ids"] == [state.edges[0]["edge_id"]]


def test_query_projection_ranks_known_purchase_providers_only():
    compiled = compile_evidence([SimpleNamespace(unit_ordinal=10, session_id="s", session_date="2023/05/30", role="user", content="I bought groceries for $120 at Walmart last Saturday, organic products for $150 at Thrive Market last month, and a plan budget for $5000.")])
    claims = [
        _claim(object_text="groceries", hints={"amount_text": "$120", "provider_text": "Walmart", "time_text": "last Saturday"}),
        _claim(object_text="organic products", hints={"amount_text": "$150", "provider_text": "Thrive Market", "time_text": "last month"}),
        _claim(object_text="a plan budget", relation="plans", hints={"amount_text": "$5000"}),
    ]
    state = V4GraphState()
    for ordinal, payload in enumerate(claims):
        claim = parse_v4_manager_response(json.dumps({"claims": [payload]}), compiled)[0]
        state.route(normalize_v4_claim(claim, compiled, ordinal=ordinal))
    question = "Which grocery store did I spend the most money at in the past month?"
    assert classify_v4_question(question) == "provider_amount_rank"
    context, meta = render_v4_query_projection(state, question)
    assert "rank=1; provider=thrive market; total=150.0" in context
    assert "provider=walmart; total=120.0" in context
    assert "5000" not in context
    assert meta["selected_edge_count"] == 2


def test_query_projection_excludes_non_grocery_amounts_for_grocery_question():
    compiled = compile_evidence([SimpleNamespace(unit_ordinal=10, session_id="s", session_date="2023/05/30", role="user", content="I bought groceries for $120 at Walmart last Saturday and brown boots for $130 at Macy's last month.")])
    claims = [
        _claim(object_text="groceries", hints={"amount_text": "$120", "provider_text": "Walmart", "time_text": "last Saturday"}),
        _claim(object_text="brown boots", hints={"amount_text": "$130", "provider_text": "Macy's", "time_text": "last month"}),
    ]
    state = V4GraphState()
    for ordinal, payload in enumerate(claims):
        claim = parse_v4_manager_response(json.dumps({"claims": [payload]}), compiled)[0]
        state.route(normalize_v4_claim(claim, compiled, ordinal=ordinal))
    context, meta = render_v4_query_projection(state, "Which grocery store did I spend the most money at?")
    assert "provider=walmart" in context
    assert "macy's" not in context
    assert meta["selected_edge_count"] == 1


def test_count_in_object_text_is_normalized_and_query_projection_filters_courses():
    compiled = compile_evidence([SimpleNamespace(unit_ordinal=10, session_id="s", session_date="2023/05/30", role="user", content="I completed courses.")])
    state = V4GraphState()
    for ordinal, payload in enumerate([
        _claim(relation="completed", object_text="8 edX courses", evidence_ids=[0]),
        _claim(relation="completed", object_text="12 courses on Coursera", evidence_ids=[0]),
        _claim(relation="completed", object_text="5 days exploring the islands", evidence_ids=[0]),
    ]):
        claim = parse_v4_manager_response(json.dumps({"claims": [payload]}), compiled)[0]
        state.route(normalize_v4_claim(claim, compiled, ordinal=ordinal))
    context, meta = render_v4_query_projection(state, "What is the total number of online courses I've completed?")
    assert "total=20" in context
    assert "5 days" not in context
    assert meta["selected_edge_count"] == 2


def test_temporal_query_projection_filters_unrelated_time_facts():
    compiled = compile_evidence([SimpleNamespace(unit_ordinal=10, session_id="s", session_date="2023/05/30", role="user", content="I wake up at 7:30 and like trendy clothes.")])
    state = V4GraphState()
    for ordinal, payload in enumerate([
        _claim(relation="observed", object_text="waking up at 7:30 am on Saturdays", evidence_ids=[0]),
        _claim(relation="prefers", object_text="trendy clothes", evidence_ids=[0]),
    ]):
        claim = parse_v4_manager_response(json.dumps({"claims": [payload]}), compiled)[0]
        state.route(normalize_v4_claim(claim, compiled, ordinal=ordinal))
    context, meta = render_v4_query_projection(state, "What time do I wake up on Saturday mornings?")
    assert "waking up at 7:30" in context
    assert "trendy clothes" not in context
    assert meta["selected_edge_count"] == 1


def test_question_classifier_routes_total_expenses_to_amount_projection():
    assert classify_v4_question("How much total money have I spent on bike-related expenses since the start of the year?") == "amount_total"


def test_manager_prompt_includes_bounded_existing_graph_reference():
    compiled = _compiled()
    state = V4GraphState(edges=[
        {
            "edge_id": "edge-1", "occurrence_key": "occ-1", "status": "completed",
            "subject": "user", "predicate": "PURCHASED", "object": "bike chain",
            "attributes": {"amount": 25.0, "currency": "USD"}, "source_refs": [],
        },
    ])
    prompt = manager_v4_messages(state, compiled)[1]["content"]
    assert "Existing graph reference" in prompt
    assert '"object":"bike chain"' in prompt
    assert "Current chunk evidence" in prompt


def test_manager_state_caps_edges_and_keeps_truncation_marker():
    state = V4GraphState(edges=[
        {
            "edge_id": f"edge-{index}", "occurrence_key": f"occ-{index}", "status": "completed",
            "subject": "user", "predicate": "PURCHASED", "object": f"item-{index}",
            "attributes": {}, "source_refs": [],
        }
        for index in range(3)
    ])
    rendered = json.loads(render_v4_manager_state(state, max_edges=2, max_raw_claims=0))
    assert len(rendered["relevant_edges"]) + len(rendered["recent_edges"]) == 2
    assert rendered["recent_edge_truncated"] is True
    assert rendered["active_edge_count"] == 3


def test_manager_state_uses_explicit_relevant_and_recent_layers():
    state = V4GraphState(edges=[
        {"edge_id": "edge-old", "occurrence_key": "occ-old", "status": "completed", "subject": "user", "predicate": "PURCHASED", "object": "midnight sky", "attributes": {}, "source_refs": []},
        {"edge_id": "edge-new", "occurrence_key": "occ-new", "status": "completed", "subject": "user", "predicate": "PURCHASED", "object": "other item", "attributes": {}, "source_refs": []},
    ])
    rendered = json.loads(render_v4_manager_state(state, max_edges=2, max_raw_claims=0, current_text="[e0][user] I bought midnight sky."))
    assert [edge["object"] for edge in rendered["relevant_edges"]] == ["midnight sky"]
    assert [edge["object"] for edge in rendered["recent_edges"]] == ["other item"]
    assert "edges" not in rendered and "truncated" not in rendered


def test_manager_state_does_not_use_assistant_text_for_relevance():
    state = V4GraphState(edges=[
        {"edge_id": "edge-1", "occurrence_key": "occ-1", "status": "completed", "subject": "user", "predicate": "PURCHASED", "object": "assistant-only", "attributes": {}, "source_refs": []},
    ])
    rendered = json.loads(render_v4_manager_state(state, max_edges=1, current_text="[e0][assistant] assistant-only"))
    assert rendered["relevant_edges"] == []
    assert rendered["recent_edges"][0]["object"] == "assistant-only"


def test_manager_state_prefers_edges_relevant_to_current_chunk():
    state = V4GraphState(edges=[
        {
            "edge_id": "old-relevant", "occurrence_key": "occ-old", "status": "completed",
            "subject": "user", "predicate": "PURCHASED", "object": "midnight sky",
            "attributes": {"provider": "the whiskey wanderers"}, "source_refs": [],
        },
        *[
            {
                "edge_id": f"new-{index}", "occurrence_key": f"occ-new-{index}", "status": "completed",
                "subject": "user", "predicate": "PLANS", "object": f"unrelated plan {index}",
                "attributes": {}, "source_refs": [],
            }
            for index in range(40)
        ],
    ])
    rendered = json.loads(render_v4_manager_state(state, max_edges=4, current_text="[e0][user] I downloaded the Midnight Sky EP again."))
    assert any(edge["object"] == "midnight sky" for edge in rendered["relevant_edges"])
    assert rendered["selection_mode"] == "user_object_provider_location_relevance_then_recency"
    assert rendered["relevant_edge_count"] >= 1


def test_manager_prompt_does_not_embed_growing_graph_state():
    prompt = manager_v4_messages(
        V4GraphState(edges=[{"edge_id": "old", "object": "must not enter the prompt"}]),
        _compiled(),
    )
    assert "must not enter the prompt" in prompt[1]["content"]
    assert "Do not extract questions" in prompt[1]["content"]


def test_v4_store_persists_deduplicated_edge_field_merge(tmp_path):
    store = TrajectoryStore(tmp_path / "trajectory.sqlite3")
    config = {"method": "memory_sidecar_v4", "model": {"model": "fake"}, "config_fingerprint": "fp"}
    store.start_run("v4", config)
    sample_id = store.start_sample("v4", "q", dataset_index=1, question_type="x", config_fingerprint="fp", code_version="test")
    compiled = compile_evidence([
        SimpleNamespace(unit_ordinal=10, session_id="s", session_date="2023/05/27", role="user", content="I bought a helmet last month."),
        SimpleNamespace(unit_ordinal=11, session_id="s", session_date="2023/05/27", role="user", content="The helmet I bought last month cost $120."),
    ])
    first = normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [_claim(object_text="helmet", hints={"time_text": "last month"}, evidence_ids=[0])]}), compiled)[0], compiled, ordinal=0)
    second = normalize_v4_claim(parse_v4_manager_response(json.dumps({"claims": [_claim(object_text="helmet", hints={"time_text": "last month", "amount_text": "$120"}, evidence_ids=[1])]}), compiled)[0], compiled, ordinal=1)
    state = V4GraphState()
    state.route(first)
    store.record_v4_batch(sample_id, batch_ordinal=1, input_hash="one", source_unit_ordinals=[10], input_text=compiled.text, memory_before_json="{}", raw_response="{}", parse_status="ok", claims=[], edges=state.edges, raw_claims=[], quarantine_claims=[])
    assert state.route(second)["route_status"] == "deduplicated_merged"
    store.record_v4_batch(sample_id, batch_ordinal=2, input_hash="two", source_unit_ordinals=[10], input_text=compiled.text, memory_before_json="{}", raw_response="{}", parse_status="ok", claims=[], edges=state.edges, raw_claims=[], quarantine_claims=[])
    assert store.load_v4_edges(sample_id)[0]["attributes"]["amount"] == 120.0
    store.close()
