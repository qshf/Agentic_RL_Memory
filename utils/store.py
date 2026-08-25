"""记忆实验共用的 SQLite 轨迹库。

两个实验共用 run/sample/call 表；``states`` 与 ``sidecar_*`` 表是在同一数据库上的
实验专用审计扩展。因此单次运行无需复制源历史，也能统一检查和导出。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .client import CallResult

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_NOT_RUNNABLE = "not_runnable"

SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id             TEXT PRIMARY KEY,
    created_at         REAL NOT NULL,
    method             TEXT NOT NULL,
    model              TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    config_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                TEXT NOT NULL REFERENCES runs(run_id),
    question_id           TEXT NOT NULL,
    attempt               INTEGER NOT NULL,
    dataset_index         INTEGER,
    question_type         TEXT,
    config_fingerprint    TEXT NOT NULL,
    status                TEXT NOT NULL,
    started_at            REAL NOT NULL,
    finished_at           REAL,
    full_history_tokens   INTEGER DEFAULT 0,
    compression_count    INTEGER DEFAULT 0,
    summary_tokens        INTEGER DEFAULT 0,
    raw_tail_tokens       INTEGER DEFAULT 0,
    answer_input_tokens   INTEGER DEFAULT 0,
    answer_output_tokens  INTEGER DEFAULT 0,
    total_input_tokens    INTEGER DEFAULT 0,
    total_output_tokens   INTEGER DEFAULT 0,
    call_count            INTEGER DEFAULT 0,
    latency_ms            INTEGER DEFAULT 0,
    hypothesis            TEXT,
    error                 TEXT,
    UNIQUE (run_id, question_id, attempt)
);

CREATE TABLE IF NOT EXISTS calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       INTEGER NOT NULL REFERENCES samples(id),
    call_ordinal    INTEGER NOT NULL,
    attempt         INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      REAL NOT NULL,
    response_text   TEXT,
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    latency_ms      INTEGER DEFAULT 0,
    error           TEXT,
    UNIQUE (sample_id, call_ordinal, attempt)
);

CREATE TABLE IF NOT EXISTS states (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       INTEGER NOT NULL REFERENCES samples(id),
    step_ordinal    INTEGER NOT NULL,
    event           TEXT NOT NULL,
    created_at      REAL NOT NULL,
    summary_text    TEXT,
    raw_text        TEXT,
    summary_tokens  INTEGER NOT NULL,
    raw_tail_tokens INTEGER NOT NULL,
    history_tokens  INTEGER NOT NULL,
    detail          TEXT,
    UNIQUE (sample_id, step_ordinal)
);

CREATE INDEX IF NOT EXISTS idx_samples_run ON samples(run_id, question_id);
CREATE INDEX IF NOT EXISTS idx_calls_sample ON calls(sample_id, call_ordinal);
CREATE INDEX IF NOT EXISTS idx_states_sample ON states(sample_id, step_ordinal);

CREATE TABLE IF NOT EXISTS sidecar_events (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id                INTEGER NOT NULL REFERENCES samples(id),
    event_ordinal            INTEGER NOT NULL,
    chunk_start_ordinal      INTEGER NOT NULL,
    chunk_end_ordinal        INTEGER NOT NULL,
    source_unit_ordinals     TEXT NOT NULL,
    input_text               TEXT NOT NULL,
    memory_before_json       TEXT NOT NULL,
    raw_response             TEXT,
    parse_status              TEXT NOT NULL,
    parsed_event_json        TEXT,
    route_status              TEXT NOT NULL,
    route_result_json        TEXT,
    error                    TEXT,
    created_at               REAL NOT NULL,
    UNIQUE (sample_id, event_ordinal)
);

CREATE TABLE IF NOT EXISTS sidecar_states (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id                INTEGER NOT NULL REFERENCES samples(id),
    event_ordinal            INTEGER NOT NULL,
    state_json               TEXT NOT NULL,
    state_sha256             TEXT NOT NULL,
    active_record_count      INTEGER NOT NULL,
    created_at               REAL NOT NULL,
    UNIQUE (sample_id, event_ordinal)
);

CREATE TABLE IF NOT EXISTS sidecar_context (
    sample_id                 INTEGER PRIMARY KEY REFERENCES samples(id),
    baseline_sample_id        INTEGER NOT NULL,
    baseline_final_step      INTEGER NOT NULL,
    baseline_compression_step INTEGER,
    baseline_cut_index       INTEGER NOT NULL,
    raw_tail_sha256          TEXT NOT NULL,
    raw_tail_tokens          INTEGER NOT NULL,
    raw_tail_full_tokens     INTEGER NOT NULL,
    raw_tail_trimmed         INTEGER NOT NULL,
    tail_source_ordinals     TEXT NOT NULL,
    created_at                REAL NOT NULL
);

-- 每个 sample 一份独立的长期记忆库；prompt 可以裁剪，事实记录不能丢。
CREATE TABLE IF NOT EXISTS sidecar_memory (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id                INTEGER NOT NULL REFERENCES samples(id),
    event_id                 TEXT NOT NULL,
    event_ordinal            INTEGER NOT NULL,
    memory_type              TEXT NOT NULL,
    key                      TEXT NOT NULL,
    value_json               TEXT NOT NULL,
    status                   TEXT NOT NULL,
    event_date               TEXT,
    source_json              TEXT NOT NULL,
    source_unit_ordinals     TEXT NOT NULL,
    confidence               REAL,
    qualifier                TEXT,
    superseded_by            TEXT,
    created_at               REAL NOT NULL,
    UNIQUE (sample_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_sidecar_events_sample ON sidecar_events(sample_id, event_ordinal);
CREATE INDEX IF NOT EXISTS idx_sidecar_states_sample ON sidecar_states(sample_id, event_ordinal);
CREATE INDEX IF NOT EXISTS idx_sidecar_context_baseline ON sidecar_context(baseline_sample_id);
CREATE INDEX IF NOT EXISTS idx_sidecar_memory_sample_key ON sidecar_memory(sample_id, key, status);

-- Memory Sidecar V2 uses immutable version rows and separates a model batch from
-- the individual candidate items returned in its events array.
CREATE TABLE IF NOT EXISTS sidecar_batches (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id                  INTEGER NOT NULL REFERENCES samples(id),
    batch_ordinal              INTEGER NOT NULL,
    source_unit_ordinals_json  TEXT NOT NULL,
    input_text                 TEXT NOT NULL,
    memory_before_json         TEXT NOT NULL,
    raw_response               TEXT,
    parse_status               TEXT NOT NULL,
    created_at                 REAL NOT NULL,
    UNIQUE(sample_id, batch_ordinal)
);

CREATE TABLE IF NOT EXISTS sidecar_event_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id            INTEGER NOT NULL REFERENCES sidecar_batches(id),
    item_ordinal        INTEGER NOT NULL,
    model_event_json    TEXT NOT NULL,
    parse_status        TEXT NOT NULL,
    route_status        TEXT NOT NULL,
    route_result_json   TEXT,
    created_record_ref  TEXT,
    created_at          REAL NOT NULL,
    UNIQUE(batch_id, item_ordinal)
);

CREATE TABLE IF NOT EXISTS sidecar_memory_v2 (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id                    INTEGER NOT NULL REFERENCES samples(id),
    record_ref                   TEXT NOT NULL,
    created_by_event_item_id     INTEGER REFERENCES sidecar_event_items(id),
    created_by_reconciliation_item_id INTEGER,
    prior_record_ref             TEXT,
    key                          TEXT NOT NULL,
    record_type                  TEXT NOT NULL,
    attributes_json              TEXT NOT NULL,
    temporal_json                TEXT NOT NULL,
    source_refs_json             TEXT NOT NULL,
    field_provenance_json        TEXT NOT NULL,
    semantic_status              TEXT NOT NULL,
    lifecycle                    TEXT NOT NULL,
    superseded_by_record_ref     TEXT,
    created_at                   REAL NOT NULL,
    UNIQUE(sample_id, record_ref)
);

CREATE TABLE IF NOT EXISTS sidecar_states_v2 (
    sample_id       INTEGER NOT NULL REFERENCES samples(id),
    batch_ordinal   INTEGER NOT NULL,
    state_json      TEXT NOT NULL,
    state_sha256    TEXT NOT NULL,
    created_at      REAL NOT NULL,
    PRIMARY KEY(sample_id, batch_ordinal)
);

CREATE TABLE IF NOT EXISTS sidecar_reconciliation_batches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id           INTEGER NOT NULL REFERENCES samples(id),
    memory_before_json  TEXT NOT NULL,
    raw_response        TEXT,
    parse_status        TEXT NOT NULL,
    state_after_json    TEXT,
    state_after_sha256  TEXT,
    created_at          REAL NOT NULL,
    UNIQUE(sample_id)
);

CREATE TABLE IF NOT EXISTS sidecar_reconciliation_items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    reconciliation_batch_id INTEGER NOT NULL REFERENCES sidecar_reconciliation_batches(id),
    group_ordinal         INTEGER NOT NULL,
    model_group_json      TEXT NOT NULL,
    route_status           TEXT NOT NULL,
    route_result_json     TEXT,
    created_record_ref    TEXT,
    created_at            REAL NOT NULL,
    UNIQUE(reconciliation_batch_id, group_ordinal)
);
"""


class TrajectoryStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(states)")}
        if "raw_tail_text" in columns and "raw_text" not in columns:
            self.conn.execute("ALTER TABLE states RENAME COLUMN raw_tail_text TO raw_text")
        v2_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(sidecar_memory_v2)")}
        if v2_columns and "source_refs_json" not in v2_columns:
            self.conn.execute("ALTER TABLE sidecar_memory_v2 ADD COLUMN source_refs_json TEXT NOT NULL DEFAULT '[]'")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def start_run(self, run_id: str, config: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO runs (run_id, created_at, method, model, config_fingerprint, config_json) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET config_json=excluded.config_json",
                (
                    run_id,
                    time.time(),
                    config["method"],
                    config["model"]["model"],
                    config["config_fingerprint"],
                    json.dumps(config, ensure_ascii=False, sort_keys=True),
                ),
            )

    def completed_question_ids(self, run_id: str, config_fingerprint: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT question_id FROM samples WHERE run_id=? AND config_fingerprint=? "
            "AND status IN (?,?)",
            (run_id, config_fingerprint, STATUS_COMPLETED, STATUS_NOT_RUNNABLE),
        ).fetchall()
        return {row["question_id"] for row in rows}

    def start_sample(
        self,
        run_id: str,
        question_id: str,
        *,
        dataset_index: int | None,
        question_type: str | None,
        config_fingerprint: str,
        code_version: str,
    ) -> int:
        del code_version
        with self.conn:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) AS n FROM samples WHERE run_id=? AND question_id=?",
                (run_id, question_id),
            ).fetchone()
            cursor = self.conn.execute(
                "INSERT INTO samples (run_id, question_id, attempt, dataset_index, question_type, "
                "config_fingerprint, status, started_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    question_id,
                    row["n"] + 1,
                    dataset_index,
                    question_type,
                    config_fingerprint,
                    STATUS_RUNNING,
                    time.time(),
                ),
            )
        return int(cursor.lastrowid)

    def finish_sample(self, sample_id: int, status: str, **fields: Any) -> None:
        if not all(name.isidentifier() for name in fields):
            raise ValueError(f"invalid column names: {sorted(fields)}")
        columns = ", ".join(f"{name}=?" for name in fields)
        with self.conn:
            self.conn.execute(
                f"UPDATE samples SET status=?, finished_at=?{', ' + columns if columns else ''} WHERE id=?",
                (status, time.time(), *fields.values(), sample_id),
            )

    def record_call(
        self,
        sample_id: int,
        *,
        call_ordinal: int,
        attempt: int,
        kind: str,
        status: str,
        request_params: dict[str, Any],
        prompt_tokens_estimated: int | None = None,
        result: CallResult | None = None,
        error: str | None = None,
        parent_unit_ordinal: int | None = None,
        source_unit_ordinals: Iterable[int] | None = None,
    ) -> int:
        del request_params, prompt_tokens_estimated, parent_unit_ordinal, source_unit_ordinals
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO calls (sample_id, call_ordinal, attempt, kind, status, created_at, "
                "response_text, input_tokens, output_tokens, latency_ms, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sample_id,
                    call_ordinal,
                    attempt,
                    kind,
                    status,
                    time.time(),
                    result.content if result else None,
                    result.input_tokens if result else 0,
                    result.output_tokens if result else 0,
                    result.latency_ms if result else 0,
                    error,
                ),
            )
        return int(cursor.lastrowid)

    def record_state(
        self,
        sample_id: int,
        *,
        step_ordinal: int,
        parent_step_id: int | None,
        event: str,
        state: Any,
        summary_text: str | None,
        raw_text: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> int:
        del parent_step_id
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO states (sample_id, step_ordinal, event, created_at, summary_text, "
                "raw_text, summary_tokens, raw_tail_tokens, history_tokens, detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    sample_id,
                    step_ordinal,
                    event,
                    time.time(),
                    summary_text,
                    raw_text,
                    state.summary_tokens,
                    state.tail_tokens,
                    state.history_tokens,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None,
                ),
            )
        return int(cursor.lastrowid)

    def record_sidecar_event(
        self,
        sample_id: int,
        *,
        event_ordinal: int,
        chunk_start_ordinal: int,
        chunk_end_ordinal: int,
        source_unit_ordinals: Iterable[int],
        input_text: str,
        memory_before_json: str,
        raw_response: str | None,
        parse_status: str,
        parsed_event: dict[str, Any] | None,
        route_status: str,
        route_result: dict[str, Any] | None,
        error: str | None = None,
    ) -> int:
        """持久化一次 manager 输出及对应的确定性路由结果。"""
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO sidecar_events (sample_id, event_ordinal, chunk_start_ordinal, "
                "chunk_end_ordinal, source_unit_ordinals, input_text, memory_before_json, "
                "raw_response, parse_status, parsed_event_json, route_status, route_result_json, "
                "error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sample_id,
                    event_ordinal,
                    chunk_start_ordinal,
                    chunk_end_ordinal,
                    json.dumps(list(source_unit_ordinals), ensure_ascii=False),
                    input_text,
                    memory_before_json,
                    raw_response,
                    parse_status,
                    json.dumps(parsed_event, ensure_ascii=False, sort_keys=True) if parsed_event else None,
                    route_status,
                    json.dumps(route_result, ensure_ascii=False, sort_keys=True) if route_result else None,
                    error,
                    time.time(),
                ),
            )
        return int(cursor.lastrowid)

    def record_sidecar_state(
        self,
        sample_id: int,
        *,
        event_ordinal: int,
        state_json: str,
        state_sha256: str,
        active_record_count: int,
    ) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO sidecar_states (sample_id, event_ordinal, state_json, state_sha256, "
                "active_record_count, created_at) VALUES (?,?,?,?,?,?)",
                (sample_id, event_ordinal, state_json, state_sha256, active_record_count, time.time()),
            )
        return int(cursor.lastrowid)

    def record_sidecar_context(
        self,
        sample_id: int,
        *,
        baseline_sample_id: int,
        baseline_final_step: int,
        baseline_compression_step: int | None,
        baseline_cut_index: int,
        raw_tail_sha256: str,
        raw_tail_tokens: int,
        raw_tail_full_tokens: int,
        raw_tail_trimmed: bool,
        tail_source_ordinals: Iterable[int],
    ) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO sidecar_context (sample_id, baseline_sample_id, baseline_final_step, "
                "baseline_compression_step, baseline_cut_index, raw_tail_sha256, raw_tail_tokens, "
                "raw_tail_full_tokens, raw_tail_trimmed, tail_source_ordinals, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sample_id,
                    baseline_sample_id,
                    baseline_final_step,
                    baseline_compression_step,
                    baseline_cut_index,
                    raw_tail_sha256,
                    raw_tail_tokens,
                    raw_tail_full_tokens,
                    int(raw_tail_trimmed),
                    json.dumps(list(tail_source_ordinals), ensure_ascii=False),
                    time.time(),
                ),
            )
        return int(cursor.lastrowid)

    def sync_sidecar_memory(self, sample_id: int, records: Iterable[dict[str, Any]]) -> None:
        """将完整 MemoryState 同步到该 sample 的长期记忆库。

        prompt 侧可以只携带窗口内记录；这里按 event_id upsert，确保旧 key 即使不在
        当前 prompt 中，也能被数据库识别并保留更新关系。
        """
        with self.conn:
            for record in records:
                event_id = str(record["event_id"])
                try:
                    event_ordinal = int(event_id.rsplit("-", 1)[-1])
                except ValueError:
                    event_ordinal = 0
                self.conn.execute(
                    "INSERT INTO sidecar_memory (sample_id, event_id, event_ordinal, memory_type, key, "
                    "value_json, status, event_date, source_json, source_unit_ordinals, confidence, "
                    "qualifier, superseded_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(sample_id, event_id) DO UPDATE SET status=excluded.status, "
                    "superseded_by=excluded.superseded_by",
                    (
                        sample_id,
                        event_id,
                        event_ordinal,
                        str(record.get("memory_type", "fact")),
                        str(record["key"]),
                        json.dumps(record.get("value"), ensure_ascii=False, sort_keys=True),
                        str(record.get("status", "active")),
                        record.get("event_date"),
                        json.dumps(record.get("source") or {}, ensure_ascii=False, sort_keys=True),
                        json.dumps(record.get("source_unit_ordinals") or [], ensure_ascii=False),
                        record.get("confidence"),
                        record.get("qualifier"),
                        record.get("superseded_by"),
                        time.time(),
                    ),
                )

    def load_sidecar_memory(self, sample_id: int) -> list[dict[str, Any]]:
        """从该 sample 的长期记忆表恢复完整追加式 records。"""
        rows = self.conn.execute(
            "SELECT event_id, memory_type, key, value_json, status, event_date, source_json, "
            "source_unit_ordinals, confidence, qualifier, superseded_by "
            "FROM sidecar_memory WHERE sample_id=? ORDER BY event_ordinal, id",
            (sample_id,),
        ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            record: dict[str, Any] = {
                "event_id": row["event_id"],
                "memory_type": row["memory_type"],
                "key": row["key"],
                "value": json.loads(row["value_json"]),
                "status": row["status"],
                "event_date": row["event_date"],
                "source": json.loads(row["source_json"]),
                "source_unit_ordinals": json.loads(row["source_unit_ordinals"]),
                "confidence": row["confidence"],
            }
            if row["qualifier"] is not None:
                record["qualifier"] = row["qualifier"]
            if row["superseded_by"] is not None:
                record["superseded_by"] = row["superseded_by"]
            records.append(record)
        return records

    # V2 persistence -----------------------------------------------------
    def record_sidecar_batch(
        self,
        sample_id: int,
        *,
        batch_ordinal: int,
        source_unit_ordinals: Iterable[int],
        input_text: str,
        memory_before_json: str,
        raw_response: str | None,
        parse_status: str,
    ) -> int:
        """Persist one manager call (the container for multiple event items)."""
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO sidecar_batches (sample_id,batch_ordinal,source_unit_ordinals_json,input_text,"
                "memory_before_json,raw_response,parse_status,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (sample_id, batch_ordinal, json.dumps(list(source_unit_ordinals)), input_text,
                 memory_before_json, raw_response, parse_status, time.time()),
            )
        return int(cursor.lastrowid)

    def record_sidecar_event_item(
        self,
        batch_id: int,
        *,
        item_ordinal: int,
        model_event: Any,
        parse_status: str,
        route_status: str,
        route_result: Any = None,
        created_record_ref: str | None = None,
    ) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO sidecar_event_items (batch_id,item_ordinal,model_event_json,parse_status,"
                "route_status,route_result_json,created_record_ref,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (batch_id, item_ordinal, json.dumps(model_event, ensure_ascii=False, sort_keys=True),
                 parse_status, route_status,
                 json.dumps(route_result, ensure_ascii=False, sort_keys=True) if route_result is not None else None,
                 created_record_ref, time.time()),
            )
        return int(cursor.lastrowid)

    def sync_sidecar_memory_v2(self, sample_id: int, records: Iterable[dict[str, Any]]) -> None:
        """Upsert immutable V2 versions without deleting superseded history."""
        with self.conn:
            for record in records:
                self.conn.execute(
                    "INSERT INTO sidecar_memory_v2 (sample_id,record_ref,created_by_event_item_id,"
                    "created_by_reconciliation_item_id,prior_record_ref,key,record_type,attributes_json,"
                    "temporal_json,source_refs_json,field_provenance_json,semantic_status,lifecycle,superseded_by_record_ref,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sample_id,record_ref) DO UPDATE SET "
                    "lifecycle=excluded.lifecycle,superseded_by_record_ref=excluded.superseded_by_record_ref",
                    (sample_id, record["record_ref"], record.get("created_by_event_item_id"),
                     record.get("created_by_reconciliation_item_id"), record.get("prior_record_ref"),
                     record["key"], record["record_type"], json.dumps(record.get("attributes", {}), ensure_ascii=False, sort_keys=True),
                     json.dumps(record.get("temporal", {}), ensure_ascii=False, sort_keys=True),
                     json.dumps(record.get("source_refs", []), ensure_ascii=False, sort_keys=True),
                     json.dumps(record.get("field_provenance", {}), ensure_ascii=False, sort_keys=True),
                     record["semantic_status"], record["lifecycle"], record.get("superseded_by_record_ref"), time.time()),
                )

    def load_sidecar_memory_v2(self, sample_id: int, *, current_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM sidecar_memory_v2 WHERE sample_id=?"
        params: list[Any] = [sample_id]
        if current_only:
            query += " AND lifecycle='current'"
        query += " ORDER BY id"
        rows = self.conn.execute(query, params).fetchall()
        return [{
            "record_ref": row["record_ref"], "prior_record_ref": row["prior_record_ref"],
            "key": row["key"], "record_type": row["record_type"],
            "attributes": json.loads(row["attributes_json"]), "temporal": json.loads(row["temporal_json"]),
            "source_refs": json.loads(row["source_refs_json"]),
            "field_provenance": json.loads(row["field_provenance_json"]),
            "semantic_status": row["semantic_status"], "lifecycle": row["lifecycle"],
            "superseded_by_record_ref": row["superseded_by_record_ref"],
        } for row in rows]

    def record_sidecar_state_v2(self, sample_id: int, *, batch_ordinal: int, state_json: str, state_sha256: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO sidecar_states_v2 (sample_id,batch_ordinal,state_json,state_sha256,created_at) VALUES (?,?,?,?,?)",
                (sample_id, batch_ordinal, state_json, state_sha256, time.time()),
            )

    def record_sidecar_reconciliation_batch(
        self, sample_id: int, *, memory_before_json: str, raw_response: str | None,
        parse_status: str, state_after_json: str | None = None, state_after_sha256: str | None = None,
    ) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO sidecar_reconciliation_batches (sample_id,memory_before_json,raw_response,"
                "parse_status,state_after_json,state_after_sha256,created_at) VALUES (?,?,?,?,?,?,?)",
                (sample_id, memory_before_json, raw_response, parse_status, state_after_json, state_after_sha256, time.time()),
            )
        return int(cursor.lastrowid)

    def record_sidecar_reconciliation_item(
        self, batch_id: int, *, group_ordinal: int, model_group: Any,
        route_status: str, route_result: Any = None, created_record_ref: str | None = None,
    ) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO sidecar_reconciliation_items (reconciliation_batch_id,group_ordinal,"
                "model_group_json,route_status,route_result_json,created_record_ref,created_at) VALUES (?,?,?,?,?,?,?)",
                (batch_id, group_ordinal, json.dumps(model_group, ensure_ascii=False, sort_keys=True), route_status,
                 json.dumps(route_result, ensure_ascii=False, sort_keys=True) if route_result is not None else None,
                 created_record_ref, time.time()),
            )
        return int(cursor.lastrowid)

    def export_hypotheses(self, run_id: str, output: Path) -> int:
        rows = self.conn.execute(
            "SELECT question_id, hypothesis FROM samples WHERE run_id=? AND status=? "
            "AND id IN (SELECT MAX(id) FROM samples WHERE run_id=? AND status=? GROUP BY question_id) "
            "ORDER BY id",
            (run_id, STATUS_COMPLETED, run_id, STATUS_COMPLETED),
        ).fetchall()
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as file:
            for row in rows:
                file.write(json.dumps({"question_id": row["question_id"], "hypothesis": row["hypothesis"] or ""}, ensure_ascii=False) + "\n")
        return len(rows)

    def run_statistics(self, run_id: str) -> dict[str, Any]:
        latest = (
            "SELECT * FROM samples WHERE run_id=? AND id IN "
            "(SELECT MAX(id) FROM samples WHERE run_id=? GROUP BY question_id)"
        )
        rows = self.conn.execute(latest, (run_id, run_id)).fetchall()
        by_status: dict[str, int] = {}
        for row in rows:
            by_status[row["status"]] = by_status.get(row["status"], 0) + 1

        def total(column: str) -> int:
            return sum(row[column] or 0 for row in rows)

        latencies = sorted(row["latency_ms"] for row in rows)
        return {
            "sample_count": len(rows),
            "status_counts": by_status,
            "total_input_tokens": total("total_input_tokens"),
            "total_output_tokens": total("total_output_tokens"),
            "total_calls": total("call_count"),
            "total_compressions": total("compression_count"),
            "latency_ms": {
                "total": sum(latencies),
                "median": latencies[len(latencies) // 2] if latencies else 0,
                "max": latencies[-1] if latencies else 0,
            },
        }
