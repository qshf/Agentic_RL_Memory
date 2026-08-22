"""Plan check 1: exclusion-based smoke sampling is reproducible and disjoint."""
from __future__ import annotations

import csv
import importlib.util
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sample_longmemeval_s", ROOT / "scripts" / "sample_longmemeval_s.py")
sampler = importlib.util.module_from_spec(SPEC)
sys.modules["sample_longmemeval_s"] = sampler
SPEC.loader.exec_module(sampler)

EVAL_MANIFEST = ROOT / "data" / "samples" / "longmemeval_s_eval_120_seed_20260821.csv"
SMOKE_MANIFEST = ROOT / "data" / "samples" / "longmemeval_s_smoke_12_seed_20260822.csv"


def fake_rows(count: int = 300) -> list[dict]:
    types = ["multi-session", "temporal-reasoning", "knowledge-update", "single-session-user"]
    return [
        {
            "question_id": f"q{index:04d}",
            "question_type": types[index % len(types)],
            "haystack_dates": ["2023/05/20 (Sat) 02:21"],
            "haystack_session_ids": ["s0"],
            "haystack_sessions": [[{"role": "user", "content": "hello", "has_answer": True}]],
        }
        for index in range(count)
    ]


def test_sampling_is_deterministic_for_a_fixed_seed():
    rows = fake_rows()
    first, _quotas = sampler.sample(rows, 12, 20260822)
    second, _quotas = sampler.sample(rows, 12, 20260822)
    assert [index for index, _row in first] == [index for index, _row in second]


def test_a_different_seed_selects_a_different_set():
    rows = fake_rows()
    first, _ = sampler.sample(rows, 12, 20260822)
    second, _ = sampler.sample(rows, 12, 20260821)
    assert {index for index, _ in first} != {index for index, _ in second}


def test_excluded_question_ids_are_never_selected():
    rows = fake_rows()
    excluded = {row["question_id"] for row in rows[:100]}
    selected, _quotas = sampler.sample(rows, 12, 20260822, excluded)
    assert {row["question_id"] for _index, row in selected}.isdisjoint(excluded)


def test_dataset_index_stays_anchored_to_the_source_file_after_exclusion():
    rows = fake_rows()
    excluded = {row["question_id"] for row in rows[:100]}
    selected, _quotas = sampler.sample(rows, 12, 20260822, excluded)
    for dataset_index, row in selected:
        assert rows[dataset_index]["question_id"] == row["question_id"]


def test_quotas_stay_proportional_over_the_eligible_pool():
    rows = fake_rows()
    excluded = {row["question_id"] for row in rows if row["question_type"] == "multi-session"}
    selected, quotas = sampler.sample(rows, 12, 20260822, excluded)
    counts = Counter(row["question_type"] for _index, row in selected)

    assert sum(counts.values()) == 12
    assert "multi-session" not in counts
    assert quotas.keys() == counts.keys()


def test_selection_reads_no_gold_field(monkeypatch):
    class Guarded(dict):
        def __getitem__(self, key):
            assert key not in {"answer", "answer_session_ids", "has_answer"}
            return super().__getitem__(key)

    rows = [Guarded(row) for row in fake_rows(40)]
    sampler.sample(rows, 8, 20260822)


@pytest.mark.skipif(not SMOKE_MANIFEST.exists(), reason="smoke manifest not generated yet")
def test_frozen_smoke_and_eval_manifests_do_not_overlap():
    def ids(path: Path) -> set[str]:
        with path.open(encoding="utf-8") as file:
            return {row["question_id"] for row in csv.DictReader(file)}

    smoke, evaluation = ids(SMOKE_MANIFEST), ids(EVAL_MANIFEST)
    assert len(smoke) == 12
    assert len(evaluation) == 120
    assert smoke.isdisjoint(evaluation)


@pytest.mark.skipif(not SMOKE_MANIFEST.exists(), reason="smoke manifest not generated yet")
def test_frozen_smoke_manifest_covers_every_question_type():
    with SMOKE_MANIFEST.open(encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert len(Counter(row["question_type"] for row in rows)) == 6
    assert {int(row["selection_seed"]) for row in rows} == {20260822}
