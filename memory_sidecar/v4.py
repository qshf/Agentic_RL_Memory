"""Memory Sidecar V4 core: minimal claims, normalization, and edge routing.

The manager emits only a small fact atom.  This module owns all identifiers,
normalization, time parsing, unknown handling, and deterministic state changes.
It is deliberately independent of the database and model client so the contract
can be tested before the V4 runner is introduced.
"""
from __future__ import annotations

import calendar
import json
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

from utils.config import sha256_text

from .protocol import answer_messages
from .v3 import CompiledEvidence


_SPACE = re.compile(r"\s+")
_RELATION_ALIASES = {
    "bought": "PURCHASED",
    "purchase": "PURCHASED",
    "purchased": "PURCHASED",
    "went to": "ATTENDED",
    "attended": "ATTENDED",
    "completed": "COMPLETED",
    "observed": "OBSERVED",
    "target": "TARGET",
    "prefers": "PREFERS",
    "likes": "PREFERS",
    "plans": "PLANS",
    "planned": "PLANS",
    "lives in": "LOCATED_IN",
    "located in": "LOCATED_IN",
    "mentions": "MENTIONS",
    "plans to": "PLANS",
    "plans_to": "PLANS",
    "is planning to": "PLANS",
    "is attending": "ATTENDED",
    "is_attending": "ATTENDED",
    "has completed": "COMPLETED",
    "has_completed": "COMPLETED",
    "completed courses": "COMPLETED",
    "finished": "COMPLETED",
    "observed wake time": "OBSERVED_WAKE_TIME",
    "observed_wake_time": "OBSERVED_WAKE_TIME",
    "wake time observed": "OBSERVED_WAKE_TIME",
    "uses": "USES",
    "use": "USES",
    "downloaded": "DOWNLOADED",
    "download": "DOWNLOADED",
}
_OCCURRENCE_PREDICATES = frozenset({"PURCHASED", "DOWNLOADED", "ATTENDED", "COMPLETED", "OBSERVED", "OBSERVED_WAKE_TIME", "MENTIONS", "USES"})
_NUMERIC_PREDICATES = frozenset({"PURCHASED", "ATTENDED", "COMPLETED", "OBSERVED", "OBSERVED_WAKE_TIME"})
_FUNCTIONAL_PREDICATES = frozenset({"TARGET", "PREFERS", "PLANS", "LOCATED_IN"})
_CATEGORY_HINTS = {
    "grocery": frozenset({"grocery", "groceries", "chicken", "beef", "produce", "organic", "food", "meal", "snack", "pantry", "dairy", "vegetable", "fruit"}),
}
_QUERY_STOPWORDS = frozenset({
    "what", "which", "where", "when", "how", "many", "much", "did", "do", "i", "my", "the",
    "a", "an", "of", "to", "in", "on", "at", "for", "from", "and", "or", "is", "are", "was",
    "were", "have", "has", "had", "been", "be", "between", "past", "last", "total", "number",
    "most", "least", "highest", "lowest", "money", "spent", "days", "time", "long", "online",
})
_UPDATE_PHRASES = (
    re.compile(r"\bchanged\b.+\bto\b", re.I),
    re.compile(r"\bupdated\s+to\b", re.I),
    re.compile(r"\bnow\s+lives\s+in\b", re.I),
    re.compile(r"\bno\s+longer\b.+\binstead\b", re.I),
    re.compile(r"\bcorrected\b.+\bto\b", re.I),
)
_AMOUNT = re.compile(r"(?P<symbol>[$€£])\s*(?P<amount>\d+(?:,\d{3})*(?:\.\d{1,2})?)")
# Keep this deliberately limited to discrete-count nouns.  In particular, do
# not match the upper bound of a range such as "7-10 days".
_COUNT = re.compile(
    r"(?<![\d-])\b(?P<count>\d+)\s+(?:(?:[A-Za-z]+)\s+)?"
    r"(?:courses?|coins?|items?|days?|plants?|albums?|books?|"
    r"graduations?|events?|trips?|nights?)\b",
    re.I,
)
_MONTH_DATE = re.compile(r"\b(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})\b")
_ISO_DATE = re.compile(r"\b(?P<year>\d{4})[/-](?P<month>\d{1,2})[/-](?P<day>\d{1,2})\b")


@dataclass(frozen=True)
class V4Claim:
    subject_text: str
    relation: str
    object_text: str
    evidence_ids: tuple[int, ...]
    claim_text: str | None = None
    hints: dict[str, str | None] = field(default_factory=dict)
    model_claim: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedV4Claim:
    claim_id: str
    parse_status: str
    subject: str | None
    predicate: str | None
    object: str | None
    object_type: str | None
    attributes: dict[str, Any]
    time_json: dict[str, Any]
    scope_json: dict[str, Any]
    source_refs: tuple[dict[str, Any], ...]
    raw_text: str
    update_intent: str
    normalization_actions: tuple[dict[str, Any], ...]
    model_claim: dict[str, Any]


def _canonical_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().lower()
    return _SPACE.sub(" ", value)


