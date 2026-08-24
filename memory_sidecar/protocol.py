"""Memory Sidecar 的结构化事件协议、校验与确定性路由。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from utils.config import sha256_text

ALLOWED_ACTIONS = frozenset({"ADD", "UPDATE", "NOOP"})
ALLOWED_MEMORY_TYPES = frozenset({"fact", "preference", "event", "plan", "assistant_fact"})
# ``superseded`` 只由 UPDATE 路由写入旧 record，不能由模型创建新 record 时指定。
ALLOWED_STATUSES = frozenset({"active", "planned", "completed"})


@dataclass(frozen=True)
class ParsedEvent:
    action: str
    memory_type: str
    key: str
    value: Any
    status: str
    event_date: str | None
    source: dict[str, Any]
    confidence: float | None
    qualifier: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result = {
            "action": self.action,
            "memory_type": self.memory_type,
            "key": self.key,
            "value": self.value,
            "status": self.status,
            "event_date": self.event_date,
            "source": self.source,
            "confidence": self.confidence,
        }
        if self.qualifier is not None:
            result["qualifier"] = self.qualifier
        return result


@dataclass
class MemoryState:
    """追加式逻辑历史，并提供物化 JSON 表示。"""

    records: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_json(cls, text: str | None) -> "MemoryState":
        if not text:
            return cls()
        value = json.loads(text)
        records = value.get("records", []) if isinstance(value, dict) else []
        if not isinstance(records, list):
            raise ValueError("memory state records must be a list")
        return cls(records=[dict(record) for record in records if isinstance(record, dict)])

    def as_dict(self) -> dict[str, Any]:
        return {"version": 1, "records": self.records}

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @property
    def active_records(self) -> list[dict[str, Any]]:
        return [record for record in self.records if record.get("status") in {"active", "planned", "completed"}]

    @staticmethod
    def _prompt_record(record: dict[str, Any]) -> dict[str, Any]:
        """将内部审计记录投影成给模型看的最小事实记录。

        event_id、confidence 和 source_unit_ordinals 用于数据库审计/路由，不参与
        Answer 或 Manager 的事实判断；source 只保留可读的 session 与消息定位。
        """
        compact: dict[str, Any] = {
            "memory_type": record.get("memory_type", "fact"),
            "key": record.get("key"),
            "value": record.get("value"),
            "status": record.get("status", "active"),
        }
        if record.get("event_date") is not None:
            compact["event_date"] = record["event_date"]
        if record.get("qualifier") is not None:
            compact["qualifier"] = record["qualifier"]
        source = record.get("source")
        if isinstance(source, dict):
            compact_source: dict[str, Any] = {}
            if source.get("session_id") is not None:
                compact_source["session_id"] = source["session_id"]
            if source.get("message_indices") is not None:
                compact_source["message_indices"] = source["message_indices"]
            if compact_source:
                compact["source"] = compact_source
        return compact

    def _prompt_records(self, records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self._prompt_record(record) for record in records]

    def render_for_answer(self) -> str:
        """渲染完整事实，但移除内部审计字段后再交给 Answer Model。"""
        return json.dumps(
            {"version": 1, "records": self._prompt_records(self.records)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def render_for_manager(self, *, max_active_records: int = 256, updates_per_key: int = 2) -> str:
        """为下一次 manager 调用渲染活跃事实和受限更新账本。

        被 supersede 的记录保留在追加式状态中，但 manager 无需每轮都读取所有历史快照。
        每个 key 仅保留最近若干版本，既展示更新方向，也不重建完整历史 prompt。
        """
        active = self.active_records[-max_active_records:] if max_active_records > 0 else []
        by_key: dict[str, list[dict[str, Any]]] = {}
        for record in self.records:
            if record.get("status") == "superseded":
                by_key.setdefault(str(record.get("key")), []).append(record)
        ledger = [record for records in by_key.values() for record in records[-updates_per_key:]]
        return json.dumps(
            {
                "version": 1,
                "active_records": self._prompt_records(active),
                "update_ledger": self._prompt_records(ledger),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def apply(self, event: ParsedEvent, *, event_id: str, source_unit_ordinals: Iterable[int]) -> dict[str, Any]:
        source_units = list(source_unit_ordinals)
        active = [record for record in self.records if record.get("key") == event.key and record.get("status") in {"active", "planned", "completed"}]
        if event.action == "NOOP":
            return {"route_status": "noop", "changed": False, "active_before": len(active), "event_id": event_id}

        if event.action == "ADD":
            duplicate = any(record.get("value") == event.value for record in active)
            if duplicate:
                return {"route_status": "deduplicated", "changed": False, "active_before": len(active), "event_id": event_id}
            if active:
                return {
                    "route_status": "rejected_add_conflict",
                    "changed": False,
                    "active_before": len(active),
                    "event_id": event_id,
                    "reason": "ADD conflicts with an active value; model must emit UPDATE",
                }

        superseded_ids: list[str] = []
        if event.action == "UPDATE":
            for record in active:
                record["status"] = "superseded"
                record["superseded_by"] = event_id
                superseded_ids.append(str(record.get("event_id")))

        if event.action in {"ADD", "UPDATE"}:
            record = {
                "event_id": event_id,
                "key": event.key,
                "memory_type": event.memory_type,
                "value": event.value,
                "status": event.status,
                "event_date": event.event_date,
                "source": event.source,
                "source_unit_ordinals": source_units,
                "confidence": event.confidence,
            }
            if event.qualifier is not None:
                record["qualifier"] = event.qualifier
            self.records.append(record)
        return {
            "route_status": "applied",
            "changed": True,
            "active_before": len(active),
            "event_id": event_id,
            "superseded_event_ids": superseded_ids,
            "active_after": sum(record.get("status") in {"active", "planned", "completed"} for record in self.records),
        }


def manager_messages(memory_state: str, chunk_text: str, source_units: list[int]) -> list[dict[str, str]]:
    system = (
        "You are a structured memory controller. Extract durable facts from the current "
        "conversation chunk and update the supplied memory state. Do not answer a final "
        "question, do not invent facts, and return one JSON object only."
    )
    user = f"""Return exactly one JSON object with this schema:
{{"action":"ADD|UPDATE|NOOP","memory_type":"fact|preference|event|plan|assistant_fact","key":"stable.entity.attribute","value":"exact value","status":"active|planned|completed","event_date":"date or null","source":{{"session_id":"...","message_indices":[0]}},"confidence":0.0,"qualifier":"scope or null"}}

