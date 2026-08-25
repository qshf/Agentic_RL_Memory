"""Memory Sidecar V2: evidence compilation, validation and deterministic routing.

The manager is deliberately treated as an untrusted semantic classifier.  This
module owns all identifiers, provenance, temporal anchoring and version changes.
It is dependency free so that routing can be tested without an API client.
"""
from __future__ import annotations

import calendar
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from utils.config import sha256_text
from .protocol import answer_messages

V2_RECORD_TYPES = frozenset({"fact", "preference", "event", "plan", "assistant_fact"})
V2_STATUSES = frozenset({"active", "planned", "completed"})
V2_OPS = frozenset({"ADD", "PATCH", "REPLACE"})
_USD_AMOUNT = re.compile(r"\$(\d+(?:,\d{3})*(?:\.\d{1,2})?)")


def _normalized_money_amount(value: str | int | float) -> str:
    return format(Decimal(str(value)).normalize(), "f")


@dataclass(frozen=True)
class Evidence:
    evidence_id: int
    unit_ordinal: int
    session_id: str
    session_date: str
    role: str
    content: str
    content_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "unit_ordinal": self.unit_ordinal,
            "session_id": self.session_id,
            "session_date": self.session_date,
            "role": self.role,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class CompiledEvidence:
    text: str
    evidence: dict[int, Evidence]

    @property
    def provenance(self) -> dict[int, Evidence]:
        return self.evidence


def compile_evidence(messages: Sequence[Any]) -> CompiledEvidence:
    """Render a chunk with local ``e0``... identifiers and immutable provenance."""
    lines: list[str] = []
    mapping: dict[int, Evidence] = {}
    previous_date: str | None = None
    for evidence_id, message in enumerate(messages):
        session_date = str(getattr(message, "session_date", getattr(message, "date", "")))
        if session_date != previous_date:
            if session_date:
                lines.append(f"## {session_date}")
            previous_date = session_date
        role = str(getattr(message, "role", "unknown"))
        content = str(getattr(message, "content", ""))
        mapping[evidence_id] = Evidence(
            evidence_id=evidence_id,
            unit_ordinal=int(getattr(message, "unit_ordinal", evidence_id)),
            session_id=str(getattr(message, "session_id", "")),
            session_date=session_date,
            role=role,
            content=content,
            content_sha256=sha256_text(content),
        )
        lines.append(f"[e{evidence_id}][{role}] {content}")
    return CompiledEvidence("\n".join(lines), mapping)


