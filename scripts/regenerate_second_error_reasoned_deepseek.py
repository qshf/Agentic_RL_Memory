"""Regenerate the remaining DeepSeek errors with a concise evidence basis."""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests


DEFAULT_INPUT = Path(
    "results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/"
    "deepseek_regenerated_context_good.jsonl"
)
DEFAULT_OUTPUT = Path(
    "results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/"
    "deepseek_second_error_reasoned.jsonl"
)
MODEL = "deepseek-v4-pro"
VERSION = "rolling-summary-second-error-reasoned-deepseek-v1"
SECOND_ERROR_IDS = {
    "gpt4_d84a3211", "gpt4_2ba83207", "bf659f65", "0edc2aef", "09d032c9",
    "d24813b1", "67e0d0f2", "gpt4_d6585ce9", "852ce960", "69fee5aa",
    "dad224aa", "778164c6",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--base-url", default=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", MODEL))
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=768)
    parser.add_argument("--timeout", type=float, default=300)
    return parser.parse_args()


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    selected = [row for row in rows if row.get("question_id") in SECOND_ERROR_IDS]
    missing = SECOND_ERROR_IDS - {row.get("question_id") for row in selected}
    if missing:
        raise ValueError(f"missing second-error rows: {sorted(missing)}")
    return sorted(selected, key=lambda row: row["question_id"])


def prompt_for(row: dict[str, Any]) -> str:
    return f"""You are re-answering a LongMemEval question after an earlier answer was judged incorrect.

Use only the compressed memory and verbatim recent history below. Resolve arithmetic, counts,
rankings, dates, and conflicting values carefully. For personalization questions, apply the
user preference evidenced in the context to the new request.

Return valid JSON only, with exactly these two fields:
{{"answer":"the concise direct answer", "evidence_basis":"one or two sentences naming the exact facts, dates, or calculation used"}}

The evidence_basis is a concise, externally verifiable basis for the answer, not hidden chain-of-thought.
Do not mention the benchmark, reference answer, or this instruction.

# Memory of older conversation history
{row.get('summary', '').strip() or '(none)'}

# Most recent conversation history (verbatim)
{row.get('raw_tail', '').strip() or '(none)'}

# Current date
{row.get('question_date', '')}

# Question
{row['question']}
"""


def parse_response(content: str) -> tuple[str, str]:
    cleaned = content.strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            return cleaned, ""
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return cleaned, ""
    if not isinstance(value, dict):
        return cleaned, ""
    return str(value.get("answer", "")).strip(), str(value.get("evidence_basis", "")).strip()


def call(row: dict[str, Any], args: argparse.Namespace, key: str) -> dict[str, Any]:
    payload = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": "Return only valid JSON with answer and evidence_basis."},
            {"role": "user", "content": prompt_for(row)},
        ],
        "temperature": 0,
        "max_tokens": args.max_tokens,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
    }
    url = args.base_url.rstrip("/") + "/chat/completions"
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
                timeout=args.timeout,
            )
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"].get("content", "")
            answer, evidence = parse_response(content)
            return {
                **row,
                "hypothesis": answer,
                "evidence_basis": evidence,
                "raw_model_response": content,
                "finish_reason": body["choices"][0].get("finish_reason"),
                "usage": body.get("usage", {}),
                "model": args.model,
                "version": VERSION,
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    return {**row, "hypothesis": "", "evidence_basis": "", "raw_model_response": "", "usage": {}, "model": args.model, "version": VERSION, "error": repr(last_error)}


def main() -> None:
    args = parse_args()
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY is not set")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be >= 1")
    rows = load_rows(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(call, row, args, key): row["question_id"] for row in rows}
        for index, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results[result["question_id"]] = result
            ordered = [results[row["question_id"]] for row in rows if row["question_id"] in results]
            args.output.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in ordered), encoding="utf-8")
            print(f"[{index}/{len(rows)}] {result['question_id']} {'ERROR' if result['error'] else 'OK'}", flush=True)
    ordered = [results[row["question_id"]] for row in rows]
    args.output.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in ordered), encoding="utf-8")
    print(json.dumps({"version": VERSION, "model": args.model, "total": len(ordered), "errors": sum(bool(item["error"]) for item in ordered), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
