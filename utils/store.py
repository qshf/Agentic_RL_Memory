"""记忆实验共用的 SQLite 轨迹库。

两个实验共用 run/sample/call 表；``states`` 与 ``sidecar_*`` 表是在同一数据库上的
实验专用审计扩展。因此单次运行无需复制源历史，也能统一检查和导出。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .client import CallResult

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_NOT_RUNNABLE = "not_runnable"

_V3_EVENT_ID = re.compile(r"^b(\d+)-i(\d+)$")


def _v3_event_coordinates(event_id: str) -> tuple[int, int] | None:
    match = _V3_EVENT_ID.fullmatch(event_id)
    return (int(match.group(1)), int(match.group(2))) if match else None

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
    batch_ordinal            INTEGER,
    item_ordinal             INTEGER,
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
    parent_chunk_ordinal       INTEGER,
    split_depth                INTEGER NOT NULL DEFAULT 0,
    split_reason               TEXT,
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

CREATE TABLE IF NOT EXISTS sidecar_v3_metrics (
    sample_id       INTEGER PRIMARY KEY REFERENCES samples(id),
    metrics_json    TEXT NOT NULL,
    created_at      REAL NOT NULL
);

-- A compaction never mutates canonical sidecar_memory.  Its complete input and
-- output are stored so an Answer-only experiment can be reproduced or audited.
CREATE TABLE IF NOT EXISTS sidecar_v3_compactions (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id              INTEGER NOT NULL REFERENCES samples(id),
    compaction_run_id      TEXT NOT NULL,
    memory_snapshot_sha256 TEXT NOT NULL,
    input_memory_json      TEXT NOT NULL,
    input_raw_tail         TEXT NOT NULL DEFAULT '',
    input_raw_tail_sha256  TEXT NOT NULL DEFAULT '',
    input_record_ids_json  TEXT NOT NULL,
    model                  TEXT NOT NULL,
    prompt_version         TEXT NOT NULL,
    raw_response           TEXT,
    parse_status           TEXT NOT NULL,
    summary_text           TEXT,
    input_tokens           INTEGER NOT NULL DEFAULT 0,
    output_tokens          INTEGER NOT NULL DEFAULT 0,
    latency_ms             INTEGER NOT NULL DEFAULT 0,
    reused_from_db         TEXT,
    reused_from_run_id     TEXT,
    created_at             REAL NOT NULL,
    UNIQUE(sample_id, compaction_run_id)
);

CREATE INDEX IF NOT EXISTS idx_sidecar_v3_compactions_lookup
    ON sidecar_v3_compactions(compaction_run_id, memory_snapshot_sha256);

-- V4 stores graph edges and the minimal-claim normalization audit separately
-- from V3 flat records. A batch is the atomic unit for replay and idempotency.
CREATE TABLE IF NOT EXISTS sidecar_v4_batches (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id                 INTEGER NOT NULL REFERENCES samples(id),
    batch_ordinal             INTEGER NOT NULL,
    input_hash                TEXT NOT NULL,
    source_unit_ordinals_json TEXT NOT NULL,
    input_text                TEXT NOT NULL,
    memory_before_json        TEXT NOT NULL,
    raw_response              TEXT,
    parse_status              TEXT NOT NULL,
    created_at                REAL NOT NULL,
    UNIQUE(sample_id, batch_ordinal),
    UNIQUE(sample_id, input_hash)
);

CREATE TABLE IF NOT EXISTS sidecar_v4_claims (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id                   INTEGER NOT NULL REFERENCES sidecar_v4_batches(id),
    claim_ordinal              INTEGER NOT NULL,
    claim_id                   TEXT NOT NULL,
    model_claim_json           TEXT NOT NULL,
    normalized_claim_json      TEXT NOT NULL,
    parse_status               TEXT NOT NULL,
    route_status               TEXT,
    route_result_json          TEXT,
    source_refs_json           TEXT NOT NULL,
    normalization_actions_json TEXT NOT NULL,
    created_at                 REAL NOT NULL,
    UNIQUE(batch_id, claim_ordinal),
    UNIQUE(batch_id, claim_id)
);

CREATE TABLE IF NOT EXISTS sidecar_v4_edges (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id                  INTEGER NOT NULL REFERENCES samples(id),
    edge_id                    TEXT NOT NULL,
    edge_key                   TEXT NOT NULL,
    claim_id                   TEXT NOT NULL,
    subject                    TEXT NOT NULL,
    predicate                  TEXT NOT NULL,
    object                     TEXT NOT NULL,
    object_type                TEXT NOT NULL,
    attributes_json            TEXT NOT NULL,
    time_json                  TEXT NOT NULL,
    scope_json                 TEXT NOT NULL,
    status                     TEXT NOT NULL,
    occurrence_key             TEXT,
    functional_key             TEXT,
    source_refs_json           TEXT NOT NULL,
    normalization_actions_json TEXT NOT NULL,
    attribute_conflicts_json   TEXT NOT NULL DEFAULT '{}',
    superseded_by              TEXT,
    created_at                 REAL NOT NULL,
    UNIQUE(sample_id, edge_id),
    UNIQUE(sample_id, edge_key)
);

CREATE INDEX IF NOT EXISTS idx_sidecar_v4_edges_lookup
    ON sidecar_v4_edges(sample_id, predicate, status);

CREATE TABLE IF NOT EXISTS sidecar_v4_raw_claims (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id                  INTEGER NOT NULL REFERENCES samples(id),
    claim_id                   TEXT NOT NULL,
    parse_status               TEXT NOT NULL,
    claim_json                 TEXT NOT NULL,
    source_refs_json           TEXT NOT NULL,
    normalization_actions_json TEXT NOT NULL,
    created_at                 REAL NOT NULL,
    UNIQUE(sample_id, claim_id)
);

CREATE TABLE IF NOT EXISTS sidecar_v4_quarantine_claims (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id                  INTEGER NOT NULL REFERENCES samples(id),
    claim_id                   TEXT NOT NULL,
    parse_status               TEXT NOT NULL,
    claim_json                 TEXT NOT NULL,
    source_refs_json           TEXT NOT NULL,
    normalization_actions_json TEXT NOT NULL,
    created_at                 REAL NOT NULL,
    UNIQUE(sample_id, claim_id)
);

CREATE TABLE IF NOT EXISTS sidecar_v4_projections (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id             INTEGER NOT NULL REFERENCES samples(id),
    projection_ordinal    INTEGER NOT NULL,
    projection_kind       TEXT NOT NULL,
    question_signature    TEXT,
    filter_json           TEXT NOT NULL,
    content               TEXT NOT NULL,
    input_edge_ids_json   TEXT NOT NULL,
    graph_truncated       INTEGER NOT NULL DEFAULT 0,
    created_at            REAL NOT NULL,
    UNIQUE(sample_id, projection_ordinal)
);
"""


class TrajectoryStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # Longer timeout is required when two replay workers append audit rows
        # to the same WAL database concurrently.
        self.conn = sqlite3.connect(self.db_path, timeout=60.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=60000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(states)")}
        if "raw_tail_text" in columns and "raw_text" not in columns:
            self.conn.execute("ALTER TABLE states RENAME COLUMN raw_tail_text TO raw_text")
        v2_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(sidecar_memory_v2)")}
        if v2_columns and "source_refs_json" not in v2_columns:
            self.conn.execute("ALTER TABLE sidecar_memory_v2 ADD COLUMN source_refs_json TEXT NOT NULL DEFAULT '[]'")
        memory_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(sidecar_memory)")}
        if memory_columns and "batch_ordinal" not in memory_columns:
            self.conn.execute("ALTER TABLE sidecar_memory ADD COLUMN batch_ordinal INTEGER")
        if memory_columns and "item_ordinal" not in memory_columns:
            self.conn.execute("ALTER TABLE sidecar_memory ADD COLUMN item_ordinal INTEGER")
        batch_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(sidecar_batches)")}
        if batch_columns and "parent_chunk_ordinal" not in batch_columns:
            self.conn.execute("ALTER TABLE sidecar_batches ADD COLUMN parent_chunk_ordinal INTEGER")
        if batch_columns and "split_depth" not in batch_columns:
            self.conn.execute("ALTER TABLE sidecar_batches ADD COLUMN split_depth INTEGER NOT NULL DEFAULT 0")
        if batch_columns and "split_reason" not in batch_columns:
            self.conn.execute("ALTER TABLE sidecar_batches ADD COLUMN split_reason TEXT")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sidecar_memory_v3_order "
            "ON sidecar_memory(sample_id,batch_ordinal,item_ordinal)"
        )
        compaction_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(sidecar_v3_compactions)")}
        if compaction_columns and "input_raw_tail" not in compaction_columns:
            self.conn.execute("ALTER TABLE sidecar_v3_compactions ADD COLUMN input_raw_tail TEXT NOT NULL DEFAULT ''")
        if compaction_columns and "input_raw_tail_sha256" not in compaction_columns:
            self.conn.execute("ALTER TABLE sidecar_v3_compactions ADD COLUMN input_raw_tail_sha256 TEXT NOT NULL DEFAULT ''")
        v4_edge_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(sidecar_v4_edges)")}
        if v4_edge_columns and "attribute_conflicts_json" not in v4_edge_columns:
            self.conn.execute("ALTER TABLE sidecar_v4_edges ADD COLUMN attribute_conflicts_json TEXT NOT NULL DEFAULT '{}'")
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

    # V3 persistence -----------------------------------------------------
    def record_v3_batch(
        self,
        sample_id: int,
        *,
        batch_ordinal: int,
        source_unit_ordinals: Iterable[int],
        input_text: str,
        memory_before_json: str,
        raw_response: str | None,
        parse_status: str,
        items: Iterable[dict[str, Any]],
        records: Iterable[dict[str, Any]],
        state_json: str,
        state_sha256: str,
        parent_chunk_ordinal: int | None = None,
        split_depth: int = 0,
        split_reason: str | None = None,
    ) -> int:
        """Atomically persist one V3 manager batch and its materialised state.

        This deliberately does not reuse the V1/V2 methods above: each of those
        commits independently and would let a failed V3 batch leak partial state.
        ``items`` contains the original model JSON plus its item-local parse and
        deterministic route result.  ``records`` is the complete copied state.
        """
        item_rows = list(items)
        state_records = list(records)
        self.conn.execute("BEGIN")
        try:
            cursor = self.conn.execute(
                "INSERT INTO sidecar_batches (sample_id,batch_ordinal,source_unit_ordinals_json,input_text,"
                "memory_before_json,raw_response,parse_status,parent_chunk_ordinal,split_depth,split_reason,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sample_id, batch_ordinal, json.dumps(list(source_unit_ordinals), ensure_ascii=False), input_text,
                    memory_before_json, raw_response, parse_status, parent_chunk_ordinal, split_depth, split_reason,
                    time.time(),
                ),
            )
            batch_id = int(cursor.lastrowid)
            for item_ordinal, item in enumerate(item_rows):
                self.conn.execute(
                    "INSERT INTO sidecar_event_items (batch_id,item_ordinal,model_event_json,parse_status,"
                    "route_status,route_result_json,created_record_ref,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        batch_id, item_ordinal,
                        json.dumps(item.get("model_event", {}), ensure_ascii=False, sort_keys=True),
                        str(item.get("parse_status", "error")), str(item.get("route_status", "rejected_parse")),
                        json.dumps(item.get("route_result"), ensure_ascii=False, sort_keys=True)
                        if item.get("route_result") is not None else None,
                        None, time.time(),
                    ),
                )
            for record in state_records:
                event_id = str(record["event_id"])
                parsed = _v3_event_coordinates(event_id)
                if parsed is None:
                    raise ValueError(f"V3 event_id must be b<batch>-i<item>: {event_id!r}")
                record_batch_ordinal, item_ordinal = parsed
                self.conn.execute(
                    "INSERT INTO sidecar_memory (sample_id,event_id,event_ordinal,batch_ordinal,item_ordinal,"
                    "memory_type,key,value_json,status,event_date,source_json,source_unit_ordinals,confidence,"
                    "qualifier,superseded_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(sample_id,event_id) DO UPDATE SET status=excluded.status, "
                    "superseded_by=excluded.superseded_by",
                    (
                        sample_id, event_id, record_batch_ordinal, record_batch_ordinal, item_ordinal,
                        str(record.get("memory_type", "fact")), str(record["key"]),
                        json.dumps(record.get("value"), ensure_ascii=False, sort_keys=True),
                        str(record.get("status", "active")), record.get("event_date"),
                        json.dumps(record.get("source") or {"source_refs": []}, ensure_ascii=False, sort_keys=True),
                        json.dumps(record.get("source_unit_ordinals") or [], ensure_ascii=False), record.get("confidence"),
                        record.get("qualifier"), record.get("superseded_by"), time.time(),
                    ),
                )
            self.conn.execute(
                "INSERT INTO sidecar_states (sample_id,event_ordinal,state_json,state_sha256,active_record_count,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (sample_id, batch_ordinal, state_json, state_sha256,
                 sum(record.get("status") in {"active", "planned", "completed"} for record in state_records), time.time()),
            )
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()
        return batch_id

    def load_sidecar_memory_v3(self, sample_id: int) -> list[dict[str, Any]]:
        """Restore V3 append-only records in explicit batch/item order."""
        rows = self.conn.execute(
            "SELECT event_id,memory_type,key,value_json,status,event_date,source_json,source_unit_ordinals,"
            "confidence,qualifier,superseded_by FROM sidecar_memory WHERE sample_id=? "
            "ORDER BY batch_ordinal,item_ordinal,id",
            (sample_id,),
        ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            record: dict[str, Any] = {
                "event_id": row["event_id"], "memory_type": row["memory_type"], "key": row["key"],
                "value": json.loads(row["value_json"]), "status": row["status"], "event_date": row["event_date"],
                "source": json.loads(row["source_json"]),
                "source_unit_ordinals": json.loads(row["source_unit_ordinals"]), "confidence": row["confidence"],
            }
            if row["qualifier"] is not None:
                record["qualifier"] = row["qualifier"]
            if row["superseded_by"] is not None:
                record["superseded_by"] = row["superseded_by"]
            records.append(record)
        return records

    # V4 persistence -----------------------------------------------------
    def record_v4_batch(
        self,
        sample_id: int,
        *,
        batch_ordinal: int,
        input_hash: str,
        source_unit_ordinals: Iterable[int],
        input_text: str,
        memory_before_json: str,
        raw_response: str | None,
        parse_status: str,
        claims: Iterable[dict[str, Any]],
        edges: Iterable[dict[str, Any]],
        raw_claims: Iterable[dict[str, Any]],
        quarantine_claims: Iterable[dict[str, Any]],
    ) -> int:
        """Atomically persist one V4 batch and its complete graph snapshot."""
        claim_rows = list(claims)
        edge_rows = list(edges)
        raw_rows = list(raw_claims)
        quarantine_rows = list(quarantine_claims)
        self.conn.execute("BEGIN")
        try:
            existing = self.conn.execute(
                "SELECT id,input_hash FROM sidecar_v4_batches WHERE sample_id=? AND batch_ordinal=?",
                (sample_id, batch_ordinal),
            ).fetchone()
            if existing is not None:
                if existing["input_hash"] != input_hash:
                    raise ValueError(f"V4 batch ordinal already exists with different input hash: {sample_id}/{batch_ordinal}")
                batch_id = int(existing["id"])
            else:
                cursor = self.conn.execute(
                    "INSERT INTO sidecar_v4_batches (sample_id,batch_ordinal,input_hash,source_unit_ordinals_json,"
                    "input_text,memory_before_json,raw_response,parse_status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (sample_id, batch_ordinal, input_hash, json.dumps(list(source_unit_ordinals), ensure_ascii=False),
                     input_text, memory_before_json, raw_response, parse_status, time.time()),
                )
                batch_id = int(cursor.lastrowid)
            for claim_ordinal, claim in enumerate(claim_rows):
                self.conn.execute(
                    "INSERT INTO sidecar_v4_claims (batch_id,claim_ordinal,claim_id,model_claim_json,normalized_claim_json,"
                    "parse_status,route_status,route_result_json,source_refs_json,normalization_actions_json,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(batch_id,claim_ordinal) DO UPDATE SET "
                    "claim_id=excluded.claim_id,model_claim_json=excluded.model_claim_json,normalized_claim_json=excluded.normalized_claim_json,"
                    "parse_status=excluded.parse_status,route_status=excluded.route_status,route_result_json=excluded.route_result_json,"
                    "source_refs_json=excluded.source_refs_json,normalization_actions_json=excluded.normalization_actions_json",
                    (batch_id, claim_ordinal, str(claim.get("claim_id", f"b{batch_ordinal}-c{claim_ordinal}")),
                     json.dumps(claim.get("model_claim", {}), ensure_ascii=False, sort_keys=True),
                     json.dumps(claim.get("normalized_claim", {}), ensure_ascii=False, sort_keys=True),
                     str(claim.get("parse_status", "unknown")), claim.get("route_status"),
                     json.dumps(claim.get("route_result"), ensure_ascii=False, sort_keys=True) if claim.get("route_result") is not None else None,
                     json.dumps(claim.get("source_refs", []), ensure_ascii=False, sort_keys=True),
                     json.dumps(claim.get("normalization_actions", []), ensure_ascii=False, sort_keys=True), time.time()),
                )
            for edge in edge_rows:
                self.conn.execute(
                    "INSERT INTO sidecar_v4_edges (sample_id,edge_id,edge_key,claim_id,subject,predicate,object,object_type,"
                    "attributes_json,time_json,scope_json,status,occurrence_key,functional_key,source_refs_json,"
                    "normalization_actions_json,attribute_conflicts_json,superseded_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(sample_id,edge_id) DO UPDATE SET attributes_json=excluded.attributes_json,"
                    "time_json=excluded.time_json,scope_json=excluded.scope_json,status=excluded.status,"
                    "source_refs_json=excluded.source_refs_json,normalization_actions_json=excluded.normalization_actions_json,"
                    "attribute_conflicts_json=excluded.attribute_conflicts_json,superseded_by=excluded.superseded_by",
                    (sample_id, str(edge["edge_id"]), str(edge["edge_key"]), str(edge.get("claim_id", "")), str(edge["subject"]),
                     str(edge["predicate"]), str(edge["object"]), str(edge.get("object_type", "unknown_entity")),
                     json.dumps(edge.get("attributes", {}), ensure_ascii=False, sort_keys=True),
                     json.dumps(edge.get("time_json", {}), ensure_ascii=False, sort_keys=True),
                     json.dumps(edge.get("scope_json", {}), ensure_ascii=False, sort_keys=True), str(edge.get("status", "active")),
                     edge.get("occurrence_key"), edge.get("functional_key"),
                     json.dumps(edge.get("source_refs", []), ensure_ascii=False, sort_keys=True),
                     json.dumps(edge.get("normalization_actions", []), ensure_ascii=False, sort_keys=True),
                     json.dumps(edge.get("attribute_conflicts", {}), ensure_ascii=False, sort_keys=True),
                     edge.get("superseded_by"), time.time()),
                )
            for target, rows in (("sidecar_v4_raw_claims", raw_rows), ("sidecar_v4_quarantine_claims", quarantine_rows)):
                for claim in rows:
                    self.conn.execute(
                        f"INSERT INTO {target} (sample_id,claim_id,parse_status,claim_json,source_refs_json,normalization_actions_json,created_at) "
                        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(sample_id,claim_id) DO UPDATE SET parse_status=excluded.parse_status,"
                        "claim_json=excluded.claim_json,source_refs_json=excluded.source_refs_json,normalization_actions_json=excluded.normalization_actions_json",
                        (sample_id, str(claim.get("claim_id", "")), str(claim.get("parse_status", "unknown")),
                         json.dumps(claim.get("model_claim", claim), ensure_ascii=False, sort_keys=True),
                         json.dumps(claim.get("source_refs", []), ensure_ascii=False, sort_keys=True),
                         json.dumps(claim.get("normalization_actions", []), ensure_ascii=False, sort_keys=True), time.time()),
                    )
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()
        return batch_id

    def load_v4_edges(self, sample_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM sidecar_v4_edges WHERE sample_id=? ORDER BY id", (sample_id,)
        ).fetchall()
        return [
            {
                "edge_id": row["edge_id"], "edge_key": row["edge_key"], "claim_id": row["claim_id"],
                "subject": row["subject"], "predicate": row["predicate"], "object": row["object"],
                "object_type": row["object_type"], "attributes": json.loads(row["attributes_json"]),
                "time_json": json.loads(row["time_json"]), "scope_json": json.loads(row["scope_json"]),
                "status": row["status"], "occurrence_key": row["occurrence_key"], "functional_key": row["functional_key"],
                "source_refs": json.loads(row["source_refs_json"]),
                "normalization_actions": json.loads(row["normalization_actions_json"]),
                "attribute_conflicts": json.loads(row["attribute_conflicts_json"]),
                "superseded_by": row["superseded_by"],
            }
            for row in rows
        ]

    def record_v4_projection(
        self,
        sample_id: int,
        *,
        projection_ordinal: int,
        projection_kind: str,
        question_signature: str | None,
        filter_spec: dict[str, Any],
        content: str,
        input_edge_ids: Iterable[str],
        graph_truncated: bool = False,
    ) -> int:
        """Persist the exact graph context sent to Answer."""
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO sidecar_v4_projections (sample_id,projection_ordinal,projection_kind,question_signature,"
                "filter_json,content,input_edge_ids_json,graph_truncated,created_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(sample_id,projection_ordinal) DO UPDATE SET projection_kind=excluded.projection_kind,"
                "question_signature=excluded.question_signature,filter_json=excluded.filter_json,content=excluded.content,"
                "input_edge_ids_json=excluded.input_edge_ids_json,graph_truncated=excluded.graph_truncated",
                (sample_id, projection_ordinal, projection_kind, question_signature,
                 json.dumps(filter_spec, ensure_ascii=False, sort_keys=True), content,
                 json.dumps(list(input_edge_ids), ensure_ascii=False), int(graph_truncated), time.time()),
            )
        return int(cursor.lastrowid)

    def record_v3_metrics(self, sample_id: int, metrics: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO sidecar_v3_metrics (sample_id,metrics_json,created_at) VALUES (?,?,?) "
                "ON CONFLICT(sample_id) DO UPDATE SET metrics_json=excluded.metrics_json,created_at=excluded.created_at",
                (sample_id, json.dumps(metrics, ensure_ascii=False, sort_keys=True), time.time()),
            )

    def record_v3_compaction(
        self,
        sample_id: int,
        *,
        compaction_run_id: str,
        memory_snapshot_sha256: str,
        input_memory_json: str,
        input_raw_tail: str,
        input_raw_tail_sha256: str,
        input_record_ids: Iterable[str],
        model: str,
        prompt_version: str,
        raw_response: str | None,
        parse_status: str,
        summary_text: str | None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int = 0,
        reused_from_db: str | None = None,
        reused_from_run_id: str | None = None,
    ) -> int:
        """Persist a V3 final-summary snapshot without altering canonical memory."""
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO sidecar_v3_compactions (sample_id,compaction_run_id,memory_snapshot_sha256,"
                "input_memory_json,input_raw_tail,input_raw_tail_sha256,input_record_ids_json,model,prompt_version,"
                "raw_response,parse_status,summary_text,input_tokens,output_tokens,latency_ms,reused_from_db,"
                "reused_from_run_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(sample_id,compaction_run_id) DO UPDATE SET "
                "memory_snapshot_sha256=excluded.memory_snapshot_sha256, "
                "input_memory_json=excluded.input_memory_json, input_raw_tail=excluded.input_raw_tail, "
                "input_raw_tail_sha256=excluded.input_raw_tail_sha256, "
                "input_record_ids_json=excluded.input_record_ids_json, model=excluded.model, "
                "prompt_version=excluded.prompt_version, raw_response=excluded.raw_response, "
                "parse_status=excluded.parse_status, summary_text=excluded.summary_text, "
                "input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens, "
                "latency_ms=excluded.latency_ms, reused_from_db=excluded.reused_from_db, "
                "reused_from_run_id=excluded.reused_from_run_id, created_at=excluded.created_at",
                (
                    sample_id, compaction_run_id, memory_snapshot_sha256, input_memory_json, input_raw_tail,
                    input_raw_tail_sha256, json.dumps(list(input_record_ids), ensure_ascii=False), model,
                    prompt_version, raw_response, parse_status, summary_text, input_tokens, output_tokens,
                    latency_ms, reused_from_db, reused_from_run_id, time.time(),
                ),
            )
        return int(cursor.lastrowid)

    def load_v3_compaction(
        self, *, run_id: str, question_id: str, compaction_run_id: str,
    ) -> dict[str, Any] | None:
        """Load the latest persisted final-summary snapshot for one source sample."""
        row = self.conn.execute(
            "SELECT c.*,s.question_id,s.run_id,s.attempt FROM sidecar_v3_compactions c "
            "JOIN samples s ON s.id=c.sample_id "
            "WHERE s.run_id=? AND s.question_id=? AND c.compaction_run_id=? "
            "ORDER BY s.attempt DESC,c.id DESC LIMIT 1",
            (run_id, question_id, compaction_run_id),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["input_record_ids"] = json.loads(result.pop("input_record_ids_json"))
        return result

    def load_v3_memory_for_run(self, *, run_id: str, question_id: str) -> list[dict[str, Any]] | None:
        """Restore the latest completed V3 canonical memory for an Answer-only replay."""
        row = self.conn.execute(
            "SELECT id FROM samples WHERE run_id=? AND question_id=? AND status=? "
            "ORDER BY attempt DESC,id DESC LIMIT 1",
            (run_id, question_id, STATUS_COMPLETED),
        ).fetchone()
        return self.load_sidecar_memory_v3(int(row["id"])) if row is not None else None

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
