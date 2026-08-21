"""Create reproducible, question-type-stratified LongMemEval split manifests."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from datasets import load_from_disk


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = PROJECT_ROOT / "data" / "longmemeval_s"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "splits"


def stable_rank(seed: int, question_id: str) -> str:
    return hashlib.sha256(f"{seed}:{question_id}".encode()).hexdigest()


def validation_counts(groups: dict[str, list[dict]], validation_ratio: float) -> dict[str, int]:
    """Allocate the requested total using largest-remainder rounding per stratum."""
    target = round(sum(len(rows) for rows in groups.values()) * validation_ratio)
    quotas = {label: len(rows) * validation_ratio for label, rows in groups.items()}
    counts = {label: int(quota) for label, quota in quotas.items()}
    remaining = target - sum(counts.values())
    order = sorted(groups, key=lambda label: (quotas[label] - counts[label], label), reverse=True)
    for label in order[:remaining]:
        counts[label] += 1
    return counts


def create_split(dataset_path: Path, output_dir: Path, validation_ratio: float, seed: int, subset: str) -> None:
    if not 0 < validation_ratio < 1:
        raise ValueError("validation_ratio must be between 0 and 1")

    dataset = load_from_disk(str(dataset_path))
    if subset not in {"s", "oracle", "all"}:
        raise ValueError("subset must be one of: s, oracle, all")
    midpoint = len(dataset) // 2
    selected_indices = (
        range(midpoint, len(dataset)) if subset == "s"
        else range(0, midpoint) if subset == "oracle"
        else range(len(dataset))
    )
    # Each question_id appears in two samples with identical final question and
    # answer but different histories. Keep both rows together to avoid leaking
    # the same final question across train and validation.
    by_question_id: dict[str, list[dict]] = defaultdict(list)
    for index in selected_indices:
        row = dataset[index]
        by_question_id[row["question_id"]].append(
            {"question_id": row["question_id"], "question_type": row["question_type"], "dataset_index": index}
        )

    groups: dict[str, list[dict]] = defaultdict(list)
    for question_id, variants in by_question_id.items():
        labels = {variant["question_type"] for variant in variants}
        if len(labels) != 1:
            raise ValueError(f"question_id {question_id} appears in multiple question types: {labels}")
        groups[variants[0]["question_type"]].append({"question_id": question_id, "samples": variants})

    counts = validation_counts(groups, validation_ratio)
    validation_ids: set[str] = set()
    for label, rows in groups.items():
        ranked = sorted(rows, key=lambda row: stable_rank(seed, row["question_id"]))
        validation_ids.update(row["question_id"] for row in ranked[: counts[label]])

    output_dir.mkdir(parents=True, exist_ok=True)
    handles = {
        "train": (output_dir / "train.jsonl").open("w", encoding="utf-8"),
        "val": (output_dir / "val.jsonl").open("w", encoding="utf-8"),
    }
    try:
        for index in selected_indices:
            row = dataset[index]
            split = "val" if row["question_id"] in validation_ids else "train"
            record = {"question_id": row["question_id"], "question_type": row["question_type"], "dataset_index": index}
            handles[split].write(json.dumps(record, ensure_ascii=True) + "\n")
    finally:
        for handle in handles.values():
            handle.close()

    metadata = {
        "dataset": str(dataset_path),
        "subset": subset,
        "seed": seed,
        "validation_ratio": validation_ratio,
        "num_samples": len(list(selected_indices)),
        "num_unique_questions": len(by_question_id),
        "split_sizes": {"train_samples": len(list(selected_indices)) - sum(dataset[index]["question_id"] in validation_ids for index in selected_indices), "val_samples": sum(dataset[index]["question_id"] in validation_ids for index in selected_indices), "train_unique_questions": len(by_question_id) - len(validation_ids), "val_unique_questions": len(validation_ids)},
        "validation_counts_by_question_type": counts,
    }
    (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--validation-ratio", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=20260820)
    parser.add_argument("--subset", choices=("s", "oracle", "all"), default="s", help="Which half of the local mirror to split; default is the LongMemEval-S half.")
    args = parser.parse_args()
    create_split(args.dataset, args.output_dir, args.validation_ratio, args.seed, args.subset)


if __name__ == "__main__":
    main()
