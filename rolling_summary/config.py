"""Frozen run configuration for the Rolling Summary V1 baseline.

Budgets use K = 1024 tokens, matching the 8,192-token summary generation cap
fixed by the experiment protocol.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "http://117.186.43.62:5027/v1"
DEFAULT_MODEL = "qwen3.8-27b"
API_KEY_ENV = "QWEN38_API_KEY"

METHOD_ROLLING_SUMMARY = "rolling_summary"
METHOD_FULL_CONTEXT = "full_context"


@dataclass(frozen=True)
class ModelConfig:
    """Decoding and transport settings. Never holds the API key."""

    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.0
    enable_thinking: bool = False
    summary_max_tokens: int = 8 * 1024
    answer_max_tokens: int | None = None
    timeout_seconds: float = 1800.0
    max_attempts: int = 3

    @classmethod
    def from_env(cls, **overrides: object) -> "ModelConfig":
        return cls(
            base_url=os.environ.get("QWEN38_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            model=os.environ.get("QWEN38_MODEL", DEFAULT_MODEL),
            **overrides,  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class BudgetConfig:
    rolling_trigger_tokens: int = 100 * 1024
    summary_budget_tokens: int = 8 * 1024
    raw_tail_budget_tokens: int = 16 * 1024
    final_context_budget_tokens: int = 24 * 1024

    def __post_init__(self) -> None:
        if self.summary_budget_tokens + self.raw_tail_budget_tokens > self.final_context_budget_tokens:
            raise ValueError("summary + raw tail budgets must fit the final context budget")
        if self.final_context_budget_tokens >= self.rolling_trigger_tokens:
            raise ValueError("final context budget must be below the rolling trigger")

    @property
    def max_unit_tokens(self) -> int:
        """A history chunk must always be able to sit inside the raw tail alone."""
        return self.raw_tail_budget_tokens


def read_api_key() -> str:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise RuntimeError(f"{API_KEY_ENV} is not set; the key must never be committed")
    return key


def code_version() -> str:
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{rev}-dirty" if dirty else rev


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(model: ModelConfig, budgets: BudgetConfig, method: str, prompt_version: str) -> str:
    """Identity of everything that changes a sample's result.

    Code version is recorded per sample for auditing but stays out of the
    fingerprint so an unrelated edit does not force a full re-run.
    """
    decoding = asdict(model)
    for transport_only in ("timeout_seconds", "max_attempts"):
        decoding.pop(transport_only)
    payload = {
        "method": method,
        "prompt_version": prompt_version,
        "model": decoding,
        "budgets": asdict(budgets),
    }
    return sha256_text(json.dumps(payload, sort_keys=True, ensure_ascii=False))
