"""Create a reproducible, question-type-stratified LongMemEval-S manifest.

The sampler uses only question_type for selection. Evidence annotations are
written as diagnostic columns and must not be provided to a memory method.

Examples:
    uv run python scripts/sample_longmemeval_s.py
    uv run python scripts/sample_longmemeval_s.py --size 12 --seed 20260822 \
      --exclude data/samples/longmemeval_s_eval_120_seed_20260821.csv \
      --output data/samples/longmemeval_s_smoke_12_seed_20260822.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data" / "official_longmemeval" / "longmemeval_s_cleaned.json"
DEFAULT_OUTPUT = ROOT / "data" / "samples" / "longmemeval_s_eval_120_seed_20260821.csv"


def proportional_quotas(counts: Counter[str], size: int) -> dict[str, int]:
    """Allocate exact target size using largest remainder apportionment."""
    total = sum(counts.values())
    if not 0 < size <= total:
        raise ValueError(f"size must be in [1, {total}], got {size}")

    raw = {question_type: size * count / total for question_type, count in counts.items()}
    quotas = {question_type: int(value) for question_type, value in raw.items()}
    remaining = size - sum(quotas.values())
    order = sorted(
        counts,
        key=lambda question_type: (raw[question_type] - quotas[question_type], question_type),
        reverse=True,
    )
    for question_type in order[:remaining]:
        quotas[question_type] += 1
    return quotas


def evidence_diagnostics(row: dict[str, Any]) -> dict[str, int | bool | str]:
    """Derive post-hoc audit fields from has_answer without affecting sampling."""
    sessions = row["haystack_sessions"]
    chronological_order = sorted(range(len(sessions)), key=lambda index: row["haystack_dates"][index])
    chronological_rank = {index: rank for rank, index in enumerate(chronological_order, start=1)}

    evidence_sessions: list[int] = []
    first_evidence_message_indices: list[int] = []
    first_evidence_rounds: list[int] = []
    evidence_message_count = 0
    for session_index, session in enumerate(sessions):
        message_indices = [
            message_index
            for message_index, message in enumerate(session, start=1)
            if message.get("has_answer")
        ]
        evidence_message_count += len(message_indices)
        if message_indices:
            evidence_sessions.append(session_index)
            first_message_index = min(message_indices)
            first_evidence_message_indices.append(first_message_index)
            first_evidence_rounds.append((first_message_index + 1) // 2)

    if not evidence_sessions:
        return {
            "has_answer_annotation": False,
            "evidence_session_count": 0,
            "evidence_message_count": 0,
            "first_evidence_session_original_rank": "",
            "first_evidence_session_chronological_rank": "",
            "evidence_sessions_first_answer_round_1": 0,
            "evidence_sessions_first_answer_round_2": 0,
            "evidence_sessions_first_answer_round_3_plus": 0,
            "has_first_round_evidence": False,
        }

    return {
        "has_answer_annotation": True,
        "evidence_session_count": len(evidence_sessions),
        "evidence_message_count": evidence_message_count,
        "first_evidence_session_original_rank": min(evidence_sessions) + 1,
        "first_evidence_session_chronological_rank": min(
            chronological_rank[index] for index in evidence_sessions
        ),
        "evidence_sessions_first_answer_round_1": sum(
            round_number == 1 for round_number in first_evidence_rounds
        ),
        "evidence_sessions_first_answer_round_2": sum(
            round_number == 2 for round_number in first_evidence_rounds
        ),
        "evidence_sessions_first_answer_round_3_plus": sum(
            round_number >= 3 for round_number in first_evidence_rounds
        ),
        "has_first_round_evidence": any(index <= 2 for index in first_evidence_message_indices),
    }


def manifest_row(row: dict[str, Any], dataset_index: int, seed: int) -> dict[str, Any]:
    sessions = row["haystack_sessions"]
    message_count = sum(len(session) for session in sessions)
    character_count = sum(
        len(message.get("content") or "")
        for session in sessions
        for message in session
    )
    return {
        "dataset_name": "longmemeval_s_cleaned",
        "dataset_index": dataset_index,
        "question_id": row["question_id"],
        "question_type": row["question_type"],
        "session_count": len(sessions),
        "message_count": message_count,
        "history_character_count": character_count,
        "selection_seed": seed,
        **evidence_diagnostics(row),
    }


def read_question_ids(manifests: list[Path]) -> set[str]:
    """Collect question ids already claimed by frozen manifests."""
    claimed: set[str] = set()
    for path in manifests:
        with path.open(encoding="utf-8") as file:
            claimed.update(row["question_id"] for row in csv.DictReader(file))
    return claimed


def sample(
    rows: list[dict[str, Any]],
    size: int,
    seed: int,
    exclude: set[str] | None = None,
) -> tuple[list[tuple[int, dict[str, Any]]], dict[str, int]]:
    """Select `size` rows, keeping `dataset_index` anchored to the source file."""
    excluded = exclude or set()
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for dataset_index, row in enumerate(rows):
        if row["question_id"] in excluded:
            continue
        grouped[row["question_type"]].append((dataset_index, row))

    counts = Counter({question_type: len(group) for question_type, group in grouped.items()})
    quotas = proportional_quotas(counts, size)
    generator = random.Random(seed)
    selected: list[tuple[int, dict[str, Any]]] = []
    for question_type in sorted(grouped):
        candidates = sorted(grouped[question_type], key=lambda item: item[1]["question_id"])
        generator.shuffle(candidates)
        selected.extend(candidates[: quotas[question_type]])

    return sorted(selected), quotas


def write_manifest(rows: list[dict[str, Any]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sample LongMemEval-S into a reproducible CSV manifest.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Official S JSON path")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="CSV manifest path")
    parser.add_argument("--size", type=int, default=120, help="Number of S samples to select")
    parser.add_argument("--seed", type=int, default=20260821, help="Deterministic random seed")
    parser.add_argument(
        "--exclude",
        type=Path,
        nargs="*",
        default=[],
        help="Frozen manifests whose question_ids must not be reused",
    )
    args = parser.parse_args()

    with args.source.open(encoding="utf-8") as file:
        source_rows = json.load(file)
    excluded = read_question_ids(args.exclude)
    selected, quotas = sample(source_rows, args.size, args.seed, excluded)
    manifest = [manifest_row(row, dataset_index, args.seed) for dataset_index, row in selected]
    write_manifest(manifest, args.output)

    print(f"Selected {len(manifest)} of {len(source_rows) - len(excluded)} eligible LongMemEval-S samples")
    print(f"Seed: {args.seed}")
    if excluded:
        print(f"Excluded {len(excluded)} question ids from {len(args.exclude)} frozen manifest(s)")
    print(f"Manifest: {args.output}")
    print("Question-type quotas:")
    for question_type in sorted(quotas):
        count = sum(row["question_type"] == question_type for row in manifest)
        print(f"  {question_type}: {count}")
    first_round = sum(row["has_first_round_evidence"] for row in manifest)
    no_annotation = sum(not row["has_answer_annotation"] for row in manifest)
    print(f"Diagnostics only: first-round evidence={first_round}/{len(manifest)}")
    print(f"Diagnostics only: no has_answer annotation={no_annotation}/{len(manifest)}")


if __name__ == "__main__":
    main()
