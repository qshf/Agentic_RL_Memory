"""共用的模型配置、provenance 与哈希工具。"""
from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "http://117.186.43.62:5027/v1"
DEFAULT_MODEL = "qwen3.8-27b"
API_KEY_ENV = "QWEN38_API_KEY"


@dataclass(frozen=True)
class ModelConfig:
    """解码与传输配置，不保存 API key。"""

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
