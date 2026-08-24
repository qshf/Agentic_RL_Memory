"""在两种记忆上下文上使用同一个 DeepSeek Answer Model 做对照。"""
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
from memory_sidecar.protocol import MemoryState
from rolling_summary.prompts import answer_messages
from utils.local_tokenizer import LocalQwenTokenizer


DEFAULT_SOURCE = Path("data/official_longmemeval/longmemeval_s_cleaned.json")
DEFAULT_MANIFEST = Path("data/samples/longmemeval_s_pilot_24_from_baseline_eval_20260822.csv")
DEFAULT_ROLLING_DB = Path("results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/trajectory.sqlite3")
DEFAULT_SIDECAR_DB = Path("results/memory_sidecar/sidecar-strong-pilot-24-v1-c2-selected/trajectory.sqlite3")
DEFAULT_OUTPUT_ROOT = Path("results/memory_sidecar/sidecar-strong-pilot-24-v1-c2-selected")
MODEL = "deepseek-v4-pro"

# 两个方案都没有被判定为上下文缺失的交集；来源见 pilot 报告第 2.4 节。
BOTH_CONTEXT_SUFFICIENT = {
    "gpt4_2ba83207", "0edc2aef", "09d032c9", "d24813b1", "67e0d0f2",
    "852ce960", "69fee5aa", "dad224aa", "778164c6", "830ce83f",
    "c4f10528", "75832dbd", "51a45a95", "gpt4_59149c77",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--rolling-db", type=Path, default=DEFAULT_ROLLING_DB)
    parser.add_argument("--sidecar-db", type=Path, default=DEFAULT_SIDECAR_DB)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--base-url", default=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", MODEL))
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--timeout", type=float, default=300.0)
    return parser.parse_args()


def selected_rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    source = {row["question_id"]: row for row in json.loads(args.source.read_text(encoding="utf-8"))}
    with args.manifest.open(encoding="utf-8", newline="") as handle:
        ids = [row["question_id"] for row in csv.DictReader(handle) if row["question_id"] in BOTH_CONTEXT_SUFFICIENT]
    return [
        {
            "question_id": qid,
            "question_type": source[qid]["question_type"],
            "question": source[qid]["question"],
            "question_date": source[qid]["question_date"],
            "reference_answer": source[qid]["answer"],
        }
        for qid in ids
    ]


def rolling_context(args: argparse.Namespace, qid: str, tokenizer: LocalQwenTokenizer) -> tuple[str, str]:
    connection = sqlite3.connect(args.rolling_db)
    try:
        sample = connection.execute(
            "select id from samples where run_id=? and question_id=? and status='completed' "
            "order by attempt desc, id desc limit 1",
            ("rolling-summary-eval120-v1-atomic-c2", qid),
        ).fetchone()
        if sample is None:
            raise ValueError(f"rolling sample not found: {qid}")
        sample_id = sample[0]
        summary = connection.execute(
            "select summary_text from states where sample_id=? and event='final' order by step_ordinal desc limit 1",
            (sample_id,),
        ).fetchone()[0] or ""
        cut = connection.execute(
            "select coalesce(max(step_ordinal), 0) from states where sample_id=? and event='rolling_compression'",
            (sample_id,),
        ).fetchone()[0]
        # 两种方案的 DeepSeek 重答使用同一 16K recent tail；Rolling 历史 run
        # 原本的 tail 没有独立固定上限，这里重新按 baseline cut 裁剪。
        tail = load_baseline_tail(
            args.rolling_db,
            "rolling-summary-eval120-v1-atomic-c2",
            qid,
            16 * 1024,
            tokenizer,
        )["raw_tail"]
        return summary, tail
    finally:
        connection.close()


