"""Memory Sidecar V3: multi-event extraction with V1-style state semantics.

The model proposes events.  This module owns evidence provenance, key identity,
batch visibility and all state transitions, so it can be tested without a model
or a database.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any, Mapping, Sequence

from utils.config import sha256_text
from utils.tokenizer import Tokenizer, count_messages
from .budget import turn_units
from .protocol import (
    ALLOWED_ACTIONS,
    ALLOWED_MEMORY_TYPES,
    ALLOWED_STATUSES,
    MemoryState,
    ParsedEvent,
    answer_messages,
)


# V3 only permits deterministic, lossless key normalisation.  Keep aliases
# explicit; adding one changes routing semantics and therefore needs a test.
KEY_ALIASES: dict[str, str] = {}
_USD_AMOUNT = re.compile(r"\$(\d+(?:,\d{3})*(?:\.\d{1,2})?)")
_SEPARATORS = re.compile(r"[\s._-]+")
COMPACTOR_V3_PROMPT_VERSION = "memory-sidecar-v3-final-compactor-v1"


@dataclass(frozen=True)
class Evidence:
    evidence_id: int
    unit_ordinal: int
    session_id: str
    session_date: str
    role: str
    content: str

    def source_ref(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "session_id": self.session_id,
            "unit_ordinal": self.unit_ordinal,
        }


@dataclass(frozen=True)
class CompiledEvidence:
    text: str
    evidence: dict[int, Evidence]


@dataclass(frozen=True)
class ParsedV3Item:
    model_event: dict[str, Any]
    event: ParsedEvent | None
    source_refs: list[dict[str, Any]]
    source_unit_ordinals: list[int]
    parse_status: str
    route_status: str | None = None
    error: str | None = None


def compile_evidence(messages: Sequence[Any]) -> CompiledEvidence:
    """Compile local evidence labels and retain every source independently."""
    lines: list[str] = []
    evidence: dict[int, Evidence] = {}
    previous_date: str | None = None
    for evidence_id, message in enumerate(messages):
        session_date = str(getattr(message, "session_date", ""))
        if session_date != previous_date:
            if session_date:
                lines.append(f"## {session_date}")
            previous_date = session_date
        role = str(getattr(message, "role", "unknown"))
        content = str(getattr(message, "content", ""))
        evidence[evidence_id] = Evidence(
            evidence_id=evidence_id,
            unit_ordinal=int(getattr(message, "unit_ordinal", evidence_id)),
            session_id=str(getattr(message, "session_id", "")),
            session_date=session_date,
            role=role,
            content=content,
        )
        lines.append(f"[e{evidence_id}][{role}] {content}")
    return CompiledEvidence("\n".join(lines), evidence)


def canonical_key(key: str) -> str:
    """Return the frozen deterministic key identity used by the router."""
    normalized = unicodedata.normalize("NFKC", key).strip().lower()
    normalized = _SEPARATORS.sub(".", normalized).strip(".")
    return KEY_ALIASES.get(normalized, normalized)


def _json_object(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^\s*```(?:json)?\s*", "", text.strip(), flags=re.I)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("manager response must be a JSON object")
    return value


def is_truncated_json(text: str) -> bool:
    """Conservatively recognise an unclosed JSON response, never a bad complete one."""
    cleaned = re.sub(r"^\s*```(?:json)?\s*", "", text.strip(), flags=re.I)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    if not cleaned:
        return False
    try:
        json.loads(cleaned)
        return False
    except json.JSONDecodeError as exc:
        # JSONDecoder identifies incomplete input at the end.  A syntax error in
        # the middle is a complete malformed response and must not trigger split.
        return cleaned[-1] not in "}]" and (
            exc.msg.startswith("Unterminated") or exc.pos >= max(0, len(cleaned) - 1)
        )


def parse_manager_response(text: str, compiled: CompiledEvidence) -> list[ParsedV3Item]:
    """Parse a V3 events array while containing errors to the individual item."""
    obj = _json_object(text)
    events = obj.get("events", [])
    if not isinstance(events, list):
        raise ValueError("events must be an array")
    result: list[ParsedV3Item] = []
    for raw in events:
        if not isinstance(raw, Mapping):
            result.append(ParsedV3Item({"_raw": raw}, None, [], [], "error", "rejected_parse", "event item must be an object"))
            continue
        model_event = dict(raw)
        try:
            event, refs, units = _parse_item(model_event, compiled)
        except _EvidenceError as exc:
            result.append(ParsedV3Item(model_event, None, [], [], "error", "rejected_invalid_evidence", str(exc)))
        except (TypeError, ValueError) as exc:
            result.append(ParsedV3Item(model_event, None, [], [], "error", "rejected_parse", f"{type(exc).__name__}: {exc}"))
        else:
            result.append(ParsedV3Item(model_event, event, refs, units, "ok"))
    return result


class _EvidenceError(ValueError):
    pass


def _parse_item(item: Mapping[str, Any], compiled: CompiledEvidence) -> tuple[ParsedEvent, list[dict[str, Any]], list[int]]:
    action = str(item.get("action", "")).upper()
    if action not in ALLOWED_ACTIONS:
        raise ValueError("action must be ADD, UPDATE or NOOP")
    if action == "NOOP":
        if set(item) != {"action"}:
            raise ValueError("NOOP must contain only action")
        return ParsedEvent("NOOP", "fact", "noop", None, "active", None, {}, None), [], []
    memory_type = str(item.get("memory_type", ""))
    if memory_type not in ALLOWED_MEMORY_TYPES:
        raise ValueError(f"invalid memory_type: {memory_type!r}")
    raw_key = item.get("key")
    if not isinstance(raw_key, str) or not raw_key.strip() or len(raw_key.strip()) > 256:
        raise ValueError("event key must be 1..256 characters")
    key = canonical_key(raw_key)
    if not key:
        raise ValueError("event key is empty after canonicalization")
    if "value" not in item:
        raise ValueError("event value is required")
    status = str(item.get("status", ""))
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"invalid event status: {status!r}")
    source = item.get("source")
    if not isinstance(source, Mapping):
        raise _EvidenceError("source.evidence_ids is required")
    ids = source.get("evidence_ids")
    if not isinstance(ids, list) or not ids:
        raise _EvidenceError("source.evidence_ids must be a non-empty array")
    if any(not isinstance(value, int) or isinstance(value, bool) for value in ids):
        raise _EvidenceError("evidence_ids must contain integers")
    if len(set(ids)) != len(ids):
        raise _EvidenceError("evidence_ids must not contain duplicates")
    if any(value not in compiled.evidence for value in ids):
        raise _EvidenceError("evidence_ids must refer to local chunk evidence")
    confidence = item.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool):
            raise ValueError("confidence must be a number or null")
        confidence = float(confidence)
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
    event_date = item.get("event_date")
    if event_date is not None and not isinstance(event_date, str):
        raise ValueError("event_date must be a string or null")
    qualifier = item.get("qualifier")
    if qualifier is not None and not isinstance(qualifier, str):
        raise ValueError("qualifier must be a string or null")
    refs = [compiled.evidence[evidence_id].source_ref() for evidence_id in ids]
    return (
        ParsedEvent(action, memory_type, key, item["value"], status, event_date, {"source_refs": refs}, confidence, qualifier),
        refs,
        [compiled.evidence[evidence_id].unit_ordinal for evidence_id in ids],
    )


class V3MemoryState(MemoryState):
    """Append-only V1 records with V3 batch visibility and source semantics."""

    def copy(self) -> "V3MemoryState":
        return V3MemoryState(records=deepcopy(self.records))

    @classmethod
    def from_json(cls, text: str | None) -> "V3MemoryState":
        state = super().from_json(text)
        return cls(records=state.records)

    def render_for_answer(self) -> str:
        return json.dumps(
            {"version": 3, "records": [self._v3_prompt_record(record) for record in self.active_records]},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )

    def render_for_manager(self) -> str:
        superseded_by_key: dict[str, list[dict[str, Any]]] = {}
        for record in self.records:
            if record.get("status") == "superseded":
                superseded_by_key.setdefault(str(record.get("key")), []).append(record)
        return json.dumps(
            {
                "version": 3,
                "active_key_index": [
                    {"key": record["key"], "memory_type": record["memory_type"], "status": record["status"]}
                    for record in self.active_records
                ],
                "current_records": [self._v3_prompt_record(record) for record in self.active_records],
                "update_ledger": [self._v3_prompt_record(record) for records in superseded_by_key.values() for record in records[-2:]],
            },
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )

    @staticmethod
    def _v3_prompt_record(record: Mapping[str, Any]) -> dict[str, Any]:
        result = {
            "memory_type": record.get("memory_type"), "key": record.get("key"),
            "value": record.get("value"), "status": record.get("status"),
        }
        if record.get("event_date") is not None:
            result["event_date"] = record["event_date"]
        if record.get("qualifier") is not None:
            result["qualifier"] = record["qualifier"]
        return result

    def route_batch(self, items: Sequence[ParsedV3Item], *, batch_ordinal: int) -> list[dict[str, Any]]:
        """Route all items against the immutable batch-before state."""
        duplicate_keys: set[str] = set()
        valid_changes = [item.event for item in items if item.event and item.event.action in {"ADD", "UPDATE"}]
        for event in valid_changes:
            assert event is not None
            if sum(other.key == event.key for other in valid_changes) > 1:
                duplicate_keys.add(event.key)
        before = deepcopy(self.records)
        routes: list[dict[str, Any]] = []
        pending: list[tuple[ParsedV3Item, dict[str, Any]]] = []
        for item_ordinal, item in enumerate(items):
            event_id = f"b{batch_ordinal}-i{item_ordinal}"
            if item.event is None:
                routes.append({"route_status": item.route_status or "rejected_parse", "changed": False, "event_id": event_id, "reason": item.error})
                continue
            if item.event.action in {"ADD", "UPDATE"} and item.event.key in duplicate_keys:
                routes.append({"route_status": "rejected_duplicate_key_in_batch", "changed": False, "event_id": event_id})
                continue
            route = self._route_one(item.event, event_id, item.source_unit_ordinals, before)
            routes.append(route)
            if route["route_status"] == "applied":
                pending.append((item, route))
        # Apply only accepted transitions after every decision has used before.
        for item, route in pending:
            assert item.event is not None
            self._apply_accepted(item.event, route["event_id"], item.source_unit_ordinals)
        return routes

    def _route_one(self, event: ParsedEvent, event_id: str, source_units: list[int], before: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        active = [record for record in before if record.get("key") == event.key and record.get("status") in ALLOWED_STATUSES]
        if event.action == "NOOP":
            return {"route_status": "noop", "changed": False, "event_id": event_id}
        if event.action == "ADD":
            if any(record.get("value") == event.value for record in active):
                return {"route_status": "deduplicated", "changed": False, "event_id": event_id}
            if active:
                status = "rejected_ambiguous_occurrence_key" if event.memory_type == "event" else "rejected_add_conflict"
                return {"route_status": status, "changed": False, "event_id": event_id}
        if event.action == "UPDATE":
            if not active:
                drift = self._key_drift(event.key, before)
                return {"route_status": "rejected_key_drift" if drift else "rejected_update_missing_target", "changed": False, "event_id": event_id, "key_drift_candidate": drift}
            if any(record.get("memory_type") != event.memory_type for record in active):
                return {"route_status": "rejected_update_type_mismatch", "changed": False, "event_id": event_id}
        return {"route_status": "applied", "changed": True, "event_id": event_id, "source_unit_ordinals": source_units}

    @staticmethod
    def _key_drift(key: str, records: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        candidates = [str(record.get("key")) for record in records if record.get("status") in ALLOWED_STATUSES]
        if not candidates:
            return None
        candidate, score = max(((candidate, SequenceMatcher(None, key, candidate).ratio()) for candidate in candidates), key=lambda pair: pair[1])
        return {"candidate_key": candidate, "normalized_key": key, "method": "sequence_matcher", "score": score} if score >= 0.82 else None

    def _apply_accepted(self, event: ParsedEvent, event_id: str, source_units: list[int]) -> None:
        if event.action == "NOOP":
            return
        if event.action == "UPDATE":
            for record in self.records:
                if record.get("key") == event.key and record.get("status") in ALLOWED_STATUSES:
                    record["status"] = "superseded"
                    record["superseded_by"] = event_id
        record: dict[str, Any] = {
            "event_id": event_id, "key": event.key, "memory_type": event.memory_type,
            "value": event.value, "status": event.status, "event_date": event.event_date,
            "source": event.source, "source_unit_ordinals": source_units, "confidence": event.confidence,
        }
        if event.qualifier is not None:
            record["qualifier"] = event.qualifier
        self.records.append(record)


def manager_v3_messages(memory_state: str, compiled: CompiledEvidence) -> list[dict[str, str]]:
    system = "You are a structured memory controller. Return one JSON object only; never answer the final question."
    user = f"""Return exactly one JSON object: {{"events":[...]}}.
