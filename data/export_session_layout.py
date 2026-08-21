"""Export the complete sample -> session arrangement for manual inspection.

One CSV row represents one complete source session. ``dataset_index`` identifies
the dataset sample; ``session_array_index`` identifies the session inside that
sample. ``chronological_rank`` is provided for comparison and does not replace
the original array order.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path

from datasets import load_from_disk


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = PROJECT_ROOT / "data" / "longmemeval_s"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "longmemeval_session_layout.csv"
DATE_FORMAT = "%Y/%m/%d (%a) %H:%M"


def parse_date(value: str) -> datetime:
    return datetime.strptime(value, DATE_FORMAT)


def preview(text: object, limit: int = 160) -> str:
    text = "" if text is None else str(text)
    return " ".join(text.split())[:limit]


def export_layout(dataset_path: Path, output_path: Path) -> int:
    dataset = load_from_disk(str(dataset_path))
    fields = [
        "dataset_index", "question_id", "question_type", "question", "question_date",
        "sample_session_count", "session_array_order",
        "chronological_rank", "array_order_is_chronological", "session_id", "session_date",
        "message_count", "message_chars", "has_answer", "answer_message_indices",
        "first_message_preview",
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    row_count = 0
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise")
        writer.writeheader()
        for dataset_index, row in enumerate(dataset):
            sessions = row["haystack_sessions"]
            dates = row["haystack_dates"]
            session_ids = row["haystack_session_ids"]
            parsed_dates = [parse_date(value) for value in dates]
            chronological_indices = sorted(range(len(sessions)), key=lambda i: (parsed_dates[i], i))
            chronological_rank = {session_index: rank + 1 for rank, session_index in enumerate(chronological_indices)}
            array_order_is_chronological = parsed_dates == sorted(parsed_dates)
            for session_array_index, (session, session_date, session_id) in enumerate(zip(sessions, dates, session_ids)):
                evidence_indices = [i for i, message in enumerate(session) if bool(message.get("has_answer"))]
                writer.writerow({
                    "dataset_index": dataset_index,
                    "question_id": row["question_id"],
                    "question_type": row["question_type"],
                    "question": row["question"],
                    "question_date": row["question_date"],
                    "sample_session_count": len(sessions),
                    "session_array_order": session_array_index + 1,
                    "chronological_rank": chronological_rank[session_array_index],
                    "array_order_is_chronological": array_order_is_chronological,
                    "session_id": session_id,
                    "session_date": session_date,
                    "message_count": len(session),
                    "message_chars": sum(len(message.get("content") or "") for message in session),
                    "has_answer": bool(evidence_indices),
                    "answer_message_indices": ";".join(map(str, evidence_indices)),
                    "first_message_preview": preview(session[0].get("content")) if session else "",
                })
                row_count += 1
    return row_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(f"wrote {export_layout(args.dataset, args.output)} session rows to {args.output}")


if __name__ == "__main__":
    main()
