"""Regenerate answers for V1 cases whose final context was judged sufficient."""
from __future__ import annotations

import argparse
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
from rolling_summary.prompts import answer_messages


DEFAULT_JUDGMENTS = Path("results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/deepseek_judgments.jsonl")
DEFAULT_SOURCE = Path("data/official_longmemeval/longmemeval_s_cleaned.json")
DEFAULT_DB = Path("results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/trajectory.sqlite3")
DEFAULT_OUTPUT = Path("results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/deepseek_regenerated_context_good.jsonl")
MODEL = "deepseek-v4-pro"
VERSION = "rolling-summary-context-good-regeneration-deepseek-v1"
CONTEXT_OMISSION_IDS = {
    "ccb36322", "0a995998", "3a704032", "dd2973ad", "81507db6", "4f54b7c9",
    "a08a253f", "37f165cf", "gpt4_1916e0ea", "gpt4_fa19884d", "1568498a",
    "ceb54acb", "f523d9fe", "8aef76bc", "8752c811", "352ab8bd", "fca762bc",
    "7a8d0b71",
}
DATA_OR_JUDGE_ISSUE_IDS = {"bc8a6e93_abs", "gpt4_7abb270c", "gpt4_372c3eed_abs"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--judgments", type=Path, default=DEFAULT_JUDGMENTS)
    p.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--base-url", default=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    p.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", MODEL))
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--max-tokens", type=int, default=512)
    p.add_argument("--timeout", type=float, default=300)
    return p.parse_args()


def load_rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    judgments = {json.loads(line)["question_id"]: json.loads(line) for line in args.judgments.read_text().splitlines() if line.strip()}
    source = {r["question_id"]: r for r in json.loads(args.source.read_text(encoding="utf-8"))}
    connection = sqlite3.connect(args.db)
    rows = []
    try:
        for qid, judge in judgments.items():
            if judge.get("judgment") != "no" or qid in CONTEXT_OMISSION_IDS or qid in DATA_OR_JUDGE_ISSUE_IDS:
                continue
            sample = connection.execute("select id from samples where question_id=? order by attempt desc limit 1", (qid,)).fetchone()
            if not sample:
                continue
            sample_id = sample[0]
            summary = connection.execute("select summary_text from states where sample_id=? and event='final'", (sample_id,)).fetchone()[0] or ""
            cut = connection.execute("select coalesce(max(step_ordinal),0) from states where sample_id=? and event='rolling_compression'", (sample_id,)).fetchone()[0]
            tail = "\n".join(x[0] for x in connection.execute("select raw_text from states where sample_id=? and event='ingest' and step_ordinal>? order by step_ordinal", (sample_id, cut)).fetchall() if x[0])
            row = source[qid]
            rows.append({
                "question_id": qid,
                "question_type": row["question_type"],
                "question": row["question"],
                "question_date": row["question_date"],
                "reference_answer": row["answer"],
                "original_hypothesis": judge.get("hypothesis", ""),
                "summary": summary,
                "raw_tail": tail,
            })
    finally:
        connection.close()
    return rows


def call(row: dict[str, Any], args: argparse.Namespace, key: str) -> dict[str, Any]:
    messages = answer_messages(row["summary"], row["raw_tail"], row["question_date"], row["question"])
    payload = {"model": args.model, "messages": messages, "temperature": 0, "max_tokens": args.max_tokens, "thinking": {"type": "disabled"}}
    url = args.base_url.rstrip("/") + "/chat/completions"
    last = None
    for attempt in range(3):
        try:
            response = requests.post(url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=payload, timeout=args.timeout)
            response.raise_for_status()
            body = response.json()
            message = body["choices"][0]["message"]
            return {**row, "hypothesis": message.get("content", "").strip(), "finish_reason": body["choices"][0].get("finish_reason"), "usage": body.get("usage", {}), "model": args.model, "version": VERSION, "error": None}
        except Exception as exc:  # noqa: BLE001
            last = repr(exc)
            if attempt < 2:
                time.sleep(2 ** attempt)
    return {**row, "hypothesis": "", "finish_reason": None, "usage": {}, "model": args.model, "version": VERSION, "error": last}


def main() -> None:
    args = parse_args()
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY is not set")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be >= 1")
    rows = load_rows(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(call, row, args, key): row["question_id"] for row in rows}
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results[result["question_id"]] = result
            ordered = [results[r["question_id"]] for r in rows if r["question_id"] in results]
            args.output.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in ordered), encoding="utf-8")
            print(f"[{i}/{len(rows)}] {result['question_id']} {'ERROR' if result['error'] else 'OK'}", flush=True)
    ordered = [results[r["question_id"]] for r in rows]
    args.output.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in ordered), encoding="utf-8")
    print(json.dumps({"version": VERSION, "model": args.model, "total": len(ordered), "errors": sum(bool(x["error"]) for x in ordered), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