Each ADD or UPDATE item needs action, memory_type, key, value, status, event_date,
source.evidence_ids, confidence, and qualifier. `source.evidence_ids` must be a non-empty
JSON array of local integer IDs, for example {{"source":{{"evidence_ids":[0,3]}}}}.
The chunk displays them as e0/e3 only for readability: emit 0 and 3, never "e0"/"e3" strings.
Use ADD only for a new stable key.
Use UPDATE only for an explicit later replacement and copy the supplied active key exactly.
UPDATE replaces the whole value. Never use PATCH, REPLACE, target_ref, record_ref, or field edits.
Use distinct keys and distinct event values for plans/targets and observed/completed facts;
never combine a target and an observation in one record. Do not UPDATE an aggregate count
merely because one new member was added: UPDATE a count only when the evidence explicitly
states the new total. Preserve exact names, numbers, dates, actions, relationships and
currencies. Cite only local evidence IDs.
For every purchase or paid amount emit a separate event with action, item, amount and currency.
Return {{"events":[]}} when there is no durable information. NOOP is exactly {{"action":"NOOP"}}.

# Complete current memory
{memory_state}

# Current chunk evidence
{compiled.text}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def answer_v3_messages(memory_state: str, raw_tail: str, question_date: str, question: str) -> list[dict[str, str]]:
    """Keep the V1 Answer template fixed for memory-context comparisons."""
    return answer_messages(memory_state, raw_tail, question_date, question)


