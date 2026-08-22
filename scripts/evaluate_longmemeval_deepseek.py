"""Evaluate LongMemEval hypotheses with a DeepSeek judge.

The official benchmark uses an LLM judge rather than exact string matching.
This script follows the published task-type rubric and writes one JSON object
per question so interrupted evaluations can be resumed safely.

Usage:
    DEEPSEEK_API_KEY=... uv run python scripts/evaluate_longmemeval_deepseek.py \
      --hypotheses results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/hypotheses.jsonl \
      --source data/official_longmemeval/longmemeval_s_cleaned.json \
      --output results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/deepseek_judgments.jsonl
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"
JUDGE_VERSION = "longmemeval-official-rubric-deepseek-v2"

RUBRIC = {
    "temporal-reasoning": (
        "Answer yes if the response contains the correct answer, an equivalent answer, "
        "or all intermediate steps needed to obtain it. Do not penalize an off-by-one "
        "error for a number of days, weeks, or months. If it contains only a subset of "
        "the required information, answer no."
    ),
    "knowledge-update": (
        "Answer yes if the response contains the correct answer. If it contains older "
        "information together with the updated answer, it is still yes as long as the "
        "updated answer required by the question is present."
    ),
    "single-session-preference": (
        "The reference is a rubric for a desired personalized response. Answer yes if "
        "the response satisfies the rubric and correctly uses the user's personal "
        "information. It need not cover every rubric point."
    ),
    "other": (
        "Answer yes if the response contains the correct answer, an equivalent answer, "
        "or all intermediate steps needed to obtain it. If it contains only a subset of "
        "the required information, answer no."
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hypotheses", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--base-url", default=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL))
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--retry-count", type=int, default=3)
    parser.add_argument("--limit", type=int, default=None)
    return parser.parse_args()


def load_rows(source: Path, hypotheses: Path, manifest: Path | None, limit: int | None) -> list[dict[str, Any]]:
    gold = {row["question_id"]: row for row in json.loads(source.read_text(encoding="utf-8"))}
    predictions = {
        row["question_id"]: row.get("hypothesis", "")
        for row in (json.loads(line) for line in hypotheses.read_text(encoding="utf-8").splitlines() if line.strip())
    }
    if manifest:
        with manifest.open(newline="", encoding="utf-8") as handle:
            ids = [row["question_id"] for row in csv.DictReader(handle)]
    else:
        ids = list(predictions)
    rows = []
    for question_id in ids[:limit]:
        if question_id not in gold or question_id not in predictions:
            raise ValueError(f"missing gold or hypothesis for {question_id}")
        row = gold[question_id]
        rows.append(
            {
                "question_id": question_id,
                "question_type": row["question_type"],
                "question": row["question"],
                "reference_answer": row["answer"],
                "hypothesis": predictions[question_id],
            }
        )
    return rows


def prompt_for(row: dict[str, Any]) -> str:
    kind = row["question_type"] if row["question_type"] in RUBRIC else "other"
    return f"""You are a strict correctness judge for the LongMemEval benchmark.

Task type: {row['question_type']}
{RUBRIC[kind]}

Return JSON only, exactly one object with this schema:
{{"judgment":"yes"}} or {{"judgment":"no"}}
Do not include explanations, markdown, or any other fields.

<question>
{row['question']}
</question>
<reference_answer_or_rubric>
{row['reference_answer']}
</reference_answer_or_rubric>
<model_response>
{row['hypothesis']}
</model_response>"""


def parse_judgment(text: str) -> str:
    cleaned = text.strip()
    try:
        value = json.loads(cleaned).get("judgment", "")
        if str(value).lower() in {"yes", "no"}:
            return str(value).lower()
    except (ValueError, AttributeError):
        pass
    match = re.search(r"\b(yes|no)\b", cleaned.lower())
    if match:
        return match.group(1)
    raise ValueError(f"judge returned neither yes nor no: {text[:200]!r}")


def judge(row: dict[str, Any], args: argparse.Namespace, api_key: str) -> dict[str, Any]:
    url = args.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": "Return only the requested JSON judgment."},
            {"role": "user", "content": prompt_for(row)},
        ],
        "max_tokens": args.max_tokens,
        "temperature": 0,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
    }
    last_error: Exception | None = None
    for attempt in range(args.retry_count):
        try:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=args.timeout,
            )
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"].get("content", "")
            judgment = parse_judgment(content)
            return {
                **row,
                "judgment": judgment,
                "judge_model": args.model,
                "judge_version": JUDGE_VERSION,
                "judge_response": content,
                "usage": body.get("usage", {}),
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - retry transport and parse failures alike
            last_error = exc
            if attempt + 1 < args.retry_count:
                time.sleep(2**attempt)
    return {
        **row,
        "judgment": None,
        "judge_model": args.model,
        "judge_version": JUDGE_VERSION,
        "judge_response": None,
        "usage": {},
        "error": repr(last_error),
    }


def read_existing(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    return {
        row["question_id"]: row
        for row in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    }


def write_summary(output: Path, rows: list[dict[str, Any]]) -> None:
    judged = [row for row in rows if row.get("judgment") in {"yes", "no"}]
    by_type: dict[str, dict[str, int]] = {}
    for row in judged:
        item = by_type.setdefault(row["question_type"], {"correct": 0, "total": 0})
        item["total"] += 1
        item["correct"] += row["judgment"] == "yes"
    summary = {
        "judge_version": JUDGE_VERSION,
        "judge_model": rows[0]["judge_model"] if rows else None,
        "total": len(rows),
        "judged": len(judged),
        "correct": sum(row["judgment"] == "yes" for row in judged),
        "accuracy": (sum(row["judgment"] == "yes" for row in judged) / len(judged)) if judged else None,
        "errors": sum(row.get("error") is not None for row in rows),
        "by_question_type": {
            key: {**value, "accuracy": value["correct"] / value["total"]}
            for key, value in sorted(by_type.items())
        },
    }
    output.with_name(output.stem + "_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    args = parse_args()
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be >= 1")
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is not set")
    rows = load_rows(args.source, args.hypotheses, args.manifest, args.limit)
    existing = read_existing(args.output)
    pending = [
        row for row in rows
        if row["question_id"] not in existing
        or existing[row["question_id"]].get("judgment") is None
        or existing[row["question_id"]].get("judge_version") != JUDGE_VERSION
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(f"{len(rows)} rows, {len(existing)} cached, {len(pending)} to judge", flush=True)
    results = dict(existing)
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(judge, row, args, api_key): row["question_id"] for row in pending}
        for index, future in enumerate(as_completed(futures), 1):
            row = future.result()
            results[row["question_id"]] = row
            ordered = [results[row["question_id"]] for row in rows if row["question_id"] in results]
            args.output.write_text(
                "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in ordered), encoding="utf-8"
            )
            print(f"[{index}/{len(pending)}] {row['question_id']} {row.get('judgment') or 'ERROR'}", flush=True)
    ordered = [results[row["question_id"]] for row in rows if row["question_id"] in results]
    args.output.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in ordered), encoding="utf-8"
    )
    write_summary(args.output, ordered)
    summary = json.loads(args.output.with_name(args.output.stem + "_summary.json").read_text(encoding="utf-8"))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