def _json_object(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^\s*```(?:json)?\s*", "", text.strip(), flags=re.I)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if not match:
            raise ValueError("response did not contain a JSON object") from None
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("response must be a JSON object")
    return value


def parse_manager_response(text: str) -> list[dict[str, Any]]:
    """Parse a V2 response; malformed items are returned as item-local errors.

    A bad event never hides valid siblings in the same ``events`` array.
    """
    obj = _json_object(text)
    events = obj.get("events", [])
    if not isinstance(events, list):
        raise ValueError("events must be an array")
    result: list[dict[str, Any]] = []
    for item in events:
        try:
            result.append(validate_event(item))
        except (TypeError, ValueError) as exc:
            result.append({"_error": f"{type(exc).__name__}: {exc}", "_raw": item})
    return result


def uncovered_usd_amounts(events: Sequence[Mapping[str, Any]], compiled: CompiledEvidence) -> list[dict[str, Any]]:
    """返回所有「没有有效 manager event 覆盖」的用户陈述美元金额。

    assistant 常会在表格或回答中复述用户已经陈述的金额；这些复述不是新事实。
    因而这是金额覆盖保证的确定性半边：只扫描用户证据里的显式 ``$金额``，
    只有当某条有效 event 恰好引用了包含它的 evidence_id、且
    ``attributes.amount`` 等于归一化后的数值时，才认为该金额被覆盖。
    剩下没被覆盖的，就是被 manager 悄悄丢弃的金额，会通过
    ``manager_money_repair_messages`` 回喂给模型重试。
    """
    # required[(evidence_id, amount)]：用户证据里出现的每个显式 $金额，
    # 以 (来源证据, 归一化金额) 为键，使 "$1,000.00" 与 1000 可比对相等
    # （去掉千分位逗号、抹平小数尾零）。
    required: dict[tuple[int, str], dict[str, Any]] = {}
    for evidence_id, evidence in compiled.evidence.items():
        if evidence.role != "user":
            continue
        for match in _USD_AMOUNT.finditer(evidence.content):
            amount = _normalized_money_amount(match.group(1).replace(",", ""))
            required[(evidence_id, amount)] = {
                "evidence_id": evidence_id,
                "amount": amount,
                "text": match.group(0),
            }

    # covered：至少被一条「有效」event 记录的 (evidence_id, amount) 集合。
    # 带 _error 的坏条目、非有限或布尔型的 amount 一律不算覆盖。
    covered: set[tuple[int, str]] = set()
    for event in events:
        if "_error" in event:
            continue
        amount = event.get("attributes", {}).get("amount")
        if (not isinstance(amount, (int, float)) or isinstance(amount, bool)
                or not math.isfinite(amount)):
            continue
        normalized = _normalized_money_amount(amount)
        for evidence_id in event.get("evidence_ids", []):
            covered.add((evidence_id, normalized))

    # 差集：证据要求出现、但没有被任何有效 event 提供的金额，
    # 正是需要修复（repair）的「缺失」金额。
    return [mention for key, mention in required.items() if key not in covered]


def _repair_event_slot(event: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """返回 repair 可替换的事件槽位；解析失败条目没有槽位。"""
    if "_error" in event:
        return None
    if event["op"] in {"PATCH", "REPLACE"}:
        return ("target", event["target_ref"])
    return (
        "add", event["record_type"], event["key"], event["semantic_status"],
        event.get("time_expression"), event.get("time_evidence_id"),
    )


def merge_money_repair_events(
    initial_events: Sequence[Mapping[str, Any]],
    repair_events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """将增量金额修复并入首轮事件，替换相同事件槽位而非重复追加。

    PATCH/REPLACE 按 ``target_ref`` 对齐；ADD 按记录类型、key、状态和时间字段
    对齐。后者覆盖“首轮已新增同一事实但漏填金额”的情形。repair 本身若含
    解析错误或重复槽位，视为不可信并拒绝整个修复结果。
    """
    initial = [deepcopy(dict(event)) for event in initial_events]
    initial_add_slots = {
        _repair_event_slot(event) for event in initial
        if event.get("op") == "ADD" and _repair_event_slot(event) is not None
    }
    repairs: list[dict[str, Any]] = []
    for event in repair_events:
        # 首轮 ADD 尚未路由，因此不存在可供模型填写的 target_ref。模型偶尔会把
        # “替换这条首轮 ADD”误写成无 target 的 REPLACE；只有精确命中首轮 ADD
        # 槽位时，才可安全地将它解释为替换 ADD，其他无 target REPLACE 仍拒绝。
        raw = event.get("_raw") if "_error" in event else None
        if (isinstance(raw, Mapping) and str(raw.get("op", "")).upper() == "REPLACE"
                and raw.get("target_ref") is None):
            try:
                candidate = validate_event({**raw, "op": "ADD"})
            except (TypeError, ValueError):
                candidate = None
            if candidate is not None and _repair_event_slot(candidate) in initial_add_slots:
                repairs.append(candidate)
                continue
        repairs.append(deepcopy(dict(event)))
    slots = [_repair_event_slot(event) for event in repairs]
    if not repairs or any(slot is None for slot in slots) or len(set(slots)) != len(slots):
        raise ValueError("repair events must be valid and use distinct event slots")

    merged = initial
    for repair, slot in zip(repairs, slots, strict=True):
        matches = [index for index, event in enumerate(merged) if _repair_event_slot(event) == slot]
        if not matches:
            merged.append(repair)
            continue
        # 首轮的同槽位事件可能已经违反“一 target 一变更”。以 repair 作为
        # 唯一的权威版本，消除重复项后再路由。
        first = matches[0]
        original = merged[first]
        replacement = deepcopy(repair)
        # repair 往往只包含 amount/currency。保留同一首轮事件中已经正确
        # 抽出的字段与证据，只有 repair 显式给出的字段覆盖旧值。
        replacement["attributes"] = {**original.get("attributes", {}), **repair["attributes"]}
        replacement["evidence_ids"] = list(dict.fromkeys(
            [*original.get("evidence_ids", []), *repair["evidence_ids"]]
        ))
        if repair.get("time_expression") is None and original.get("time_expression") is not None:
            replacement["time_expression"] = original["time_expression"]
            replacement["time_evidence_id"] = original["time_evidence_id"]
        merged[first] = replacement
        for index in reversed(matches[1:]):
            del merged[index]
    return merged


def parse_reconciliation_response(text: str) -> list[dict[str, Any]]:
    """Parse the constrained final reconciler response."""
    obj = _json_object(text)
    groups = obj.get("duplicate_groups", [])
    if not isinstance(groups, list):
        raise ValueError("duplicate_groups must be an array")
    result: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, Mapping) or not isinstance(group.get("record_refs"), list):
            result.append({"_error": "duplicate group must contain record_refs", "_raw": group})
            continue
        refs = group["record_refs"]
        if any(not isinstance(ref, str) or not ref for ref in refs):
            result.append({"_error": "record_refs must contain non-empty strings", "_raw": group})
            continue
        result.append({"record_refs": list(dict.fromkeys(refs))})
    return result


def validate_event(item: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise TypeError("event item must be an object")
    op = str(item.get("op", "")).upper()
    if op not in V2_OPS:
        raise ValueError("op must be ADD, PATCH or REPLACE")
    record_type = str(item.get("record_type", ""))
    if record_type not in V2_RECORD_TYPES:
        raise ValueError(f"invalid record_type: {record_type!r}")
    key = str(item.get("key", "")).strip()
    if not key or len(key) > 256:
        raise ValueError("key must be 1..256 characters")
    status = str(item.get("semantic_status", ""))
    if status not in V2_STATUSES:
        raise ValueError(f"invalid semantic_status: {status!r}")
    attributes = item.get("attributes", {})
    if not isinstance(attributes, dict):
        raise ValueError("attributes must be an object")
    if "amount" in attributes:
        amount = attributes["amount"]
        if (not isinstance(amount, (int, float)) or isinstance(amount, bool)
                or not math.isfinite(amount)):
            raise ValueError("attributes.amount must be a finite number, not null")
    if "currency" in attributes and (not isinstance(attributes["currency"], str) or not attributes["currency"].strip()):
        raise ValueError("attributes.currency must be a non-empty string when present")
    evidence_ids = item.get("evidence_ids")
    if not isinstance(evidence_ids, list) or not evidence_ids:
        raise ValueError("evidence_ids must be a non-empty array")
    normalized_evidence_ids: list[int] = []
    for value in evidence_ids:
        if isinstance(value, int) and not isinstance(value, bool):
            normalized_evidence_ids.append(value)
        elif isinstance(value, str) and re.fullmatch(r"e\d+", value):
            normalized_evidence_ids.append(int(value[1:]))
        else:
            raise ValueError("evidence_ids must contain integers or eN labels")
    expression = item.get("time_expression")
    if expression is not None and not isinstance(expression, str):
        raise ValueError("time_expression must be a string or null")
    time_evidence_id = item.get("time_evidence_id")
    if isinstance(time_evidence_id, str) and re.fullmatch(r"e\d+", time_evidence_id):
        time_evidence_id = int(time_evidence_id[1:])
    if expression is None and time_evidence_id is not None:
        raise ValueError("time_evidence_id requires time_expression")
    if expression is not None and not isinstance(time_evidence_id, int):
        raise ValueError("time_evidence_id is required with time_expression")
    target = item.get("target_ref")
    if op == "ADD" and target is not None:
        raise ValueError("ADD must not contain target_ref")
    if op != "ADD" and (not isinstance(target, str) or not target):
        raise ValueError(f"{op} requires target_ref")
    return {
        "op": op,
        "record_type": record_type,
        "key": key,
        "semantic_status": status,
        "attributes": deepcopy(attributes),
        "time_expression": expression,
        "time_evidence_id": time_evidence_id,
        "evidence_ids": list(dict.fromkeys(normalized_evidence_ids)),
        **({"target_ref": target} if target is not None else {}),
    }


def _parse_anchor(value: str) -> datetime | None:
    match = re.search(r"(\d{4}/\d{2}/\d{2})[^0-9]*(\d{2}:\d{2})", value)
    if match:
        return datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y/%m/%d %H:%M")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value[:10], fmt)
        except ValueError:
            pass
    return None


def resolve_time(expression: str | None, anchor_session_date: str) -> str | None:
    """Resolve common relative expressions against a session timestamp.

    Unknown or ambiguous phrases intentionally remain unresolved rather than being
    guessed.  Date-only ISO output keeps the answer projection compact.
    """
    if not expression:
        return None
    anchor = _parse_anchor(anchor_session_date)
    if anchor is None:
        return None
    text = expression.strip().lower()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    month = re.fullmatch(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?", text)
    if month:
        names = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
        number = names.get(month.group(1))
        year = int(month.group(3) or anchor.year)
        if number:
            try:
                return date(year, number, int(month.group(2))).isoformat()
            except ValueError:
                return None
    if text in {"yesterday", "the day before"}:
        return (anchor.date() - timedelta(days=1)).isoformat()
    if text == "today":
        return anchor.date().isoformat()
    if text == "last month":
        total = anchor.year * 12 + anchor.month - 2
        year, month_number = divmod(total, 12)
        return date(year, month_number + 1, 1).isoformat()
    if text == "last year":
        return date(anchor.year - 1, anchor.month, min(anchor.day, calendar.monthrange(anchor.year - 1, anchor.month)[1])).isoformat()
    relative = re.fullmatch(r"(?:about\s+)?(\d+)\s+(day|week|month|year)s?\s+ago", text)
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2)
        if unit == "day":
            result = anchor.date() - timedelta(days=amount)
        elif unit == "week":
            result = anchor.date() - timedelta(days=7 * amount)
        elif unit == "month":
            total = anchor.year * 12 + anchor.month - amount
            year, month_number = divmod(total - 1, 12)
            result = date(year, month_number + 1, min(anchor.day, calendar.monthrange(year, month_number + 1)[1]))
        else:
            try:
                result = anchor.date().replace(year=anchor.year - amount)
            except ValueError:
                result = anchor.date().replace(month=2, day=28, year=anchor.year - amount)
        return result.isoformat()
    weekdays = {name.lower(): index for index, name in enumerate(calendar.day_name)}
    match = re.fullmatch(r"last\s+(" + "|".join(weekdays) + r")", text)
    if match:
        target = weekdays[match.group(1)]
        delta = (anchor.weekday() - target) % 7 or 7
        return (anchor.date() - timedelta(days=delta)).isoformat()
    return None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _merge_attributes(old: Mapping[str, Any], new: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    merged = deepcopy(dict(old))
    for key, value in new.items():
        # Legacy pilot records may contain a null placeholder. Filling it is a
        # monotonic enrichment; two unequal known values remain a conflict.
        if merged.get(key) is None and value is not None:
            merged[key] = deepcopy(value)
            continue
        if key in merged and _canonical(merged[key]) != _canonical(value):
            return None, key
        merged[key] = deepcopy(value)
    return merged, None


@dataclass
class V2MemoryState:
    records: list[dict[str, Any]] = field(default_factory=list)

    @property
    def current_records(self) -> list[dict[str, Any]]:
        return [r for r in self.records if r.get("lifecycle") == "current"]

    def as_dict(self) -> dict[str, Any]:
        return {"version": 2, "records": deepcopy(self.records)}

    def to_json(self) -> str:
        return _canonical(self.as_dict())

    def answer_records(self) -> list[dict[str, Any]]:
        return [
            {"record_type": r["record_type"], "key": r["key"], "semantic_status": r["semantic_status"],
             "attributes": deepcopy(r["attributes"]), "temporal": deepcopy(r.get("temporal", {}))}
            for r in self.current_records
        ]

    def render_for_answer(self) -> str:
        return _canonical({"version": 2, "records": self.answer_records()})

    def render_for_reconciler(self) -> str:
        """Render current records with internal refs for duplicate routing only."""
        return _canonical({"version": 2, "records": [
            {"record_ref": r["record_ref"], "record_type": r["record_type"], "key": r["key"],
             "semantic_status": r["semantic_status"], "attributes": deepcopy(r["attributes"]),
             "temporal": deepcopy(r.get("temporal", {}))}
            for r in self.current_records
        ]})

    def render_for_manager(self, max_records: int = 256) -> str:
        records = self.current_records[-max_records:] if max_records > 0 else []
        return _canonical({"records": [{"record_ref": r["record_ref"], "record_type": r["record_type"],
            "key": r["key"], "semantic_status": r["semantic_status"], "attributes": r["attributes"],
            "normalized_time": r.get("temporal", {}).get("normalized_date")} for r in records]})

    def _new_ref(self, batch_ordinal: int, item_ordinal: int, prefix: str = "r") -> str:
        return f"{prefix}-{batch_ordinal}-{item_ordinal}"

    def _supersede(self, old: dict[str, Any], new_ref: str) -> None:
        old["lifecycle"] = "superseded"
        old["superseded_by_record_ref"] = new_ref

    def _materialize(self, event: dict[str, Any], evidence: Mapping[int, Evidence], batch: int, item: int,
                     prior: dict[str, Any] | None = None, *, prefix: str = "r") -> dict[str, Any]:
        refs = [evidence[eid].as_dict() for eid in event["evidence_ids"]]
        temporal: dict[str, Any] = {}
        if event.get("time_expression") is not None:
            anchor = evidence[event["time_evidence_id"]]
            temporal = {"expression": event["time_expression"], "time_evidence_id": event["time_evidence_id"],
                        "anchor_session_date": anchor.session_date, "normalized_date": resolve_time(event["time_expression"], anchor.session_date),
                        "resolver": "deterministic-v1"}
        field_provenance = {name: deepcopy(refs) for name in event["attributes"]}
        return {"record_ref": self._new_ref(batch, item, prefix), "record_type": event["record_type"],
                "key": event["key"], "semantic_status": event["semantic_status"],
                "attributes": deepcopy(event["attributes"]), "temporal": temporal,
                "source_refs": refs, "field_provenance": field_provenance, "lifecycle": "current",
                "prior_record_ref": prior["record_ref"] if prior else None,
                "superseded_by_record_ref": None}

    def route_batch(self, events: Sequence[Mapping[str, Any]], evidence: Mapping[int, Evidence], batch_ordinal: int) -> list[dict[str, Any]]:
        """Route all events against the batch-start snapshot (no version forks)."""
        if isinstance(evidence, CompiledEvidence):
            evidence = evidence.evidence
        start_current = {r["record_ref"]: r for r in self.current_records}
        targets = [str(e.get("target_ref")) for e in events if e.get("op") in {"PATCH", "REPLACE"}]
        duplicated = {ref for ref in targets if targets.count(ref) > 1}
        results: list[dict[str, Any]] = []
        for item_index, raw in enumerate(events):
            if "_error" in raw:
                results.append({"item_ordinal": item_index, "route_status": "rejected_parse", "error": raw["_error"]})
                continue
            event = dict(raw)
            invalid_ids = [eid for eid in event["evidence_ids"] if eid not in evidence]
            invalid_time = event.get("time_expression") is not None and (
                event.get("time_evidence_id") not in event["evidence_ids"] or event.get("time_evidence_id") not in evidence
            )
            if invalid_time:
                results.append({"item_ordinal": item_index, "route_status": "rejected_invalid_time_evidence"})
                continue
            if invalid_ids:
                results.append({"item_ordinal": item_index, "route_status": "rejected_invalid_evidence", "invalid_evidence_ids": invalid_ids})
                continue
            target = start_current.get(event.get("target_ref"))
            if event["op"] in {"PATCH", "REPLACE"} and str(event.get("target_ref")) in duplicated:
                results.append({"item_ordinal": item_index, "route_status": "rejected_duplicate_target_in_batch"})
                continue
            if event["op"] in {"PATCH", "REPLACE"} and target is None:
                results.append({"item_ordinal": item_index, "route_status": "rejected_invalid_target"})
                continue
            if target and (target["key"], target["record_type"]) != (event["key"], event["record_type"]):
                results.append({"item_ordinal": item_index, "route_status": "rejected_target_mismatch"})
                continue
            if event["op"] == "ADD":
                candidates = [r for r in start_current.values() if (r["key"], r["record_type"], r["semantic_status"]) == (event["key"], event["record_type"], event["semantic_status"])]
                temporal_probe = None
                if event.get("time_expression") is not None:
                    temporal_probe = resolve_time(event["time_expression"], evidence[event["time_evidence_id"]].session_date)
                # Without a temporal/occurrence discriminator, two identical
                # events may be a restatement or two occurrences.  Keep both
                # and defer the decision to final reconciliation.
                occurrence_known = temporal_probe is not None
                exact = next((r for r in candidates if occurrence_known and r.get("temporal", {}).get("normalized_date") == temporal_probe and _canonical(r["attributes"]) == _canonical(event["attributes"])), None)
                if exact:
                    successor = deepcopy(exact)
                    successor["record_ref"] = self._new_ref(batch_ordinal, item_index)
                    successor["prior_record_ref"] = exact["record_ref"]
                    successor["source_refs"] = _dedupe_refs(exact.get("source_refs", []) + [evidence[e].as_dict() for e in event["evidence_ids"]])
                    successor["field_provenance"] = deepcopy(exact.get("field_provenance", {}))
                    self._supersede(exact, successor["record_ref"])
                    self.records.append(successor)
                    results.append({"item_ordinal": item_index, "route_status": "deduplicated_source_merged", "created_record_ref": successor["record_ref"]})
                    continue
                compatible = next((r for r in candidates if occurrence_known and r.get("temporal", {}).get("normalized_date") == temporal_probe and _merge_attributes(r["attributes"], event["attributes"])[0] is not None), None)
                if compatible:
                    event["op"] = "PATCH"
                    target = compatible
                    event["target_ref"] = compatible["record_ref"]
                elif any(r.get("temporal", {}).get("normalized_date") == temporal_probe and temporal_probe is not None for r in candidates):
                    results.append({"item_ordinal": item_index, "route_status": "rejected_add_conflict"})
                    continue
            if event["op"] == "PATCH":
                merged, conflict = _merge_attributes(target["attributes"], event["attributes"])
                if conflict:
                    results.append({"item_ordinal": item_index, "route_status": "rejected_patch_conflict", "field": conflict})
                    continue
                old_temporal = deepcopy(target.get("temporal", {}))
                new_temporal = old_temporal
                if event.get("time_expression") is not None:
                    candidate = self._materialize(event, evidence, batch_ordinal, item_index)["temporal"]
                    if old_temporal.get("normalized_date") and candidate.get("normalized_date") and old_temporal["normalized_date"] != candidate["normalized_date"]:
                        results.append({"item_ordinal": item_index, "route_status": "rejected_patch_temporal_conflict"})
                        continue
                    new_temporal = candidate
                successor = self._materialize(event, evidence, batch_ordinal, item_index, target)
                successor["attributes"] = merged or {}
                successor["temporal"] = new_temporal
                successor["source_refs"] = _dedupe_refs(target.get("source_refs", []) + [evidence[e].as_dict() for e in event["evidence_ids"]])
                successor["field_provenance"] = deepcopy(target.get("field_provenance", {}))
                for name in event["attributes"]:
                    successor["field_provenance"][name] = [evidence[e].as_dict() for e in event["evidence_ids"]]
                self._supersede(target, successor["record_ref"])
                self.records.append(successor)
                results.append({"item_ordinal": item_index, "route_status": "converted_add_to_patch" if raw.get("op") == "ADD" else "applied", "created_record_ref": successor["record_ref"]})
                continue
            successor = self._materialize(event, evidence, batch_ordinal, item_index, target)
            if target:
                self._supersede(target, successor["record_ref"])
            self.records.append(successor)
            results.append({"item_ordinal": item_index, "route_status": "applied", "created_record_ref": successor["record_ref"]})
        return results

    def reconcile(self, groups: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Merge only validated duplicate groups; uncertain/conflicting groups stay intact."""
        current = {r["record_ref"]: r for r in self.current_records}
        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        for index, group in enumerate(groups):
            refs = group.get("record_refs") if isinstance(group, Mapping) else None
            if (not isinstance(refs, list) or len(refs) < 2
                    or len({ref for ref in refs if isinstance(ref, str)}) != len(refs)
                    or any(ref not in current for ref in refs) or any(ref in seen for ref in refs)):
                results.append({"group_ordinal": index, "route_status": "rejected_invalid_group"})
                continue
            rows = [current[ref] for ref in refs]
            if len({(r["record_type"], r["key"], r["semantic_status"]) for r in rows}) != 1:
                results.append({"group_ordinal": index, "route_status": "rejected_group_mismatch"})
                continue
            merged = deepcopy(rows[0]["attributes"])
            invalid = False
            for row in rows[1:]:
                merged, conflict = _merge_attributes(merged, row["attributes"])
                if conflict:
                    invalid = True
                    break
                left, right = rows[0].get("temporal", {}).get("normalized_date"), row.get("temporal", {}).get("normalized_date")
                if left and right and left != right:
                    invalid = True
                    break
            if invalid:
                results.append({"group_ordinal": index, "route_status": "rejected_group_conflict"})
                continue
            parent = min(rows, key=self.records.index)
            successor = deepcopy(parent)
            successor["record_ref"] = f"r-final-{index}"
            successor["attributes"] = merged or {}
            temporal_values = [row.get("temporal", {}) for row in rows]
            successor["temporal"] = next((value for value in temporal_values if value.get("normalized_date") or value.get("expression")), {})
            successor["source_refs"] = _dedupe_refs([ref for row in rows for ref in row.get("source_refs", [])])
            successor["field_provenance"] = {key: _dedupe_refs([ref for row in rows for ref in row.get("field_provenance", {}).get(key, [])]) for key in successor["attributes"]}
            successor["prior_record_ref"] = parent["record_ref"]
            for row in rows:
                self._supersede(row, successor["record_ref"])
            self.records.append(successor)
            seen.update(refs)
            results.append({"group_ordinal": index, "route_status": "applied", "created_record_ref": successor["record_ref"]})
        return results


def _dedupe_refs(refs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        key = _canonical(ref)
        if key not in seen:
            result.append(deepcopy(dict(ref)))
            seen.add(key)
    return result


def manager_v2_messages(memory_json: str, compiled: CompiledEvidence) -> list[dict[str, str]]:
    system = "You are a semantic memory manager. Return one JSON object only; never invent provenance or identifiers."
    user = f"""Return {{\"events\":[...]}}. Each event has op ADD|PATCH|REPLACE, record_type fact|preference|event|plan|assistant_fact, key, semantic_status active|planned|completed, attributes, time_expression, time_evidence_id, evidence_ids, and target_ref for PATCH/REPLACE. Use one event per independent fact; one message may support multiple events. Use [] when nothing durable is present.

Rules:
- Preserve every explicit name, amount, action, relationship, and time needed to answer future questions.
- Each distinct paid amount is a separate atomic expense record. Use a stable item-specific key and attributes {{\"item\": ..., \"action\": \"purchased|paid|installed|repaired\", \"amount\": number, \"currency\": \"USD\"}}. Never use null for an explicit monetary amount, and do not put multiple prices inside a nested costs/prices object.
- If an active record already represents the same purchase or expense occurrence, PATCH that record instead of adding a duplicate. Do not merge distinct expenses merely because they share a service visit.
- In JSON, evidence_ids and time_evidence_id MUST be integers (0 for e0, 1 for e1); eN labels below are display-only. If time_expression is null, time_evidence_id must also be null. A non-null time_evidence_id must identify a message in evidence_ids that states the time expression.
- Keep the response valid JSON and include all events.

Current active memory:
{memory_json}

Evidence (display labels e0, e1...):
{compiled.text}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def manager_money_repair_messages(
    memory_json: str,
    compiled: CompiledEvidence,
    initial_response: str,
    missing_amounts: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Ask for incremental corrective events when deterministic money coverage fails."""
    missing = ", ".join(
        f"e{item['evidence_id']} contains {item['text']}" for item in missing_amounts
    )
    system = "You repair semantic memory JSON. Return one JSON object only; never invent provenance or identifiers."
    user = f"""The prior Manager response omitted explicit monetary evidence: {missing}.

Return {{"events":[...]}} containing ONLY the corrective ADD/PATCH/REPLACE event(s) needed to cover every listed amount. Do not repeat valid events from the prior response. Use PATCH or REPLACE only when target_ref identifies a Current active memory record. If correcting a prior ADD from this same batch, return ADD with the same key instead; do not use REPLACE without target_ref. For each listed amount, an event must cite its eN as an integer evidence_id and set attributes.amount to that numeric value. If Current active memory already has the same purchase, PATCH its record_ref. Do not use null for amount or currency.

Current active memory (JSON):
{memory_json}

Evidence (display labels e0, e1...):
{compiled.text}

Prior Manager response:
{initial_response}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def reconciler_v2_messages(memory_json: str) -> list[dict[str, str]]:
    """Prompt for the one global duplicate detector; it cannot edit facts."""
    return [
        {"role": "system", "content": "You are a duplicate detector. Return one JSON object only; never invent or edit facts."},
        {"role": "user", "content": f"Return {{\"duplicate_groups\":[{{\"record_refs\":[\"r-...\",\"r-...\"]}}]}}. Include a group only when records are certainly the same occurrence; otherwise return an empty array. Do not output any fields other than record_refs.\n\nCurrent records:\n{memory_json}"},
    ]


def fit_answer_v2_context(
    memory_json: str,
    raw_tail: str,
    question_date: str,
    question: str,
    tokenizer: Any,
    context_budget_tokens: int,
    answer_output_reserve_tokens: int,
) -> tuple[list[dict[str, str]] | None, str, int]:
    """Fit answer input without evicting current memory.

    Returns ``(messages, tail, tail_tokens)``; ``messages is None`` means even
    the complete current memory plus the reserved completion cannot fit.
    """
    base = answer_messages_v2(memory_json, "", question_date, question)
    base_tokens = sum(tokenizer.count(message["content"]) for message in base)
    available = context_budget_tokens - answer_output_reserve_tokens - base_tokens
    if available < 0:
        return None, "", 0
    encoded = tokenizer.encode(raw_tail) if raw_tail else []
    if len(encoded) > available:
        tail = tokenizer.decode(encoded[-available:]) if available else ""
    else:
        tail = raw_tail
    return answer_messages_v2(memory_json, tail, question_date, question), tail, tokenizer.count(tail)


def answer_messages_v2(memory_json: str, raw_tail: str, question_date: str, question: str) -> list[dict[str, str]]:
    """Answer projection that exposes only current V2 records, never audit fields."""
    return answer_messages(memory_json, raw_tail, question_date, question)


def state_sha256_v2(state: V2MemoryState) -> str:
    return sha256_text(state.to_json())


# Friendly aliases for callers that describe the plan's nouns directly.
EvidenceCompiler = compile_evidence
V2State = V2MemoryState
parse_v2_response = parse_manager_response
