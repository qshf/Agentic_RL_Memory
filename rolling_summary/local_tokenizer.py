"""Offline token counting with the official Qwen3.8-27B tokenizer.

Downloads only the tokenizer artifacts (``tokenizer.json``) once via
``huggingface_hub``, lazily on first use, then counts tokens locally. This
removes the per-call ``/tokenize`` POST from the rolling engine's hot path:
``count`` becomes a pure local O(text) BPE encode.
"""
from __future__ import annotations

from pathlib import Path


class LocalQwenTokenizer:
    """``count(text) -> int`` drop-in for the engine's Tokenizer protocol."""

    def __init__(
        self,
        repo_id: str = "Qwen/Qwen3.8-27B",
        tokenizer_path: str | None = None,
    ) -> None:
        self._repo_id = repo_id
        self._tokenizer_path = tokenizer_path  # explicit local file skips the network
        self._tokenizer = None

    def _load(self):
        if self._tokenizer is not None:
            return self._tokenizer
        if self._tokenizer_path is None:
            from huggingface_hub import hf_hub_download

            self._tokenizer_path = hf_hub_download(
                repo_id=self._repo_id,
                filename="tokenizer.json",
                library_name="agentic-rl-memory",
            )
        from tokenizers import Tokenizer

        self._tokenizer = Tokenizer.from_file(str(self._tokenizer_path))
        return self._tokenizer

    def count(self, text: str) -> int:
        if not text:
            return 0
        tokenizer = self._load()
        return len(tokenizer.encode(text).ids)

    @property
    def tokenizer_path(self) -> Path | None:
        """Resolved local path after the first load (None until then)."""
        return Path(self._tokenizer_path) if self._tokenizer_path else None