Rules:
- Preserve exact names, numbers, prices, dates, times, relationships, and qualifiers.
- Use UPDATE when the same key has a newer or incompatible value; do not emit ADD for a conflicting active key.
- Store user preferences, completed events, plans, and concrete assistant-provided facts when they can answer a future question.
- Use NOOP for greetings, filler, repeated confirmations, and generic content with no durable value.
- `source.message_indices` must refer to the supplied source unit ordinals when possible.
- Keep the value concise but exact. Do not include explanations outside the JSON object.

# Current active memory plus recent update ledger
{memory_state or '{"version":1,"records":[]}' }

# Current chunk, source units {source_units}
{chunk_text}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def answer_messages(memory_state: str, raw_tail: str, question_date: str, question: str) -> list[dict[str, str]]:
    system = (
        "You answer a question using structured memory and recent conversation history. "
        "Return only the concise answer, with no preamble."
    )
    user = f"""# Structured memory
{memory_state or '{"version":1,"records":[]}' }

# Recent conversation history (verbatim)
{raw_tail or '(none)'}

# Current date
{question_date}

Use only the memory and recent history above. For numeric, comparison, ranking, arithmetic,
or temporal questions, enumerate the applicable records, reconcile updates by date/status,
and then give the exact conclusion. Do not silently use superseded or planned values when
the question asks for the current/completed value. For personalization questions, apply
the stored preference only when its scope supports the request.

# Question
{question}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise ValueError("manager response did not contain a JSON object") from None
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("manager response JSON must be an object")
    return value


def parse_event(text: str, *, default_source: dict[str, Any]) -> ParsedEvent:
    value = _json_object(text)
    action = str(value.get("action", "")).upper()
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"invalid sidecar action: {action!r}")
    if action == "NOOP":
        return ParsedEvent("NOOP", "fact", "noop", None, "active", None, default_source, None)
    memory_type = str(value.get("memory_type", ""))
    if memory_type not in ALLOWED_MEMORY_TYPES:
        raise ValueError(f"invalid memory_type: {memory_type!r}")
    key = str(value.get("key", "")).strip()
    if not key or len(key) > 256:
        raise ValueError("event key must be 1..256 characters")
    if "value" not in value:
        raise ValueError("event value is required")
    status = str(value.get("status", "active"))
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"invalid event status: {status!r}")
    confidence = value.get("confidence")
    if confidence is not None:
        confidence = float(confidence)
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
    source = value.get("source")
    if not isinstance(source, dict):
        source = dict(default_source)
    return ParsedEvent(
        action=action,
        memory_type=memory_type,
        key=key,
        value=value["value"],
        status=status,
        event_date=str(value["event_date"]) if value.get("event_date") is not None else None,
        source=source,
        confidence=confidence,
        qualifier=str(value["qualifier"]) if value.get("qualifier") is not None else None,
    )


def chunk_text(messages: Iterable[Any]) -> str:
    lines: list[str] = []
    previous_session: int | None = None
    for message in messages:
        if message.session_index != previous_session:
            lines.append(message.session_header)
            previous_session = message.session_index
        lines.append(message.rendered)
    return "\n".join(lines)


def state_sha256(state: MemoryState) -> str:
    return sha256_text(state.to_json())