def _json_object(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^\s*```(?:json)?\s*", "", text.strip(), flags=re.I)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("manager response must be a JSON object")
    return value


def parse_v4_manager_response(text: str, compiled: CompiledEvidence) -> list[V4Claim]:
    """Parse the minimal manager protocol; malformed items remain auditable."""
    obj = _json_object(text)
    claims = obj.get("claims", obj.get("events", []))
    if not isinstance(claims, list):
        raise ValueError("claims must be an array")
    result: list[V4Claim] = []
    for ordinal, raw in enumerate(claims):
        if not isinstance(raw, Mapping):
            result.append(_incomplete_claim({"_raw": raw}, f"item-{ordinal}"))
            continue
        item = dict(raw)
        subject = item.get("subject_text", item.get("subject"))
        relation = item.get("relation", item.get("predicate"))
        object_text = item.get("object_text", item.get("object"))
        evidence_ids = item.get("evidence_ids")
        if not all(isinstance(value, str) and value.strip() for value in (subject, relation, object_text)):
            result.append(_incomplete_claim(item, f"item-{ordinal}"))
            continue
        if not isinstance(evidence_ids, list) or not evidence_ids or any(
            not isinstance(value, int) or isinstance(value, bool) or value not in compiled.evidence
            or compiled.evidence[value].role != "user"
            for value in evidence_ids
        ):
            result.append(_incomplete_claim({**item, "_invalid_reason": "evidence_must_be_user"}, f"item-{ordinal}"))
            continue
        hints = item.get("hints", {})
        if not isinstance(hints, Mapping):
            hints = {}
        evidence_text = "\n".join(compiled.evidence[value].content for value in evidence_ids)
        invalid_optional: list[str] = []
        valid_hints: dict[str, str | None] = {}
        for key, value in hints.items():
            if not isinstance(value, str) or not value.strip():
                continue
            if value.casefold() not in evidence_text.casefold():
                invalid_optional.append(f"hints.{key}")
                continue
            valid_hints[str(key)] = value
        claim_text = item.get("claim_text")
        if isinstance(claim_text, str) and claim_text.strip() and claim_text.casefold() not in evidence_text.casefold():
            invalid_optional.append("claim_text")
            claim_text = None
        model_claim = dict(item)
        if invalid_optional:
            model_claim["_invalid_optional_fields"] = invalid_optional
        result.append(V4Claim(
            subject_text=str(subject), relation=str(relation), object_text=str(object_text),
            evidence_ids=tuple(evidence_ids), claim_text=claim_text if isinstance(claim_text, str) and claim_text.strip() else None,
            hints=valid_hints, model_claim=model_claim,
        ))
    return result


def _incomplete_claim(raw: dict[str, Any], ordinal: str) -> V4Claim:
    """Keep malformed input in the same collection; normalization quarantines it."""
    return V4Claim("", "", "", (), model_claim={**raw, "_ordinal": ordinal})


def normalize_v4_claim(
    claim: V4Claim, compiled: CompiledEvidence, *, ordinal: int, batch_ordinal: int | None = None,
) -> NormalizedV4Claim:
    """Normalize one claim without inventing facts or silently dropping failures."""
    actions: list[dict[str, Any]] = []
    refs = tuple(compiled.evidence[evidence_id].source_ref() for evidence_id in claim.evidence_ids if evidence_id in compiled.evidence)
    evidence_contents = [compiled.evidence[evidence_id].content for evidence_id in claim.evidence_ids if evidence_id in compiled.evidence and compiled.evidence[evidence_id].role == "user"]
    # Optional claim_text is audit-only. Normalization must always inspect the
    # complete cited user evidence so a model cannot add or hide typed values.
    raw_text = "\n".join(evidence_contents)
    claim_id = sha256_text(json.dumps({"batch_ordinal": batch_ordinal, "ordinal": ordinal, "claim": claim.model_claim}, ensure_ascii=False, sort_keys=True))[:24]
    if not claim.subject_text or not claim.relation or not claim.object_text or not refs:
        return NormalizedV4Claim(claim_id, "incomplete", None, None, None, None, {}, _unparsed_time(None), {}, refs, raw_text, "unknown", tuple(actions), claim.model_claim)

    subject = _canonical_text(claim.subject_text)
    object_text = _canonical_text(claim.object_text)
    relation = _canonical_text(claim.relation)
    predicate = _RELATION_ALIASES.get(relation)
    if predicate is None:
        actions.append({"kind": "unknown_relation", "input": claim.relation})
        return NormalizedV4Claim(claim_id, "unknown_relation", subject, None, object_text, "unknown_entity", {}, _unparsed_time(None), {}, refs, raw_text, "unknown", tuple(actions), claim.model_claim)
    if relation != predicate.lower():
        actions.append({"kind": "relation_alias", "input": claim.relation, "output": predicate})

    source_text = _claim_attribute_source(claim.object_text, claim.claim_text, raw_text)
    attributes, attribute_actions = _parse_attributes(source_text, claim.hints)
    actions.extend(attribute_actions)
    time_json, time_actions = _parse_time(claim.hints.get("time_text") or raw_text, claim.evidence_ids, compiled)
    actions.extend(time_actions)
    for field in claim.model_claim.get("_invalid_optional_fields", []):
        actions.append({"kind": "invalid_optional_field", "field": field})
    scope_text = claim.hints.get("scope_text")
    scope_json = {"value": _canonical_text(scope_text), "parse_status": "ok"} if scope_text else {"value": None, "parse_status": "unknown"}
    if scope_text is None:
        actions.append({"kind": "scope_unknown"})
    update_intent = _update_intent(raw_text)
    object_type = "entity" if predicate == "LOCATED_IN" else "unknown_entity"
    return NormalizedV4Claim(
        claim_id, "normalized", subject, predicate, object_text, object_type, attributes, time_json, scope_json,
        refs, raw_text, update_intent, tuple(actions), claim.model_claim,
    )


def _claim_attribute_source(object_text: str, claim_text: str | None, raw_text: str) -> str:
    """Choose the smallest evidence span likely belonging to this claim.

    A chunk can contain several facts and every claim may cite the same unit.
    Scanning the whole unit makes the first amount/count leak into unrelated
    claims. A validated claim_text is exact evidence; otherwise select the
    sentence with the greatest lexical overlap with object_text. Falling back
    to raw_text preserves recall when the model paraphrases the object.
    """
    if claim_text:
        return claim_text
    object_tokens = {
        token for token in re.findall(r"[a-z0-9]+", _canonical_text(object_text))
        if len(token) >= 3
    }
    if not object_tokens:
        return raw_text
    sentences = [part.strip() for part in re.split(r"(?:[.!?]\s+|\n+)", raw_text) if part.strip()]
    if not sentences:
        return raw_text
    scored = []
    for index, sentence in enumerate(sentences):
        tokens = set(re.findall(r"[a-z0-9]+", _canonical_text(sentence)))
        scored.append((len(object_tokens & tokens), -index, sentence))
    best_score, _, best_sentence = max(scored)
    return best_sentence if best_score else raw_text


def _parse_attributes(source_text: str, hints: Mapping[str, str | None]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    actions: list[dict[str, Any]] = []
    amount_text = hints.get("amount_text")
    match = _AMOUNT.search(amount_text or source_text)
    if not amount_text and hints.get("provider_text"):
        # A sentence may contain both an item price and a provider-specific
        # amount (e.g. house price vs. mortgage pre-approval). Associate the
        # amount closest to the cited provider instead of taking the first one.
        provider_match = re.search(re.escape(str(hints["provider_text"])), source_text, flags=re.I)
        candidates = list(_AMOUNT.finditer(source_text))
        if provider_match and candidates:
            match = min(candidates, key=lambda candidate: abs(candidate.start() - provider_match.start()))
    attributes: dict[str, Any] = {"amount": None, "currency": None, "count": None, "provider": None, "location": None}
    if match:
        attributes["amount"] = float(match.group("amount").replace(",", ""))
        attributes["currency"] = {"$": "USD", "€": "EUR", "£": "GBP"}[match.group("symbol")]
        actions.append({"kind": "amount_parse", "input": match.group(0), "output": attributes["amount"]})
    elif amount_text:
        attributes["amount_raw"] = amount_text
        actions.append({"kind": "amount_unparsed", "input": amount_text})
    count_match = _COUNT.search(hints.get("count_text") or source_text)
    if count_match:
        attributes["count"] = int(count_match.group("count"))
        actions.append({"kind": "count_parse", "output": attributes["count"]})
    for field, output in (("provider_text", "provider"), ("location_text", "location")):
        value = hints.get(field)
        if value:
            attributes[output] = _canonical_text(value)
            actions.append({"kind": f"{output}_hint", "input": value, "output": attributes[output]})
    return attributes, actions


def _parse_time(text: str | None, evidence_ids: Sequence[int], compiled: CompiledEvidence) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not text:
        return _unparsed_time(None), []
    session_dates = {compiled.evidence[evidence_id].session_date for evidence_id in evidence_ids if evidence_id in compiled.evidence}
    if len(session_dates) > 1:
        return _unparsed_time(text, status="relative_time_ambiguous"), [{"kind": "relative_time_ambiguous"}]
    for pattern, parser in ((_ISO_DATE, _parse_iso), (_MONTH_DATE, _parse_month)):
        match = pattern.search(text)
        if match:
            parsed = parser(match)
            if parsed:
                return _time_day(parsed), [{"kind": "time_parse", "input": match.group(0), "output": parsed.isoformat()}]
    reference = _parse_session_date(next(iter(session_dates), ""))
    if reference:
        relative = _parse_relative_time(text.lower(), reference)
        if relative is not None:
            return relative
    return _unparsed_time(text), [{"kind": "time_unparsed", "input": text}]


def _parse_relative_time(text: str, reference: date) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Resolve a small, explicit set of relative dates against evidence time."""
    metadata = {"reference_date": reference.isoformat()}
    if re.search(r"\btoday\b", text):
        return _time_day(reference, relative_to={**metadata, "expression": "today"}), [{"kind": "relative_time_parse", "output": reference.isoformat()}]
    if re.search(r"\byesterday\b", text):
        parsed = reference - timedelta(days=1)
        return _time_day(parsed, relative_to={**metadata, "expression": "yesterday"}), [{"kind": "relative_time_parse", "output": parsed.isoformat()}]
    if re.search(r"\btomorrow\b", text):
        parsed = reference + timedelta(days=1)
        return _time_day(parsed, relative_to={**metadata, "expression": "tomorrow"}), [{"kind": "relative_time_parse", "output": parsed.isoformat()}]
    if "the week before last" in text:
        current_week_start = reference - timedelta(days=reference.weekday())
        end = current_week_start - timedelta(days=8)
        start = end - timedelta(days=6)
        return _time_interval(start, end, "week", relative_to={**metadata, "expression": "the week before last"}), [{"kind": "relative_time_parse", "output": [start.isoformat(), end.isoformat()]}]
    if "last week" in text:
        current_week_start = reference - timedelta(days=reference.weekday())
        end = current_week_start - timedelta(days=1)
        start = end - timedelta(days=6)
        return _time_interval(start, end, "week", relative_to={**metadata, "expression": "last week"}), [{"kind": "relative_time_parse", "output": [start.isoformat(), end.isoformat()]}]
    if "last month" in text:
        end = date(reference.year, reference.month, 1) - timedelta(days=1)
        start = date(end.year, end.month, 1)
        return _time_interval(start, end, "month", relative_to={**metadata, "expression": "last month"}), [{"kind": "relative_time_parse", "output": [start.isoformat(), end.isoformat()]}]
    if "previous saturday" in text or "last saturday" in text:
        delta = (reference.weekday() - calendar.SATURDAY) % 7 or 7
        parsed = reference - timedelta(days=delta)
        expression = "previous Saturday" if "previous saturday" in text else "last Saturday"
        return _time_day(parsed, relative_to={**metadata, "expression": expression}), [{"kind": "relative_time_parse", "output": parsed.isoformat()}]
    return None


def _parse_iso(match: re.Match[str]) -> date | None:
    try:
        return date(int(match["year"]), int(match["month"]), int(match["day"]))
    except ValueError:
        return None


def _parse_month(match: re.Match[str]) -> date | None:
    try:
        return datetime.strptime(match.group(0), "%B %d, %Y").date()
    except ValueError:
        try:
            return datetime.strptime(match.group(0), "%b %d, %Y").date()
        except ValueError:
            return None


def _parse_session_date(value: str) -> date | None:
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except ValueError:
            pass
    return None


def _time_day(value: date, *, relative_to: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"value": value.isoformat(), "granularity": "day", "timezone": None, "interval_end": None, "recurrence": None, "relative_to": relative_to, "parse_status": "ok"}


def _time_interval(start: date, end: date, granularity: str, *, relative_to: dict[str, Any]) -> dict[str, Any]:
    return {"value": start.isoformat(), "granularity": granularity, "timezone": None, "interval_end": end.isoformat(), "recurrence": None, "relative_to": relative_to, "parse_status": "ok"}


def _unparsed_time(raw: str | None, *, status: str = "unparsed") -> dict[str, Any]:
    # An unparsed value is not a canonical time and must never participate in
    # occurrence identity or be rendered as the full evidence/question text.
    # Keep only a bounded audit hint for diagnosis.
    raw_hint = _SPACE.sub(" ", str(raw)).strip()[:256] if raw else None
    return {"value": None, "raw": raw_hint, "granularity": "unknown", "timezone": None, "interval_end": None, "recurrence": None, "relative_to": None, "parse_status": status}


def _update_intent(raw_text: str) -> str:
    lowered = _canonical_text(raw_text)
    for phrase in _UPDATE_PHRASES:
        if phrase.search(lowered):
            return "replace"
    return "unknown"


@dataclass
class V4GraphState:
    """Append-only edge store with conservative occurrence and snapshot routing."""

    edges: list[dict[str, Any]] = field(default_factory=list)
    raw_claims: list[dict[str, Any]] = field(default_factory=list)
    quarantine_claims: list[dict[str, Any]] = field(default_factory=list)

    def copy(self) -> "V4GraphState":
        return V4GraphState(deepcopy(self.edges), deepcopy(self.raw_claims), deepcopy(self.quarantine_claims))

    def route(self, claim: NormalizedV4Claim) -> dict[str, Any]:
        if claim.parse_status == "incomplete":
            self.quarantine_claims.append(_claim_audit(claim))
            return {"route_status": "quarantined", "claim_id": claim.claim_id}
        if claim.parse_status == "unknown_relation":
            self.raw_claims.append(_claim_audit(claim))
            return {"route_status": "raw_unknown_relation", "claim_id": claim.claim_id}
        assert claim.predicate and claim.subject and claim.object
        if claim.predicate in _OCCURRENCE_PREDICATES:
            return self._route_occurrence(claim)
        return self._route_functional(claim)

    def _route_occurrence(self, claim: NormalizedV4Claim) -> dict[str, Any]:
        identity = _occurrence_identity(claim)
        ambiguous_day = False
        if identity is not None:
            for edge in self.edges:
                if edge.get("occurrence_key") == identity and edge.get("status") != "superseded":
                    if _is_ambiguous_day_match(edge, claim):
                        ambiguous_day = True
                        continue
                    changed, merged_fields = _merge_duplicate_edge(edge, claim)
                    result = "deduplicated_merged" if changed else "deduplicated"
                    return {
                        "route_status": result,
                        "claim_id": claim.claim_id,
                        "edge_id": edge["edge_id"],
                        "dedupe_rule": "exact_key",
                        "merged_fields": merged_fields,
                    }
        for edge in self.edges:
            if edge.get("status") != "superseded" and _same_provider_suffix_occurrence(edge, claim):
                changed, merged_fields = _merge_duplicate_edge(edge, claim)
                return {
                    "route_status": "deduplicated_alias_merged" if changed else "deduplicated_alias",
                    "claim_id": claim.claim_id,
                    "edge_id": edge["edge_id"],
                    "dedupe_rule": "provider_suffix_same_source",
                    "merged_fields": merged_fields,
                }
        edge = _edge_from_claim(claim, occurrence_key=None if ambiguous_day else identity, status="completed" if claim.predicate in {"PURCHASED", "ATTENDED", "COMPLETED"} else "observed")
        self.edges.append(edge)
        return {
            "route_status": "applied_ambiguous_occurrence" if identity is None or ambiguous_day else "applied",
            "claim_id": claim.claim_id,
            "edge_id": edge["edge_id"],
            "dedupe_rule": "ambiguous_day" if ambiguous_day else None,
        }

    def _route_functional(self, claim: NormalizedV4Claim) -> dict[str, Any]:
        scope = claim.scope_json.get("value")
        edge = _edge_from_claim(claim, occurrence_key=None, status="active")
        active = [existing for existing in self.edges if existing.get("status") in {"active", "contradicted"} and existing.get("functional_key") == _functional_key(claim)] if scope else []
        if active and claim.update_intent == "replace":
            for existing in active:
                existing["status"] = "superseded"
                existing["superseded_by"] = edge["edge_id"]
            self.edges.append(edge)
            return {"route_status": "superseded", "claim_id": claim.claim_id, "edge_id": edge["edge_id"]}
        if active and any(existing["object"] != edge["object"] for existing in active):
            edge["status"] = "contradicted"
            for existing in active:
                existing["status"] = "contradicted"
            self.edges.append(edge)
            return {"route_status": "conflict", "claim_id": claim.claim_id, "edge_id": edge["edge_id"]}
        self.edges.append(edge)
        return {"route_status": "applied", "claim_id": claim.claim_id, "edge_id": edge["edge_id"]}


def merge_v4_edges(edges: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Merge duplicate canonical nodes/relations for the all-graph projection.

    Occurrence identity is preferred when present; functional edges use their
    functional key plus object. Provenance and normalization actions are unioned
    in stable order. This does not merge two different occurrences merely because
    their text happens to look alike.
    """
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for original in edges:
        edge = deepcopy(dict(original))
        if edge.get("occurrence_key"):
            key = ("occurrence", edge["occurrence_key"])
        elif edge.get("functional_key"):
            key = ("functional", edge["functional_key"], edge.get("object"))
        else:
            key = ("edge", edge.get("subject"), edge.get("predicate"), edge.get("object"), json.dumps(edge.get("time_json", {}), sort_keys=True), json.dumps(edge.get("scope_json", {}), sort_keys=True))
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = edge
            continue
        refs = existing.setdefault("source_refs", [])
        for ref in edge.get("source_refs", []):
            if ref not in refs:
                refs.append(ref)
        actions = existing.setdefault("normalization_actions", [])
        for action in edge.get("normalization_actions", []):
            if action not in actions:
                actions.append(action)
        if existing.get("status") != edge.get("status") and edge.get("status") == "contradicted":
            existing["status"] = "contradicted"
    return sorted(grouped.values(), key=lambda edge: (
        str(edge.get("time_json", {}).get("value") or "9999"), str(edge.get("predicate") or ""),
        str(edge.get("subject") or ""), str(edge.get("object") or ""), str(edge.get("edge_id") or ""),
    ))


def render_v4_graph_all(state_or_edges: V4GraphState | Sequence[Mapping[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Render all current graph relations as stable, compact answer context."""
    if isinstance(state_or_edges, V4GraphState):
        edges = state_or_edges.edges
        raw_claims = state_or_edges.raw_claims
    else:
        edges = list(state_or_edges)
        raw_claims = []
    merged = merge_v4_edges([edge for edge in edges if edge.get("status") != "superseded"])
    lines = ["[V4 GRAPH MEMORY]"]
    for edge in merged:
        attrs = edge.get("attributes") or {}
        details: list[str] = []
        if attrs.get("amount") is not None:
            details.append(f"amount={attrs['amount']} {attrs.get('currency') or ''}".strip())
        if attrs.get("count") is not None:
            details.append(f"count={attrs['count']}")
        if attrs.get("provider"):
            details.append(f"provider={attrs['provider']}")
        if attrs.get("location"):
            details.append(f"location={attrs['location']}")
        time_json = edge.get("time_json") or {}
        if time_json.get("value"):
            time_value = str(time_json["value"])
            if time_json.get("interval_end"):
                time_value += f"..{time_json['interval_end']}"
            details.append(f"time={time_value}")
        scope = (edge.get("scope_json") or {}).get("value")
        if scope:
            details.append(f"scope={scope}")
        if edge.get("status") == "contradicted":
            details.append("status=contradicted")
        details.append("sources=" + ",".join(f"u{ref.get('unit_ordinal')}" for ref in edge.get("source_refs", [])))
        lines.append(f"- {edge.get('subject')} --{edge.get('predicate')}--> {edge.get('object')}" + ("; " + "; ".join(details) if details else ""))
    merged_raw: dict[tuple[str, str, str], dict[str, Any]] = {}
    for claim in raw_claims:
        model_claim = claim.get("model_claim") if isinstance(claim.get("model_claim"), Mapping) else {}
        subject = _canonical_text(str(model_claim.get("subject_text") or model_claim.get("subject") or ""))
        relation = _canonical_text(str(model_claim.get("relation") or model_claim.get("predicate") or "unknown_relation"))
        object_text = _canonical_text(str(model_claim.get("object_text") or model_claim.get("object") or ""))
        key = (subject, relation, object_text)
        existing = merged_raw.get(key)
        if existing is None:
            existing = {"subject": subject, "relation": relation, "object": object_text, "source_refs": []}
            merged_raw[key] = existing
        for ref in claim.get("source_refs", []):
            if ref not in existing["source_refs"]:
                existing["source_refs"].append(ref)
    for claim in sorted(merged_raw.values(), key=lambda item: (item["subject"], item["relation"], item["object"])):
        source_ids = ",".join(f"u{ref.get('unit_ordinal')}" for ref in claim["source_refs"])
        suffix = f"; sources={source_ids}" if source_ids else ""
        lines.append(f"- [RAW RELATION] {claim['subject']} --{claim['relation']}--> {claim['object']}{suffix}")
    nodes = {edge.get("subject") for edge in merged} | {edge.get("object") for edge in merged}
    nodes |= {claim["subject"] for claim in merged_raw.values()} | {claim["object"] for claim in merged_raw.values()}
    return "\n".join(lines), {"merged_edge_count": len(merged), "raw_claim_count": len(merged_raw), "raw_claim_input_count": len(raw_claims), "node_count": len(nodes)}


def render_v4_numeric_projection(
    state_or_edges: V4GraphState | Sequence[Mapping[str, Any]],
    *,
    predicates: set[str] | frozenset[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Render deterministic numeric occurrences and unfiltered aggregates.

    This is intentionally a separate projection from graph-all. It does not parse
    the question or hide any matching edge; callers can run it as an explicit
    experimental arm and retain every aggregate's input edge IDs for audit.
    """
    edges = state_or_edges.edges if isinstance(state_or_edges, V4GraphState) else list(state_or_edges)
    allowed_predicates = predicates or _NUMERIC_PREDICATES
    numeric = [
        edge for edge in merge_v4_edges([edge for edge in edges if edge.get("status") != "superseded"])
        if edge.get("predicate") in allowed_predicates
        and ((edge.get("attributes") or {}).get("amount") is not None or (edge.get("attributes") or {}).get("count") is not None)
    ]
    lines = ["[V4 NUMERIC FACTS]"]
    amount_groups: dict[tuple[str, str, str | None], list[dict[str, Any]]] = {}
    count_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for edge in numeric:
        attrs = edge.get("attributes") or {}
        provider = str(attrs.get("provider") or "(unknown)")
        time_json = edge.get("time_json") or {}
        time_value = time_json.get("value") or "unknown"
        interval_end = time_json.get("interval_end")
        time_label = f"{time_value}..{interval_end}" if interval_end else str(time_value)
        details = [f"edge={edge.get('edge_id')}", f"provider={provider}", f"time={time_label}"]
        if attrs.get("amount") is not None:
            currency = attrs.get("currency")
            details.append(f"amount={attrs['amount']} {currency or ''}".strip())
            amount_groups.setdefault((str(edge.get("predicate")), provider, currency), []).append(edge)
        if attrs.get("count") is not None:
            details.append(f"count={attrs['count']}")
            count_groups.setdefault((str(edge.get("predicate")), provider), []).append(edge)
        lines.append(f"- {edge.get('subject')} --{edge.get('predicate')}--> {edge.get('object')}; " + "; ".join(details))
    lines.append("[UNFILTERED NUMERIC AGGREGATES]")
    aggregate_rows: list[dict[str, Any]] = []
    for (predicate, provider, currency), group in sorted(amount_groups.items()):
        total = sum(float((edge.get("attributes") or {})["amount"]) for edge in group)
        ids = [str(edge["edge_id"]) for edge in group]
        lines.append(f"- SUM {predicate}; provider={provider}; currency={currency or 'unknown'}; total={total}; input_edges={','.join(ids)}")
        aggregate_rows.append({"kind": "amount_sum", "predicate": predicate, "provider": provider, "currency": currency, "total": total, "input_edge_ids": ids})
    for (predicate, provider), group in sorted(count_groups.items()):
        total = sum(int((edge.get("attributes") or {})["count"]) for edge in group)
        ids = [str(edge["edge_id"]) for edge in group]
        lines.append(f"- SUM_COUNT {predicate}; provider={provider}; total={total}; input_edges={','.join(ids)}")
        aggregate_rows.append({"kind": "count_sum", "predicate": predicate, "provider": provider, "total": total, "input_edge_ids": ids})
    return "\n".join(lines), {
        "numeric_edge_count": len(numeric),
        "aggregate_count": len(aggregate_rows),
        "aggregates": aggregate_rows,
        "input_edge_ids": [str(edge["edge_id"]) for edge in numeric],
    }


def classify_v4_question(question: str) -> str:
    """Classify only the narrow query shapes handled deterministically in V4."""
    text = _canonical_text(question)
    if ("store" in text or "shop" in text or "provider" in text) and any(
        token in text for token in ("spent", "money", "amount", "most", "least", "highest", "lowest")
    ):
        return "provider_amount_rank"
    if any(token in text for token in ("how much", "total money", "expenses", "total spent")):
        return "amount_total"
    if "how many days" in text and "between" in text:
        return "temporal"
    if any(token in text for token in ("what time", "when do i", "wake up", "go to bed", "how long")):
        return "temporal"
    if any(token in text for token in ("how many", "total number", "number of")):
        return "count"
    return "graph_all"


def _query_terms(question: str) -> set[str]:
    """Extract lightweight lexical terms without a dataset/domain vocabulary."""
    terms = set()
    for token in re.findall(r"[a-z0-9]+", _canonical_text(question)):
        if len(token) < 4 or token in _QUERY_STOPWORDS:
            continue
        if token.endswith("ies") and len(token) > 4:
            token = token[:-3] + "y"
        elif token.endswith("s") and len(token) > 4:
            token = token[:-1]
        terms.add(token)
    return terms


def _edge_matches_query(edge: Mapping[str, Any], terms: set[str]) -> bool:
    attrs = edge.get("attributes") or {}
    fields = [edge.get("object"), attrs.get("provider"), attrs.get("location"), (edge.get("scope_json") or {}).get("value")]
    edge_terms = set()
    for field in fields:
        for token in re.findall(r"[a-z0-9]+", _canonical_text(str(field or ""))):
            if token.endswith("ies") and len(token) > 4:
                token = token[:-3] + "y"
            elif token.endswith("s") and len(token) > 4:
                token = token[:-1]
            edge_terms.add(token)
    return bool(terms & edge_terms)


def render_v4_query_projection(
    state_or_edges: V4GraphState | Sequence[Mapping[str, Any]], question: str,
) -> tuple[str, dict[str, Any]]:
    """Render a conservative, deterministic projection for known query shapes.

    Unknown providers are deliberately excluded from provider ranking. They remain
    in the persisted graph and can be audited, but attributing them to a nearby
    named provider would turn missing provenance into a false fact.
    """
    kind = classify_v4_question(question)
    edges = state_or_edges.edges if isinstance(state_or_edges, V4GraphState) else list(state_or_edges)
    active = merge_v4_edges([edge for edge in edges if edge.get("status") != "superseded"])
    if kind == "provider_amount_rank":
        selected = [
            edge for edge in active
            if edge.get("predicate") == "PURCHASED"
            and (edge.get("attributes") or {}).get("amount") is not None
            and (edge.get("attributes") or {}).get("provider")
        ]
        question_terms = _query_terms(question)
        relevant = [edge for edge in selected if _edge_matches_query(edge, question_terms)]
        for category, hints in _CATEGORY_HINTS.items():
            if category in question_terms:
                relevant = [edge for edge in selected if _edge_matches_query(edge, question_terms | hints)]
        if relevant:
            selected = relevant
        groups: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
        for edge in selected:
            attrs = edge.get("attributes") or {}
            groups.setdefault((str(attrs["provider"]), attrs.get("currency")), []).append(edge)
        lines = ["[V4 QUERY PROJECTION] provider_amount_rank", "[PURCHASED AMOUNTS BY KNOWN PROVIDER]"]
        aggregates = []
        for (provider, currency), group in sorted(groups.items()):
            total = sum(float((edge.get("attributes") or {})["amount"]) for edge in group)
            ids = [str(edge["edge_id"]) for edge in group]
            lines.append(f"- provider={provider}; currency={currency or 'unknown'}; total={total}; input_edges={','.join(ids)}")
            aggregates.append({"provider": provider, "currency": currency, "total": total, "input_edge_ids": ids})
        ranked = sorted(aggregates, key=lambda row: (-row["total"], row["provider"]))
        lines.append("[RANKED PROVIDERS]")
        for index, row in enumerate(ranked, 1):
            lines.append(f"- rank={index}; provider={row['provider']}; total={row['total']} {row['currency'] or ''}".rstrip())
        unknown_numeric = sum(
            1 for edge in active
            if edge.get("predicate") == "PURCHASED" and (edge.get("attributes") or {}).get("amount") is not None
            and not (edge.get("attributes") or {}).get("provider")
        )
        lines.append(f"[EXCLUDED UNKNOWN PROVIDER AMOUNTS] count={unknown_numeric}")
        return "\n".join(lines), {"projection_kind": kind, "selected_edge_count": len(selected), "aggregates": ranked, "input_edge_ids": [edge_id for row in aggregates for edge_id in row["input_edge_ids"]], "excluded_unknown_provider_count": unknown_numeric}
    if kind == "amount_total":
        selected = [
            edge for edge in active
            if edge.get("predicate") == "PURCHASED" and (edge.get("attributes") or {}).get("amount") is not None
        ]
        question_terms = _query_terms(question)
        relevant = [edge for edge in selected if _edge_matches_query(edge, question_terms)]
        if relevant:
            selected = relevant
        lines = ["[V4 QUERY PROJECTION] amount_total", "[PURCHASED AMOUNTS]"]
        total = 0.0
        for edge in selected:
            attrs = edge.get("attributes") or {}
            total += float(attrs["amount"])
            lines.append(f"- {edge.get('object')}; amount={attrs['amount']} {attrs.get('currency') or ''}; provider={attrs.get('provider') or '(unknown)'}; sources=" + ",".join(f"u{ref.get('unit_ordinal')}" for ref in edge.get("source_refs", [])))
        lines.append(f"[TOTAL PURCHASED AMOUNT] total={total}")
        return "\n".join(lines), {"projection_kind": kind, "selected_edge_count": len(selected), "total": total, "input_edge_ids": [str(edge["edge_id"]) for edge in selected]}
    if kind == "temporal":
        selected = [edge for edge in active if edge.get("predicate") in {"TARGET", "PREFERS", "OBSERVED", "OBSERVED_WAKE_TIME"}]
        question_terms = _query_terms(question)
        relevant = [edge for edge in active if edge.get("predicate") in {"ATTENDED", "OBSERVED"} and _edge_matches_query(edge, question_terms)]
        if relevant:
            selected = relevant
        if "wake" in _canonical_text(question) or "bed" in _canonical_text(question):
            selected = [
                edge for edge in selected
                if edge.get("predicate") == "OBSERVED_WAKE_TIME"
                or any(token in str(edge.get("object", "")) for token in ("wake", "woke", "waking", "bed"))
            ]
        lines = ["[V4 QUERY PROJECTION] temporal", "[TARGET PREFERENCE OBSERVATION FACTS]"]
        for edge in selected:
            time_json = edge.get("time_json") or {}
            time_value = time_json.get("value") if time_json.get("parse_status") == "ok" else "unknown"
            status = "; status=contradicted" if edge.get("status") == "contradicted" else ""
            lines.append(f"- {edge.get('subject')} --{edge.get('predicate')}--> {edge.get('object')}; time={time_value or 'unknown'}; interval_end={time_json.get('interval_end') or 'none'}{status}; sources=" + ",".join(f"u{ref.get('unit_ordinal')}" for ref in edge.get("source_refs", [])))
        return "\n".join(lines), {"projection_kind": kind, "selected_edge_count": len(selected), "input_edge_ids": [str(edge["edge_id"]) for edge in selected]}
    if kind == "count":
        selected = []
        for edge in active:
            if edge.get("predicate") not in _NUMERIC_PREDICATES:
                continue
            attrs = edge.get("attributes") or {}
            if attrs.get("count") is None:
                match = _COUNT.search(str(edge.get("object", "")))
                if match:
                    edge = deepcopy(edge)
                    edge.setdefault("attributes", {})["count"] = int(match.group("count"))
            if (edge.get("attributes") or {}).get("count") is not None:
                selected.append(edge)
        if "course" in _canonical_text(question):
            selected = [edge for edge in selected if "course" in str(edge.get("object", ""))]
        if selected:
            context, meta = render_v4_numeric_projection(selected, predicates=_NUMERIC_PREDICATES)
            return "[V4 QUERY PROJECTION] count\n" + context, {"projection_kind": kind, "selected_edge_count": len(selected), **meta}

        # A count question is not necessarily a numeric claim. Many samples
        # represent each graduation, garment, plant, or album as a separate
        # occurrence without a count attribute. Do not project an empty numeric
        # context; expose the relevant occurrence rows so Answer can count them.
        question_terms = _query_terms(question)
        occurrence_edges = [edge for edge in active if edge.get("predicate") in _OCCURRENCE_PREDICATES]
        relevant = [
            edge for edge in occurrence_edges
            if question_terms & set(re.findall(r"[a-z0-9]+", _canonical_text(str(edge.get("object", "")))))
        ]
        selected_occurrences = relevant or occurrence_edges
        lines = ["[V4 QUERY PROJECTION] count", "[OCCURRENCE FACTS TO COUNT]"]
        for edge in selected_occurrences:
            attrs = edge.get("attributes") or {}
            details = []
            if attrs.get("provider"):
                details.append(f"provider={attrs['provider']}")
            if (edge.get("time_json") or {}).get("value"):
                details.append(f"time={(edge.get('time_json') or {}).get('value')}")
            lines.append(f"- {edge.get('subject')} --{edge.get('predicate')}--> {edge.get('object')}" + ("; " + "; ".join(details) if details else ""))
        return "\n".join(lines), {
            "projection_kind": kind,
            "selected_edge_count": len(selected_occurrences),
            "count_projection_mode": "occurrence_fallback",
            "input_edge_ids": [str(edge["edge_id"]) for edge in selected_occurrences],
        }
    context, meta = render_v4_graph_all(active)
    return context, {"projection_kind": kind, **meta}


def answer_v4_messages(graph_context: str, raw_tail: str, question_date: str, question: str) -> list[dict[str, str]]:
    """Use the unchanged V1 Answer prompt with the V4 graph projection."""
    return answer_messages(graph_context, raw_tail, question_date, question)


def _occurrence_identity(claim: NormalizedV4Claim) -> str | None:
    time_json = claim.time_json if claim.time_json.get("parse_status") == "ok" else {}
    time_value = time_json.get("value")
    provider = claim.attributes.get("provider")
    location = claim.attributes.get("location")
    scope_json = claim.scope_json if claim.scope_json.get("parse_status") == "ok" else {"value": None, "parse_status": "unknown"}
    scope = scope_json.get("value")
    # Without a time or any contextual discriminator, a repeated mention may be a
    # new occurrence. Preserve it instead of silently merging it.
    if not any((time_value, provider, location, scope)):
        return None
    payload = {
        "subject": claim.subject,
        "predicate": claim.predicate,
        "canonical_object": claim.object,
        "time": {
            "value": time_json.get("value"),
            "granularity": time_json.get("granularity"),
            "interval_end": time_json.get("interval_end"),
            "recurrence": time_json.get("recurrence"),
        },
        "provider": provider,
        "location": location,
        "scope": {"value": scope_json.get("value"), "parse_status": scope_json.get("parse_status")},
    }
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))[:24]


def _has_day_only_time(claim: NormalizedV4Claim) -> bool:
    time_json = claim.time_json
    return time_json.get("parse_status") == "ok" and time_json.get("granularity") == "day" and not time_json.get("interval_end")


def _source_refs_overlap(left: Mapping[str, Any], right: NormalizedV4Claim) -> bool:
    existing = {(ref.get("session_id"), ref.get("unit_ordinal")) for ref in left.get("source_refs", [])}
    incoming = {(ref.get("session_id"), ref.get("unit_ordinal")) for ref in right.source_refs}
    return bool(existing & incoming)


def _is_ambiguous_day_match(edge: Mapping[str, Any], claim: NormalizedV4Claim) -> bool:
    """A day-only key cannot merge claims from distinct source units."""
    if not _has_day_only_time(claim):
        return False
    return not _source_refs_overlap(edge, claim)


def _same_provider_suffix_occurrence(edge: Mapping[str, Any], claim: NormalizedV4Claim) -> bool:
    """Recognize a duplicated object wording such as ``item`` / ``item from shop``.

    This is deliberately narrower than fuzzy entity matching: the evidence source,
    predicate, subject, typed provider, amount/count, and normalized time must all
    agree. It only repairs duplicate model extractions of the same source event.
    """
    attributes = edge.get("attributes") or {}
    provider = attributes.get("provider")
    if not provider or provider != claim.attributes.get("provider"):
        return False
    if edge.get("subject") != claim.subject or edge.get("predicate") != claim.predicate:
        return False
    if not _same_time(edge.get("time_json") or {}, claim.time_json):
        return False
    existing_scope = edge.get("scope_json") or {}
    incoming_scope = claim.scope_json or {}
    if (existing_scope.get("parse_status"), existing_scope.get("value")) != (incoming_scope.get("parse_status"), incoming_scope.get("value")):
        return False
    numeric_match = False
    for field in ("amount", "currency", "count", "location"):
        current, incoming = attributes.get(field), claim.attributes.get(field)
        if current != incoming:
            return False
        if field in {"amount", "count"} and current is not None:
            numeric_match = True
    if not numeric_match:
        return False
    existing_sources = {(ref.get("session_id"), ref.get("unit_ordinal")) for ref in edge.get("source_refs", [])}
    claim_sources = {(ref.get("session_id"), ref.get("unit_ordinal")) for ref in claim.source_refs}
    if existing_sources.intersection(claim_sources):
        same_source = True
    else:
        existing_sessions = {session for session, _ in existing_sources if session}
        claim_sessions = {session for session, _ in claim_sources if session}
        existing_units = [unit for session, unit in existing_sources if session in claim_sessions and isinstance(unit, int)]
        claim_units = [unit for session, unit in claim_sources if session in existing_sessions and isinstance(unit, int)]
        same_source = bool(existing_sessions.intersection(claim_sessions)) and any(
            abs(left - right) <= 32 for left in existing_units for right in claim_units
        )
    if not same_source:
        return False
    return _strip_provider_suffix(str(edge.get("object", "")), str(provider)) == _strip_provider_suffix(claim.object, str(provider))


def _same_time(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(left.get(field) == right.get(field) for field in ("value", "granularity", "interval_end", "recurrence")) and left.get("parse_status") == right.get("parse_status") == "ok"


def _strip_provider_suffix(object_text: str, provider: str) -> str:
    suffix = re.compile(rf"\s+(?:from|at)\s+{re.escape(provider)}$")
    return suffix.sub("", _canonical_text(object_text))


def _functional_key(claim: NormalizedV4Claim) -> str:
    return sha256_text(json.dumps([claim.subject, claim.predicate, claim.scope_json.get("value")], ensure_ascii=False))[:24]


def _edge_from_claim(claim: NormalizedV4Claim, *, occurrence_key: str | None, status: str) -> dict[str, Any]:
    edge_key = sha256_text(json.dumps([claim.claim_id, occurrence_key, status], ensure_ascii=False))[:24]
    return {
        "edge_id": edge_key,
        "edge_key": edge_key,
        "claim_id": claim.claim_id,
        "subject": claim.subject,
        "predicate": claim.predicate,
        "object": claim.object,
        "object_type": claim.object_type,
        "attributes": deepcopy(claim.attributes),
        "time_json": deepcopy(claim.time_json),
        "scope_json": deepcopy(claim.scope_json),
        "source_refs": list(claim.source_refs),
        "status": status,
        "occurrence_key": occurrence_key,
        "functional_key": _functional_key(claim) if claim.predicate in _FUNCTIONAL_PREDICATES else None,
        "normalization_actions": list(claim.normalization_actions),
        "attribute_conflicts": {},
        "superseded_by": None,
    }


def _merge_duplicate_edge(edge: dict[str, Any], claim: NormalizedV4Claim) -> tuple[bool, list[str]]:
    """Merge newly observed non-empty fields into an existing occurrence.

    A repeated mention often supplies provenance first and an amount/provider later.
    Deduplication must not make the first, less complete mention authoritative.
    Conflicting non-empty values are retained in an audit field instead of being
    silently overwritten.
    """
    changed = False
    merged_fields: list[str] = []
    attributes = edge.setdefault("attributes", {})
    for field, incoming in claim.attributes.items():
        if incoming is None:
            continue
        current = attributes.get(field)
        if current is None:
            attributes[field] = deepcopy(incoming)
            changed = True
            merged_fields.append(f"attributes.{field}")
        elif current != incoming:
            conflicts = edge.setdefault("attribute_conflicts", {})
            values = conflicts.setdefault(field, [current])
            if incoming not in values:
                values.append(deepcopy(incoming))
                changed = True
                merged_fields.append(f"attribute_conflicts.{field}")

    incoming_time = claim.time_json
    current_time = edge.get("time_json") or {}
    if current_time.get("parse_status") != "ok" and incoming_time.get("parse_status") == "ok":
        edge["time_json"] = deepcopy(incoming_time)
        changed = True
        merged_fields.append("time_json")
    incoming_scope = claim.scope_json
    current_scope = edge.get("scope_json") or {}
    if current_scope.get("parse_status") != "ok" and incoming_scope.get("parse_status") == "ok":
        edge["scope_json"] = deepcopy(incoming_scope)
        changed = True
        merged_fields.append("scope_json")

    refs = edge.setdefault("source_refs", [])
    for ref in claim.source_refs:
        if ref not in refs:
            refs.append(deepcopy(ref))
            changed = True
            merged_fields.append("source_refs")
    actions = edge.setdefault("normalization_actions", [])
    for action in claim.normalization_actions:
        if action not in actions:
            actions.append(deepcopy(action))
            changed = True
            merged_fields.append("normalization_actions")
    return changed, merged_fields


def _claim_audit(claim: NormalizedV4Claim) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id, "parse_status": claim.parse_status, "raw_text": claim.raw_text,
        "source_refs": list(claim.source_refs), "model_claim": claim.model_claim,
        "normalization_actions": list(claim.normalization_actions),
    }


def render_v4_manager_state(
    state: V4GraphState, *, max_edges: int = 64, max_raw_claims: int = 8,
    current_text: str = "",
) -> str:
    """Render a bounded graph view for the next extraction call.

    The complete graph remains in the state/database for routing and audit. The
    Manager only needs a compact reference for aliases and explicit updates; an
    unbounded graph would recreate V3's growing prompt problem.
    """
    if max_edges < 0 or max_raw_claims < 0:
        raise ValueError("manager graph limits must be non-negative")
    active_edges = [edge for edge in state.edges if edge.get("status") != "superseded"]
    user_evidence = "\n".join(
        re.findall(r"\[e\d+\]\[user\]\s*(.*?)(?=\n\[e\d+\]\[|\Z)", current_text, flags=re.S)
    )
    query_terms = {
        token for token in re.findall(r"[a-z0-9]+", _canonical_text(user_evidence))
        if len(token) >= 2 and token not in {"the", "and", "or", "to", "in", "of", "is", "a", "an"}
    }

    def edge_terms(edge: Mapping[str, Any]) -> set[str]:
        attrs = edge.get("attributes") or {}
        values = [edge.get("object"), attrs.get("provider"), attrs.get("location")]
        return {
            token for token in re.findall(r"[a-z0-9]+", _canonical_text(" ".join(str(value) for value in values if value)))
            if len(token) >= 2 and token not in {"the", "and", "or", "to", "in", "of", "is", "a", "an"}
        }

    def relevance(edge: Mapping[str, Any]) -> int:
        # Ignore one-character/common relation tokens; object/provider overlap
        # is the useful signal for resolving aliases across chunks.
        terms = edge_terms(edge)
        return len((terms & query_terms) - {"user", "the", "and", "or", "to", "in", "of"})

    relevant_candidates = [(index, edge) for index, edge in enumerate(active_edges) if relevance(edge) > 0]
    relevant_ranked = sorted(
        relevant_candidates,
        key=lambda item: (-relevance(item[1]), -(1 if item[1].get("occurrence_key") is not None else 0), -item[0], str(item[1].get("edge_id", ""))),
    )
    relevant_budget = min(48, max_edges)
    relevant_edges = [edge for _, edge in relevant_ranked[:relevant_budget]] if relevant_budget else []
    selected_ids = {str(edge.get("edge_id")) for edge in relevant_edges}
    recent_candidates = [(index, edge) for index, edge in enumerate(active_edges) if str(edge.get("edge_id")) not in selected_ids]
    recent_ranked = sorted(recent_candidates, key=lambda item: (-item[0], str(item[1].get("edge_id", ""))))
    recent_budget = max(0, max_edges - len(relevant_edges))
    recent_edges = [edge for _, edge in recent_ranked[:recent_budget]] if recent_budget else []
    relevant_edge_count = len(relevant_candidates)
    def compact_edge(edge: Mapping[str, Any]) -> dict[str, Any]:
        attributes = edge.get("attributes") or {}
        time_json = edge.get("time_json") or {}
        scope_json = edge.get("scope_json") or {}
        return {
            "subject": edge.get("subject"),
            "predicate": edge.get("predicate"),
            "object": edge.get("object"),
            "status": edge.get("status"),
            "attributes": {
                key: attributes.get(key)
                for key in ("amount", "currency", "count", "provider", "location")
                if attributes.get(key) is not None
            },
            "time": time_json.get("value") if time_json.get("parse_status") == "ok" else None,
            "scope": scope_json.get("value") if scope_json.get("parse_status") == "ok" else None,
        }

    def compact_raw_claim(claim: Mapping[str, Any]) -> dict[str, Any]:
        model_claim = claim.get("model_claim") if isinstance(claim.get("model_claim"), Mapping) else {}
        return {
            "subject": model_claim.get("subject_text") or model_claim.get("subject"),
            "relation": model_claim.get("relation") or model_claim.get("predicate"),
            "object": model_claim.get("object_text") or model_claim.get("object"),
        }

    return json.dumps(
        {
            "version": "4.1",
            "manager_graph_edge_budget": max_edges,
            "relevant_edges": [compact_edge(edge) for edge in relevant_edges],
            "recent_edges": [compact_edge(edge) for edge in recent_edges],
            "raw_claims": [compact_raw_claim(claim) for claim in state.raw_claims[-max_raw_claims:]] if max_raw_claims else [],
            "active_edge_count": len(active_edges),
            "relevant_edge_count": relevant_edge_count,
            "recent_edge_count": len(recent_candidates),
            "relevant_edge_truncated": len(relevant_edges) < len(relevant_candidates),
            "recent_edge_truncated": len(recent_edges) < len(recent_candidates),
            "selection_mode": "user_object_provider_location_relevance_then_recency" if user_evidence else "recency_fallback",
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def manager_v4_messages(
    state: V4GraphState, compiled: CompiledEvidence, *, max_edges: int = 64,
) -> list[dict[str, str]]:
    """Build a graph-aware extraction prompt; routing still owns canonical state."""
    manager_state = render_v4_manager_state(state, max_edges=max_edges, current_text=compiled.text)
    system = "You extract durable facts. Return one JSON object only; never answer the final question."
    user = f"""Return exactly one JSON object: {{\"claims\":[...]}}.
Each claim must contain only these required fields:
subject_text, relation, object_text, evidence_ids.
claim_text and hints are optional raw text. Do not output IDs, dates in ISO format,
node types, statuses, update actions, or normalized numbers. Copy facts from the evidence;
do not infer. The relation must be one of: bought, attended, completed, observed,
target, prefers, plans, lives in, uses, observed wake time, or mentions. Use one claim per durable user-provided
fact only. Do not extract questions, requests, recommendations, hypotheticals, options,
or assistant statements. Cite only local integer evidence IDs from user statements.
Return at most 12 claims. If there are no durable facts, return {{"claims":[]}}.
Do not emit claim_text unless the evidence itself would otherwise be ambiguous; keep every
optional hint to a short literal span from the evidence.

Optional hints may contain amount_text, count_text, time_text, provider_text,
location_text, or scope_text. If uncertain, omit the hint.

# Existing graph reference (program-normalized; do not copy facts without current evidence)
{manager_state}

# Current chunk evidence
{compiled.text}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
