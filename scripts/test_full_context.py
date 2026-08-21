"""Smoke tests for split construction and Full Context prompt formatting."""
from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prompt_orders_sessions_and_places_question_last() -> None:
    runner = load_module("run_full_context")
    row = {"haystack_dates": ["2023/04/11 (Tue) 09:00", "2023/04/10 (Mon) 09:00"], "haystack_session_ids": ["later", "earlier"], "haystack_sessions": [[{"role": "assistant", "content": "Later statement"}], [{"role": "user", "content": "Earlier statement"}]], "question_date": "2023/04/12 (Wed) 09:00", "question": "What happened?"}
    prompt = runner.build_prompt(row)
    assert prompt.index("Earlier statement") < prompt.index("Later statement") < prompt.index("What happened?")


def test_largest_remainder_preserves_total() -> None:
    splitter = load_module("create_stratified_split")
    groups = {"a": [{}] * 3, "b": [{}] * 4, "c": [{}] * 6}
    assert sum(splitter.validation_counts(groups, 0.2).values()) == 3


def test_duplicate_questions_can_share_a_split() -> None:
    splitter = load_module("create_stratified_split")
    groups = {"type-a": [{"question_id": "one"}, {"question_id": "two"}], "type-b": [{"question_id": "three"}]}
    counts = splitter.validation_counts(groups, 0.5)
    assert sum(counts.values()) == 2


if __name__ == "__main__":
    test_prompt_orders_sessions_and_places_question_last()
    test_largest_remainder_preserves_total()
    test_duplicate_questions_can_share_a_split()
    print("ok")
