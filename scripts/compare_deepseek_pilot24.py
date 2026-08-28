"""Compare rolling and structured-sidecar contexts on all 24 pilot samples.

The script follows ``compare_deepseek_context_sufficient.py`` for the DeepSeek
request protocol, but evaluates the whole pilot manifest rather than its
14-sample context-sufficient subset. Results are appended after every completed
request, so rerunning the command resumes without paying for completed calls.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_sidecar.data import load_baseline_tail
from memory_sidecar.protocol import MemoryState, answer_messages
from utils.config import sha256_text
from utils.local_tokenizer import LocalQwenTokenizer


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data" / "official_longmemeval" / "longmemeval_s_cleaned.json"
DEFAULT_MANIFEST = ROOT / "data" / "samples" / "longmemeval_s_pilot_24_from_baseline_eval_20260822.csv"
DEFAULT_ROLLING_DB = ROOT / "results" / "rolling_summary" / "rolling-summary-eval120-v1-atomic-c2" / "trajectory.sqlite3"
DEFAULT_SIDECAR_DB = ROOT / "results" / "memory_sidecar" / "sidecar-strong-pilot-24-v1-c2-selected" / "trajectory.sqlite3"
DEFAULT_OUTPUT_ROOT = ROOT / "results" / "memory_sidecar" / "sidecar-strong-pilot-24-v1-c2-selected" / "deepseek-pilot24-20260827"
DEFAULT_ROLLING_RUN_ID = "rolling-summary-eval120-v1-atomic-c2"
DEFAULT_SIDECAR_RUN_ID = "sidecar-strong-pilot-24-v1-c2-selected"
DEFAULT_MODEL = "deepseek-v4-pro"
TAIL_BUDGET_TOKENS = 16 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--rolling-db", type=Path, default=DEFAULT_ROLLING_DB)
    parser.add_argument("--rolling-run-id", default=DEFAULT_ROLLING_RUN_ID)
    parser.add_argument("--sidecar-db", type=Path, default=DEFAULT_SIDECAR_DB)
    parser.add_argument("--sidecar-run-id", default=DEFAULT_SIDECAR_RUN_ID)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--base-url", default=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL))
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--timeout", type=float, default=300.0)
    return parser.parse_args()


def selected_rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    source_rows = json.loads(args.source.read_text(encoding="utf-8"))
    source = {str(row["question_id"]): row for row in source_rows}
    with args.manifest.open(encoding="utf-8", newline="") as handle:
        ids = [str(row["question_id"]) for row in csv.DictReader(handle)]
    if len(ids) != 24:
        raise ValueError(f"expected 24 pilot samples, found {len(ids)}")
    if len(set(ids)) != len(ids):
        raise ValueError("pilot manifest contains duplicate question IDs")
    missing = [question_id for question_id in ids if question_id not in source]
    if missing:
        raise ValueError(f"source is missing question IDs: {missing[:5]}")
    return [
        {
            "question_id": question_id,
            "question_type": source[question_id]["question_type"],
            "question": source[question_id]["question"],
            "question_date": source[question_id]["question_date"],
            "reference_answer": source[question_id]["answer"],
        }
        for question_id in ids
    ]


def _sample_id(connection: sqlite3.Connection, run_id: str, question_id: str) -> int:
    row = connection.execute(
        "SELECT id FROM samples WHERE run_id=? AND question_id=? AND status='completed' "
        "ORDER BY attempt DESC, id DESC LIMIT 1",
        (run_id, question_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"completed sample not found: {run_id}/{question_id}")
    return int(row[0])


def rolling_context(args: argparse.Namespace, question_id: str, tokenizer: LocalQwenTokenizer) -> tuple[str, str]:
    connection = sqlite3.connect(args.rolling_db)
    try:
        sample_id = _sample_id(connection, args.rolling_run_id, question_id)
        row = connection.execute(
            "SELECT summary_text FROM states WHERE sample_id=? AND event='final' "
            "ORDER BY step_ordinal DESC LIMIT 1",
            (sample_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"rolling final state not found: {question_id}")
        summary = str(row[0] or "")
    finally:
        connection.close()
    tail = load_baseline_tail(
        args.rolling_db, args.rolling_run_id, question_id, TAIL_BUDGET_TOKENS, tokenizer
    )["raw_tail"]
    return summary, tail


def sidecar_context(args: argparse.Namespace, question_id: str, tokenizer: LocalQwenTokenizer) -> tuple[str, str]:
    connection = sqlite3.connect(args.sidecar_db)
    try:
        sample_id = _sample_id(connection, args.sidecar_run_id, question_id)
        rows = connection.execute(
            "SELECT event_id,event_ordinal,memory_type,key,value_json,status,event_date,source_json "
            "FROM sidecar_memory WHERE sample_id=? ORDER BY event_ordinal,id",
            (sample_id,),
        ).fetchall()
    finally:
        connection.close()
    records = [
        {
            "event_id": event_id,
            "event_ordinal": ordinal,
            "memory_type": memory_type,
            "key": key,
            "value": json.loads(value_json),
            "status": status,
            "event_date": event_date,
            "source": json.loads(source_json),
        }
        for event_id, ordinal, memory_type, key, value_json, status, event_date, source_json in rows
        if status != "superseded"
    ]
    if not records:
        raise ValueError(f"sidecar has no current records: {question_id}")
    tail = load_baseline_tail(
        args.rolling_db, args.rolling_run_id, question_id, TAIL_BUDGET_TOKENS, tokenizer
    )["raw_tail"]
    return MemoryState(records=records).render_for_answer(), tail


def call_answer(
    row: dict[str, Any], scheme: str, memory: str, tail: str, args: argparse.Namespace, api_key: str
) -> dict[str, Any]:
    messages = answer_messages(memory, tail, row["question_date"], row["question"])
    payload = {
        "model": args.model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": args.max_tokens,
        "thinking": {"type": "disabled"},
    }
    error: str | None = None
    for attempt in range(1, 4):
        try:
            response = requests.post(
                args.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=args.timeout,
            )
            response.raise_for_status()
            body = response.json()
            choice = body["choices"][0]
            return {
                **row,
                "scheme": scheme,
                "hypothesis": (choice["message"].get("content") or "").strip(),
                "usage": body.get("usage", {}),
                "model": args.model,
                "finish_reason": choice.get("finish_reason"),
                "prompt_sha256": sha256_text("\n\n".join(message["content"] for message in messages)),
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001
            error = repr(exc)
            if attempt < 3:
                time.sleep(2 ** (attempt - 1))
    return {
        **row,
        "scheme": scheme,
        "hypothesis": "",
        "usage": {},
        "model": args.model,
        "finish_reason": None,
        "prompt_sha256": sha256_text("\n\n".join(message["content"] for message in messages)),
        "error": error,
    }


def completed_ids(output: Path) -> set[str]:
    if not output.exists():
        return set()
    ids: set[str] = set()
    for line in output.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("error") is None and row.get("hypothesis"):
            ids.add(str(row["question_id"]))
    return ids


def append_result(output: Path, result: dict[str, Any]) -> None:
    with output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def run_scheme(
    rows: list[dict[str, Any]], scheme: str, args: argparse.Namespace, api_key: str, tokenizer: LocalQwenTokenizer
) -> None:
    output = args.output_root / f"deepseek_pilot24_{scheme}.jsonl"
    done = completed_ids(output)
    pending = [row for row in rows if row["question_id"] not in done]
    print(f"[{scheme}] {len(done)} completed, {len(pending)} pending", flush=True)
    if not pending:
        return
    contexts = {
        row["question_id"]: (
            rolling_context(args, row["question_id"], tokenizer)
            if scheme == "rolling"
            else sidecar_context(args, row["question_id"], tokenizer)
        )
        for row in pending
    }
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(call_answer, row, scheme, *contexts[row["question_id"]], args, api_key): row["question_id"]
            for row in pending
        }
        for index, future in enumerate(as_completed(futures), 1):
            result = future.result()
            append_result(output, result)
            status = "ERROR" if result["error"] else "OK"
            print(f"[{scheme} {index}/{len(pending)}] {result['question_id']} {status}", flush=True)


def main() -> None:
    args = parse_args()
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be >= 1")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is not set")
    args.output_root.mkdir(parents=True, exist_ok=True)
    rows = selected_rows(args)
    config = {
        "sample_count": len(rows),
        "schemes": ["rolling", "sidecar"],
        "source": str(args.source),
        "manifest": str(args.manifest),
        "rolling_db": str(args.rolling_db),
        "rolling_run_id": args.rolling_run_id,
        "sidecar_db": str(args.sidecar_db),
        "sidecar_run_id": args.sidecar_run_id,
        "model": args.model,
        "tail_budget_tokens": TAIL_BUDGET_TOKENS,
        "thinking": {"type": "disabled"},
    }
    config_path = args.output_root / "config.json"
    if config_path.exists() and json.loads(config_path.read_text(encoding="utf-8")) != config:
        raise SystemExit(f"existing output config differs: {config_path}")
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output_root / "question_ids.json").write_text(
        json.dumps([row["question_id"] for row in rows], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tokenizer = LocalQwenTokenizer()
    tokenizer.count("warmup")
    for scheme in ("rolling", "sidecar"):
        run_scheme(rows, scheme, args, api_key, tokenizer)
    print(json.dumps({"sample_count": len(rows), "output_root": str(args.output_root)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