def render_compactor_v3_input(state: V3MemoryState) -> tuple[str, list[str]]:
    """Render only current records, retaining IDs for database-level provenance."""
    records = []
    record_ids: list[str] = []
    for record in state.active_records:
        records.append({
            "event_id": record["event_id"], "memory_type": record["memory_type"], "key": record["key"],
            "value": record["value"], "status": record["status"], "event_date": record.get("event_date"),
            "qualifier": record.get("qualifier"), "source": record.get("source", {"source_refs": []}),
        })
        record_ids.append(str(record["event_id"]))
    return json.dumps({"version": 3, "records": records}, ensure_ascii=False, sort_keys=True, separators=(",", ":")), record_ids


def compactor_v3_messages(memory_json: str) -> list[dict[str, str]]:
    """Ask for a standalone, traceable text context without touching canonical memory."""
    system = "You compact structured memory for a later answer. Return one JSON object only."
    user = f"""Return exactly {{\"summary_text\":\"...\"}}.
Write a concise standalone factual memory summary. Preserve current facts, preferences, dates,
numbers, currencies, relationships, and unresolved plans. Merge only duplicate or equivalent
records. Do not invent facts, do not perform arithmetic, do not answer any final question, and
do not include commentary about this task. The summary must stand alone because the later Answer
will receive this text together with a separate recent raw tail, but no structured memory.

# Current canonical memory
{memory_json}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_compactor_response(text: str) -> str:
    """Validate the narrow final-summary protocol before it reaches Answer."""
    obj = _json_object(text)
    if set(obj) != {"summary_text"}:
        raise ValueError("compactor response must contain only summary_text")
    summary = obj["summary_text"]
    if not isinstance(summary, str):
        raise ValueError("summary_text must be a string")
    return summary.strip()


def fit_answer_v3_context(
    memory_json: str,
    raw_tail: str,
    question_date: str,
    question: str,
    tokenizer: Tokenizer,
    context_budget: int,
    answer_max_tokens: int,
    tail_blocks: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, str]] | None, str, int, int]:
    """Use the shared rolling budget; only trim old complete turns from recent tail."""
    reserve = max(0, answer_max_tokens)
    base = answer_v3_messages(memory_json, "", question_date, question)
    if count_messages(base, tokenizer) + reserve > context_budget:
        return None, raw_tail, tokenizer.count(raw_tail) if raw_tail else 0, 0
    prompt = answer_v3_messages(memory_json, raw_tail, question_date, question)
    full_tokens = tokenizer.count(raw_tail) if raw_tail else 0
    if count_messages(prompt, tokenizer) + reserve <= context_budget:
        return prompt, raw_tail, full_tokens, 0
    units = _tail_turns(tail_blocks) if tail_blocks is not None else [(raw_tail,)]
    while units:
        candidate = "\n".join(block for unit in units for block in unit)
        prompt = answer_v3_messages(memory_json, candidate, question_date, question)
        if count_messages(prompt, tokenizer) + reserve <= context_budget:
            return prompt, candidate, tokenizer.count(candidate), max(0, full_tokens - tokenizer.count(candidate))
        units.pop(0)
    return base, "", 0, full_tokens


def _tail_turns(tail_blocks: Sequence[Mapping[str, Any]]) -> list[tuple[str, ...]]:
    """Group persisted rolling blocks into complete user/assistant turns."""
    blocks = [str(block.get("text", "")) for block in tail_blocks if block.get("text")]
    units: list[tuple[str, ...]] = []
    index = 0
    while index < len(blocks):
        current = blocks[index]
        following = blocks[index + 1] if index + 1 < len(blocks) else None
        current_role = current.rsplit("\n", 1)[-1].split(":", 1)[0].strip().lower()
        following_role = (following.rsplit("\n", 1)[-1].split(":", 1)[0].strip().lower() if following else "")
        if current_role == "user" and following_role == "assistant":
            units.append((current, following))
            index += 2
        else:
            # An initial assistant or terminal user is still a complete source
            # unit, but never split inside its rendered block.
            units.append((current,))
            index += 1
    return units


def split_chunk_at_turn_boundary(chunk: Sequence[Any]) -> tuple[tuple[Any, ...], tuple[Any, ...]] | None:
    units = turn_units(chunk)
    if len(units) < 2:
        return None
    midpoint = len(units) // 2
    return tuple(message for unit in units[:midpoint] for message in unit), tuple(message for unit in units[midpoint:] for message in unit)


def uncovered_usd_amounts(items: Sequence[ParsedV3Item], routes: Sequence[Mapping[str, Any]], compiled: CompiledEvidence) -> list[dict[str, Any]]:
    """Audit only: missing user dollar amounts never cause an additional completion."""
    required: dict[tuple[int, str], dict[str, Any]] = {}
    for evidence_id, evidence in compiled.evidence.items():
        if evidence.role != "user":
            continue
        for match in _USD_AMOUNT.finditer(evidence.content):
            amount = format(Decimal(match.group(1).replace(",", "")).normalize(), "f")
            required[(evidence_id, amount)] = {"evidence_id": evidence_id, "amount": amount, "text": match.group(0)}
    covered: set[tuple[int, str]] = set()
    for item, route in zip(items, routes, strict=True):
        if route.get("route_status") not in {"applied", "deduplicated"} or item.event is None:
            continue
        value = item.event.value
        amount = value.get("amount") if isinstance(value, Mapping) else None
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or not math.isfinite(amount):
            continue
        normalized = format(Decimal(str(amount)).normalize(), "f")
        for ref in item.source_refs:
            covered.add((int(ref["evidence_id"]), normalized))
    return [mention for key, mention in required.items() if key not in covered]


def state_sha256_v3(state: V3MemoryState) -> str:
    return sha256_text(state.to_json())
