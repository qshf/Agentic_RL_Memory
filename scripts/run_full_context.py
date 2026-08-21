"""Run the Full Context baseline on a LongMemEval split through Chat Completions.

The history is deliberately not truncated. Select a model with enough context
window, or use --limit for a small smoke test first.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from datasets import load_from_disk


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = PROJECT_ROOT / "data" / "longmemeval_s"
DEFAULT_SPLITS = PROJECT_ROOT / "data" / "splits"
SYSTEM_PROMPT = "Answer the final question from the provided conversation history. Be concise and do not invent facts."


def parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y/%m/%d (%a) %H:%M")


def build_prompt(row: dict[str, Any]) -> str:
    """Serialize every session exactly once, oldest to newest, then append the query."""
    sessions = sorted(
        zip(row["haystack_dates"], row["haystack_session_ids"], row["haystack_sessions"]),
        key=lambda item: parse_date(item[0]),
    )
    lines = ["Complete conversation history:"]
    for date, session_id, messages in sessions:
        lines.append(f"\n[Session {session_id} | {date}]")
        for message in messages:
            lines.append(f"{message['role'].capitalize()}: {message['content']}")
    lines.extend(("\n[Final question]", f"Date: {row['question_date']}", row["question"], "\nAnswer the final question only."))
    return "\n".join(lines)


def load_split_records(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def completed_indices(path: Path) -> set[int]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as handle:
        return {record["dataset_index"] for line in handle if line.strip() if (record := json.loads(line)).get("status") == "ok"}


def chat_completion(base_url: str, api_key: str, model: str, prompt: str, temperature: float, max_tokens: int) -> dict[str, Any]:
    payload = json.dumps({"model": model, "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}], "temperature": temperature, "max_tokens": max_tokens}).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--splits-dir", type=Path, default=DEFAULT_SPLITS)
    parser.add_argument("--split", choices=("train", "val"), default="val")
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-output-tokens", type=int, default=256)
    parser.add_argument("--dry-run", action="store_true", help="Validate prompt construction without calling the API.")
    args = parser.parse_args()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not args.dry_run and not api_key:
        raise SystemExit("Set OPENAI_API_KEY, or use --dry-run.")

    output = args.output or PROJECT_ROOT / "results" / f"full_context_{args.split}_{args.model.replace('/', '_')}.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    completed = completed_indices(output)
    dataset = load_from_disk(str(args.dataset))
    split_records = load_split_records(args.splits_dir / f"{args.split}.jsonl")
    if args.limit is not None:
        split_records = split_records[: args.limit]

    with output.open("a", encoding="utf-8") as handle:
        for position, split_record in enumerate(split_records, start=1):
            dataset_index = split_record["dataset_index"]
            if dataset_index in completed:
                continue
            row = dataset[dataset_index]
            if row["question_id"] != split_record["question_id"]:
                raise ValueError(f"split data mismatch at index {dataset_index}")
            prompt = build_prompt(row)
            record: dict[str, Any] = {"dataset_index": dataset_index, "question_id": row["question_id"], "question_type": row["question_type"], "model": args.model, "history_chars": len(prompt) - len(row["question"]), "prompt_chars": len(prompt)}
            if args.dry_run:
                record["status"] = "dry_run"
            else:
                started = time.monotonic()
                try:
                    response = chat_completion(args.base_url, api_key, args.model, prompt, args.temperature, args.max_output_tokens)
                    record.update(status="ok", prediction=response["choices"][0]["message"]["content"], usage=response.get("usage"), latency_seconds=round(time.monotonic() - started, 3))
                except (urllib.error.HTTPError, urllib.error.URLError, KeyError, json.JSONDecodeError) as error:
                    record.update(status="error", error=str(error), latency_seconds=round(time.monotonic() - started, 3))
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")
            handle.flush()
            print(f"[{position}/{len(split_records)}] {row['question_id']}#{dataset_index}: {record['status']}")


if __name__ == "__main__":
    main()
