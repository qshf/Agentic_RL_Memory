"""实验共用的冻结 manifest 与源数据集读取。"""
from __future__ import annotations

import csv
import gc
import json
from pathlib import Path
from typing import Any


def read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    if not rows:
        raise ValueError(f"manifest {path} is empty")
    return rows


def load_source(path: Path, question_ids: set[str]) -> dict[str, dict[str, Any]]:
    """从源 JSON 中只加载请求的 LongMemEval 行。"""
    with path.open(encoding="utf-8") as file:
        rows = json.load(file)
    selected = {row["question_id"]: row for row in rows if row["question_id"] in question_ids}
    del rows
    gc.collect()
    missing = question_ids - selected.keys()
    if missing:
        raise ValueError(f"manifest references {len(missing)} unknown question ids: {sorted(missing)[:5]}")
    return selected
