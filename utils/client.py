"""OpenAI-compatible client for the Qwen service, with server-side tokenization.

The server ``/tokenize`` endpoint is the only budget authority: it applies the
same chat template the completion endpoint uses, so token counts here are the
counts the model actually sees.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Sequence

import requests

from .config import ModelConfig, sha256_text

RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class ApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.attempts = attempts


@dataclass
class CallResult:
    content: str
    finish_reason: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    attempts: int
    request_params: dict[str, Any]
    prompt_sha256: str
    response_sha256: str


@dataclass
class TokenCounter:
    """Caches server token counts by content hash to keep call volume sane."""

    client: "QwenClient"
    _cache: dict[str, int] = field(default_factory=dict)
    calls: int = 0

    def count(self, text: str) -> int:
        key = sha256_text(text)
        if key not in self._cache:
            self._cache[key] = self.client.tokenize_text(text)
            self.calls += 1
        return self._cache[key]


class QwenClient:
    def __init__(self, config: ModelConfig, api_key: str, session: requests.Session | None = None) -> None:
        self.config = config
        self._api_key = api_key
        self._session = session or requests.Session()
        self._max_model_len: int | None = None

    @property
    def _root_url(self) -> str:
        base = self.config.base_url.rstrip("/")
        return base[: -len("/v1")] if base.endswith("/v1") else base

    @property
    def max_model_len(self) -> int:
        if self._max_model_len is None:
            self.tokenize_text("warmup")
        assert self._max_model_len is not None
        return self._max_model_len

    @property
    def chat_template_kwargs(self) -> dict[str, Any]:
        return {"enable_thinking": self.config.enable_thinking}

    def _post(self, url: str, payload: dict[str, Any], timeout: float) -> tuple[dict[str, Any], int]:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"}
        last: ApiError | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                response = self._session.post(url, json=payload, headers=headers, timeout=timeout)
            except requests.RequestException as error:
                last = ApiError(f"{url}: {error}", retryable=True, attempts=attempt)
            else:
                if response.status_code == 200:
                    return response.json(), attempt
                last = ApiError(
                    f"{url}: HTTP {response.status_code}: {response.text[:500]}",
                    status=response.status_code,
                    retryable=response.status_code in RETRYABLE_STATUS,
                    attempts=attempt,
                )
            if not last.retryable or attempt == self.config.max_attempts:
                raise last
            time.sleep(min(2 ** attempt, 30))
        raise last  # pragma: no cover - loop always returns or raises

    def _record_max_model_len(self, body: dict[str, Any]) -> None:
        length = body.get("max_model_len")
        if isinstance(length, int):
            self._max_model_len = length

    def tokenize_text(self, text: str) -> int:
        body, _ = self._post(
            f"{self._root_url}/tokenize",
            {"model": self.config.model, "prompt": text},
            timeout=self.config.timeout_seconds,
        )
        self._record_max_model_len(body)
        return int(body["count"])

    def tokenize_messages(self, messages: Sequence[dict[str, str]]) -> int:
        body, _ = self._post(
            f"{self._root_url}/tokenize",
            {
                "model": self.config.model,
                "messages": list(messages),
                "add_generation_prompt": True,
                "chat_template_kwargs": self.chat_template_kwargs,
            },
            timeout=self.config.timeout_seconds,
        )
        self._record_max_model_len(body)
        return int(body["count"])

    def detokenize(self, tokens: Sequence[int]) -> str:
        body, _ = self._post(
            f"{self._root_url}/detokenize",
            {"model": self.config.model, "tokens": list(tokens)},
            timeout=self.config.timeout_seconds,
        )
        return body["prompt"]

    def token_ids(self, text: str) -> list[int]:
        body, _ = self._post(
            f"{self._root_url}/tokenize",
            {"model": self.config.model, "prompt": text},
            timeout=self.config.timeout_seconds,
        )
        self._record_max_model_len(body)
        return list(body["tokens"])

    def chat(self, messages: Sequence[dict[str, str]], *, max_tokens: int | None = None) -> CallResult:
        params: dict[str, Any] = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "chat_template_kwargs": self.chat_template_kwargs,
        }
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        payload = {**params, "messages": list(messages)}
        started = time.monotonic()
        body, attempts = self._post(
            f"{self.config.base_url.rstrip('/')}/chat/completions",
            payload,
            timeout=self.config.timeout_seconds,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        choice = body["choices"][0]
        content = choice["message"].get("content") or ""
        usage = body.get("usage") or {}
        return CallResult(
            content=content,
            finish_reason=choice.get("finish_reason") or "",
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=latency_ms,
            attempts=attempts,
            request_params=params,
            prompt_sha256=sha256_text("\n\n".join(m["content"] for m in messages)),
            response_sha256=sha256_text(content),
        )

    def preflight(self) -> dict[str, Any]:
        """Model list, tokenizer and a minimal completion, before any sample runs."""
        headers = {"Authorization": f"Bearer {self._api_key}"}
        response = self._session.get(
            f"{self.config.base_url.rstrip('/')}/models", headers=headers, timeout=60
        )
        if response.status_code != 200:
            raise ApiError(f"/models: HTTP {response.status_code}: {response.text[:300]}")
        served = [item["id"] for item in response.json().get("data", [])]
        if self.config.model not in served:
            raise ApiError(f"model {self.config.model!r} is not served; available: {served}")

        probe_messages = [{"role": "user", "content": "Reply with the single word OK."}]
        prompt_tokens = self.tokenize_messages(probe_messages)
        result = self.chat(probe_messages, max_tokens=16)
        return {
            "served_models": served,
            "max_model_len": self.max_model_len,
            "probe_prompt_tokens": prompt_tokens,
            "probe_output": result.content.strip()[:100],
            "probe_finish_reason": result.finish_reason,
            "chat_template_kwargs": self.chat_template_kwargs,
        }
