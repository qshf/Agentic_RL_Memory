"""Small SQLite store for the Rolling Summary demo.

The demo only needs four things: run configuration, per-question results,
model calls, and summary state snapshots. It intentionally does not maintain a
second database copy of the source history.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .client import CallResult
from .rolling import RollingState

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
    raw_tail_text   TEXT,
    summary_tokens  INTEGER NOT NULL,
    raw_tail_tokens INTEGER NOT NULL,
    history_tokens  INTEGER NOT NULL,
    detail          TEXT,
    UNIQUE (sample_id, step_ordinal)
);

CREATE INDEX IF NOT EXISTS idx_samples_run ON samples(run_id, question_id);
CREATE INDEX IF NOT EXISTS idx_calls_sample ON calls(sample_id, call_ordinal);
CREATE INDEX IF NOT EXISTS idx_states_sample ON states(sample_id, step_ordinal);
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
        state: RollingState,
        summary_text: str | None,
        detail: dict[str, Any] | None = None,
    ) -> int:
        del parent_step_id
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO states (sample_id, step_ordinal, event, created_at, summary_text, "
                "raw_tail_text, summary_tokens, raw_tail_tokens, history_tokens, detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    sample_id,
                    step_ordinal,
                    event,
                    time.time(),
                    summary_text,
                    state.render_tail(),
                    state.summary_tokens,
                    state.tail_tokens,
                    state.history_tokens,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None,
                ),
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
