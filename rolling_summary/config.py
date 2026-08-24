"""Frozen run configuration for the Rolling Summary V1 baseline.

Budgets use K = 1024 tokens, matching the 8,192-token summary generation cap
fixed by the experiment protocol.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from utils.config import (
    API_KEY_ENV,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ROOT,
    ModelConfig,
    code_version,
    file_sha256,
    read_api_key,
    sha256_text,
)

METHOD_ROLLING_SUMMARY = "rolling_summary"
METHOD_FULL_CONTEXT = "full_context"


@dataclass(frozen=True)
class BudgetConfig:
    rolling_trigger_tokens: int = 80 * 1024
    summary_budget_tokens: int = 8 * 1024
    compress_prefix_tokens: int = 80 * 1024


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