def sidecar_context(args: argparse.Namespace, qid: str, tokenizer: LocalQwenTokenizer) -> tuple[str, str]:
    connection = sqlite3.connect(args.sidecar_db)
    try:
        sample = connection.execute(
            "select id from samples where run_id=? and question_id=? and status='completed' "
            "order by attempt desc, id desc limit 1",
            ("sidecar-strong-pilot-24-v1-c2-selected", qid),
        ).fetchone()
        if sample is None:
            raise ValueError(f"sidecar sample not found: {qid}")
        sample_id = sample[0]
        records = connection.execute(
            "select event_id,event_ordinal,memory_type,key,value_json,status,event_date,source_json "
            "from sidecar_memory where sample_id=? order by event_ordinal,id",
            (sample_id,),
        ).fetchall()
        parsed = []
        for event_id, ordinal, memory_type, key, value_json, status, event_date, source_json in records:
            if status == "superseded":
                continue
            parsed.append({
                "event_id": event_id, "event_ordinal": ordinal, "memory_type": memory_type,
                "key": key, "value": json.loads(value_json), "status": status,
                "event_date": event_date, "source": json.loads(source_json),
            })
        memory = MemoryState(records=parsed).render_for_answer()
    finally:
        connection.close()
    tail = load_baseline_tail(
        args.rolling_db,
        "rolling-summary-eval120-v1-atomic-c2",
        qid,
        16 * 1024,
        tokenizer,
    )["raw_tail"]
    return memory, tail


def call_answer(row: dict[str, Any], scheme: str, memory: str, tail: str, args: argparse.Namespace, key: str) -> dict[str, Any]:
    messages = answer_messages(memory, tail, row["question_date"], row["question"])
    payload = {
        "model": args.model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": args.max_tokens,
        "thinking": {"type": "disabled"},
    }
    last_error = None
    for attempt in range(3):
        try:
            response = requests.post(
                args.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
                timeout=args.timeout,
            )
            response.raise_for_status()
            body = response.json()
            choice = body["choices"][0]
            return {
                **row, "scheme": scheme, "hypothesis": (choice["message"].get("content") or "").strip(),
                "usage": body.get("usage", {}), "model": args.model,
                "finish_reason": choice.get("finish_reason"), "error": None,
            }
        except Exception as exc:  # noqa: BLE001
            last_error = repr(exc)
            if attempt < 2:
                time.sleep(2**attempt)
    return {**row, "scheme": scheme, "hypothesis": "", "usage": {}, "model": args.model, "error": last_error}


def run_scheme(rows: list[dict[str, Any]], scheme: str, args: argparse.Namespace, key: str, tokenizer: LocalQwenTokenizer) -> list[dict[str, Any]]:
    contexts = {}
    for row in rows:
        contexts[row["question_id"]] = rolling_context(args, row["question_id"], tokenizer) if scheme == "rolling" else sidecar_context(args, row["question_id"], tokenizer)
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(call_answer, row, scheme, *contexts[row["question_id"]], args, key): row["question_id"]
            for row in rows
        }
        for index, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results[result["question_id"]] = result
            print(f"[{scheme} {index}/{len(rows)}] {result['question_id']} {'ERROR' if result['error'] else 'OK'}", flush=True)
    return [results[row["question_id"]] for row in rows]


def main() -> None:
    args = parse_args()
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY is not set")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be >= 1")
    args.output_root.mkdir(parents=True, exist_ok=True)
    tokenizer = LocalQwenTokenizer()
    tokenizer.count("warmup")
    rows = selected_rows(args)
    if len(rows) != len(BOTH_CONTEXT_SUFFICIENT):
        raise SystemExit(f"expected {len(BOTH_CONTEXT_SUFFICIENT)} selected rows, got {len(rows)}")
    (args.output_root / "deepseek_context_sufficient_ids.json").write_text(
        json.dumps([row["question_id"] for row in rows], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for scheme in ("rolling", "sidecar"):
        output = args.output_root / f"deepseek_context_sufficient_{scheme}.jsonl"
        results = run_scheme(rows, scheme, args, key, tokenizer)
        output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results), encoding="utf-8")
    print(json.dumps({"sample_count": len(rows), "schemes": ["rolling", "sidecar"], "output_root": str(args.output_root)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
