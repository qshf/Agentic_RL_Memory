"""审计 Sidecar 上下文缺失样本的消息、事件路由和最终记忆 provenance。

脚本只读两份 SQLite 和官方数据集，不调用模型、不修改数据库。它把每个重点事实
对应到原始 user 消息、Sidecar manager chunk、解析事件、规则路由结果和最终 memory
记录，便于在修改 memory schema 或 manager 协议前复核证据链。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any


FAILURE_SAMPLES = {
    "gpt4_d84a3211": [r"helmet.*\$120|\$120.*helmet"],
    "bf659f65": [r"Happier Than Ever.*downloaded", r"Tame Impala.*vinyl", r"Whiskey Wanderers.*EP"],
    "ccb36322": [r"Spotify"],
    "3a704032": [r"peace lily|succulent", r"snake plant"],
    "dd2973ad": [r"doctor.*appointment|appointment.*doctor", r"2 ?AM|2 ?am"],
    "gpt4_d6585ce9": [r"Queen.*parents|parents.*Queen"],
    "81507db6": [r"graduation"],
    "6cb6f249": [r"10-day|week-long.*break|break.*social media"],
}

DEFAULT_SOURCE = Path("data/official_longmemeval/longmemeval_s_cleaned.json")
DEFAULT_SIDECAR_DB = Path("results/memory_sidecar/sidecar-strong-pilot-24-v1-c2-selected/trajectory.sqlite3")
DEFAULT_BASELINE_DB = Path("results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/trajectory.sqlite3")
DEFAULT_OUTPUT = Path("results/memory_sidecar/sidecar-strong-pilot-24-v1-c2-selected/context_failure_audit.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--sidecar-db", type=Path, default=DEFAULT_SIDECAR_DB)
    parser.add_argument("--baseline-db", type=Path, default=DEFAULT_BASELINE_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def rows_by_question(source_path: Path) -> dict[str, dict[str, Any]]:
    rows = json.loads(source_path.read_text(encoding="utf-8"))
    return {row["question_id"]: row for row in rows}


def json_value(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def source_messages(row: dict[str, Any], patterns: list[str]) -> list[dict[str, Any]]:
    result = []
    for session_id, session in zip(row["haystack_session_ids"], row["haystack_sessions"]):
        for message_index, message in enumerate(session):
            if message.get("role") != "user":
                continue
            if any(re.search(pattern, message.get("content", ""), re.IGNORECASE) for pattern in patterns):
                result.append({
                    "session_id": session_id,
                    "message_index": message_index,
                    "role": message.get("role"),
                    "content": message.get("content", ""),
                    "matched_patterns": [
                        pattern for pattern in patterns
                        if re.search(pattern, message.get("content", ""), re.IGNORECASE)
                    ],
                })
    return result


def locate_baseline_states(
    connection: sqlite3.Connection,
    baseline_sample_id: int,
    messages: list[dict[str, Any]],
) -> None:
    states = connection.execute(
        "SELECT step_ordinal, raw_text FROM states "
        "WHERE sample_id=? AND event='ingest' ORDER BY step_ordinal",
        (baseline_sample_id,),
    ).fetchall()
    for message in messages:
        content = message["content"].strip().lower()
        # canonical raw_text contains the complete message; a short prefix is enough
        # to tolerate renderer-added session headers.
        prefix = content[:120]
        matches = [
            int(step) for step, raw_text in states
            if prefix in (raw_text or "").lower()
        ]
        if not matches:
            prefix = content[:60]
            matches = [
                int(step) for step, raw_text in states
                if prefix in (raw_text or "").lower()
            ]
        message["baseline_step_ordinals"] = matches


def matching_events(
    connection: sqlite3.Connection,
    sample_id: int,
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    events = connection.execute(
        "SELECT event_ordinal, chunk_start_ordinal, chunk_end_ordinal, source_unit_ordinals, "
        "input_text, parse_status, parsed_event_json, route_status, route_result_json, error "
        "FROM sidecar_events WHERE sample_id=? ORDER BY event_ordinal",
        (sample_id,),
    ).fetchall()
    content = message["content"].strip().lower()
    prefix = content[:100]
    located = []
    for event in events:
        source_units = json_value(event["source_unit_ordinals"], [])
        input_text = (event["input_text"] or "").lower()
        if prefix in input_text or any(
            int(step) in source_units for step in message.get("baseline_step_ordinals", [])
        ):
            parsed = json_value(event["parsed_event_json"], None)
            route = json_value(event["route_result_json"], None)
            located.append({
                "event_ordinal": int(event["event_ordinal"]),
                "chunk_start_ordinal": int(event["chunk_start_ordinal"]),
                "chunk_end_ordinal": int(event["chunk_end_ordinal"]),
                "source_unit_ordinals": source_units,
                "parse_status": event["parse_status"],
                "parsed_event": parsed,
                "route_status": event["route_status"],
                "route_result": route,
                "error": event["error"],
            })
    return located


def final_memory(connection: sqlite3.Connection, sample_id: int) -> list[dict[str, Any]]:
    records = connection.execute(
        "SELECT event_id, event_ordinal, memory_type, key, value_json, status, event_date, "
        "source_json, source_unit_ordinals, superseded_by "
        "FROM sidecar_memory WHERE sample_id=? ORDER BY event_ordinal, id",
        (sample_id,),
    ).fetchall()
    return [
        {
            "event_id": row["event_id"],
            "event_ordinal": row["event_ordinal"],
            "memory_type": row["memory_type"],
            "key": row["key"],
            "value": json_value(row["value_json"], row["value_json"]),
            "status": row["status"],
            "event_date": row["event_date"],
            "source": json_value(row["source_json"], {}),
            "source_unit_ordinals": json_value(row["source_unit_ordinals"], []),
            "superseded_by": row["superseded_by"],
        }
        for row in records
    ]


def audit(args: argparse.Namespace) -> dict[str, Any]:
    source = rows_by_question(args.source)
    sidecar = sqlite3.connect(args.sidecar_db)
    sidecar.row_factory = sqlite3.Row
    baseline = sqlite3.connect(args.baseline_db)
    baseline.row_factory = sqlite3.Row
    try:
        result = {"samples": [], "read_only": True}
        for question_id, patterns in FAILURE_SAMPLES.items():
            row = source[question_id]
            sample = sidecar.execute(
                "SELECT id, question_id, status FROM samples "
                "WHERE run_id=? AND question_id=? AND status='completed' "
                "ORDER BY attempt DESC, id DESC LIMIT 1",
                ("sidecar-strong-pilot-24-v1-c2-selected", question_id),
            ).fetchone()
            if sample is None:
                raise ValueError(f"completed Sidecar sample not found: {question_id}")
            context = sidecar.execute(
                "SELECT * FROM sidecar_context WHERE sample_id=?", (sample["id"],)
            ).fetchone()
            baseline_sample_id = int(context["baseline_sample_id"])
            messages = source_messages(row, patterns)
            locate_baseline_states(baseline, baseline_sample_id, messages)
            for message in messages:
                message["events"] = matching_events(sidecar, int(sample["id"]), message)
            result["samples"].append({
                "question_id": question_id,
                "question_type": row["question_type"],
                "question": row["question"],
                "gold": row["answer"],
                "answer_session_ids": row["answer_session_ids"],
                "sidecar_sample_id": int(sample["id"]),
                "baseline_sample_id": baseline_sample_id,
                "tail": {
                    "tokens": int(context["raw_tail_tokens"]),
                    "full_tokens": int(context["raw_tail_full_tokens"]),
                    "trimmed": bool(context["raw_tail_trimmed"]),
                    "source_ordinals": json_value(context["tail_source_ordinals"], []),
                },
                "messages": messages,
                "final_memory": final_memory(sidecar, int(sample["id"])),
            })
        return result
    finally:
        sidecar.close()
        baseline.close()


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Sidecar 上下文失败逐题数据库审计",
        "",
        "本报告由 `scripts/audit_sidecar_context_failures.py` 只读生成。每条重点消息均关联到 baseline `states.raw_text`、Sidecar `sidecar_events` 的 manager 输入与事件、规则路由结果，以及最终 `sidecar_memory`。",
        "",
    ]
    for item in report["samples"]:
        lines += [f"## {item['question_id']}", "", f"问题：{item['question']}", f"Gold：`{item['gold']}`", ""]
        tail = item["tail"]
        lines.append(f"尾部：{tail['tokens']} tokens（原始 {tail['full_tokens']}，trimmed={tail['trimmed']}），source ordinals 从 `{tail['source_ordinals'][0] if tail['source_ordinals'] else None}` 到 `{tail['source_ordinals'][-1] if tail['source_ordinals'] else None}`。")
        lines += ["", "### 原始消息与事件", ""]
        for message in item["messages"]:
            text = message["content"].replace("\n", " ")
            lines += [f"- **{message['session_id']} message[{message['message_index']}]**，baseline step `{message['baseline_step_ordinals']}`：{text}"]
            if not message["events"]:
                lines.append("  - 未匹配到 Sidecar manager event。")
            for event in message["events"]:
                parsed = event["parsed_event"] or {}
                route = event["route_result"] or {}
                lines.append(
                    f"  - event `{event['event_ordinal']}` chunk `{event['chunk_start_ordinal']}-{event['chunk_end_ordinal']}`："
                    f"`{parsed.get('action', 'PARSE_ERROR')}` `{parsed.get('key', '')}` -> route `{event['route_status']}`；"
                    f"value={json.dumps(parsed.get('value'), ensure_ascii=False)}；reason={route.get('reason', '')}"
                )
        active = [record for record in item["final_memory"] if record["status"] in {"active", "planned", "completed"}]
        lines += ["", f"### 最终 memory（active/planned/completed，共 {len(active)} 条）", ""]
        for record in active:
            lines.append(
                f"- `{record['key']}` = {json.dumps(record['value'], ensure_ascii=False)} "
                f"(event `{record['event_id']}`, source={json.dumps(record['source'], ensure_ascii=False)})"
            )
        lines += ["", "### 审计结论", "", "见上面的事件 route：重点是事实未抽取、抽取后被 `rejected_add_conflict` 拒绝，还是抽取后只保留了不完整属性。", ""]
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    report = audit(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path = args.output.with_suffix(".md")
    md_path.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"samples": len(report["samples"]), "json": str(args.output), "markdown": str(md_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
