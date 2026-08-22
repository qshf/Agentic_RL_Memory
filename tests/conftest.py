"""Shared fakes: a deterministic offline stand-in for the Qwen service."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rolling_summary.client import CallResult, TokenCounter  # noqa: E402
from rolling_summary.config import BudgetConfig, ModelConfig, sha256_text  # noqa: E402


def word_count(text: str) -> int:
    """Whitespace tokens, so budgets in tests are readable by eye."""
    return len(text.split())


class FakeClient:
    """Whitespace tokenizer plus a scripted chat endpoint that records requests."""

    def __init__(
        self,
        *,
        max_model_len: int = 100_000,
        summary_max_tokens: int = 20,
        responses: list[str] | None = None,
        failures: int = 0,
    ) -> None:
        self.config = ModelConfig(
            base_url="http://fake/v1",
            model="fake-model",
            summary_max_tokens=summary_max_tokens,
            max_attempts=3,
        )
        self._max_model_len = max_model_len
        self.responses = responses or []
        self.failures = failures
        self.requests: list[dict] = []
        self.tokenize_calls = 0

    @property
    def max_model_len(self) -> int:
        return self._max_model_len

    def tokenize_text(self, text: str) -> int:
        self.tokenize_calls += 1
        return word_count(text)

    def tokenize_messages(self, messages) -> int:
        self.tokenize_calls += 1
        return sum(word_count(message["content"]) for message in messages)

    def token_ids(self, text: str) -> list[int]:
        return list(range(word_count(text)))

    def detokenize(self, tokens) -> str:
        return " ".join(f"t{index}" for index in tokens)

    def chat(self, messages, *, max_tokens=None) -> CallResult:
        self.requests.append({"messages": list(messages), "max_tokens": max_tokens})
        index = len(self.requests) - 1
        content = self.responses[index] if index < len(self.responses) else f"response-{index}"
        return CallResult(
            content=content,
            finish_reason="stop",
            input_tokens=sum(word_count(message["content"]) for message in messages),
            output_tokens=word_count(content),
            latency_ms=1,
            attempts=1,
            request_params={"model": self.config.model, "max_tokens": max_tokens},
            prompt_sha256=sha256_text("\n\n".join(m["content"] for m in messages)),
            response_sha256=sha256_text(content),
        )

    @property
    def summary_requests(self) -> list[dict]:
        return [
            request
            for request in self.requests
            if request["messages"][0]["content"].startswith("You maintain a long-term memory")
        ]


@pytest.fixture
def budgets() -> BudgetConfig:
    """Small budgets so a handful of short sessions exercise every branch."""
    return BudgetConfig(
        rolling_trigger_tokens=200,
        summary_budget_tokens=20,
        raw_tail_budget_tokens=40,
        final_context_budget_tokens=60,
    )


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def tokenizer(client: FakeClient) -> TokenCounter:
    return TokenCounter(client)


def make_row(
    *,
    question_id: str = "q1",
    session_count: int = 4,
    words_per_message: int = 10,
    messages_per_session: int = 4,
    dates: list[str] | None = None,
) -> dict:
    """A LongMemEval-shaped row, including gold fields the pipeline must ignore."""
    dates = dates or [f"2023/05/{20 + index:02d} (Sat) 02:21" for index in range(session_count)]
    sessions = []
    for session_index in range(session_count):
        session = []
        for message_index in range(messages_per_session):
            role = "user" if message_index % 2 == 0 else "assistant"
            body = " ".join(
                f"s{session_index}m{message_index}w{word}" for word in range(words_per_message)
            )
            session.append({"role": role, "content": body, "has_answer": message_index == 1})
        sessions.append(session)
    return {
        "question_id": question_id,
        "question_type": "single-session-user",
        "question": "SECRETQUESTION what did I say about the thing?",
        "question_date": "2023/06/01 (Thu) 10:00",
        "answer": "SECRETGOLDANSWER",
        "answer_session_ids": ["session-0"],
        "haystack_dates": dates,
        "haystack_session_ids": [f"session-{index}" for index in range(session_count)],
        "haystack_sessions": sessions,
    }
