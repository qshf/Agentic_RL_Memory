"""使用官方 Qwen3.8-27B tokenizer 在本地统计 token。

首次使用时经 ``huggingface_hub`` 懒加载一次 ``tokenizer.json``，此后在本地计数，
从热路径中移除逐次 ``/tokenize`` POST；``count`` 变为纯本地 O(text) BPE 编码。
"""
from __future__ import annotations

from pathlib import Path


class LocalQwenTokenizer:
    """实现引擎 ``Tokenizer`` 协议的 ``count(text) -> int``。"""

    def __init__(
        self,
        repo_id: str = "Qwen/Qwen3.8-27B",
        tokenizer_path: str | None = None,
    ) -> None:
        self._repo_id = repo_id
        self._tokenizer_path = tokenizer_path  # 显式本地文件路径时不访问网络
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
        return len(self.encode(text))

    def encode(self, text: str) -> list[int]:
        """在本地编码文本并返回 token id。

        Sidecar runner 用它做预算统计和确定性 tail 裁剪，因此不会对每个候选项请求模型服务。
        """
        if not text:
            return []
        return list(self._load().encode(text).ids)

    def decode(self, token_ids: list[int]) -> str:
        """把本地产生的 token id 解码为文本。"""
        if not token_ids:
            return ""
        return self._load().decode(token_ids)

    @property
    def tokenizer_path(self) -> Path | None:
        """首次加载后返回解析出的本地路径；加载前为 None。"""
        return Path(self._tokenizer_path) if self._tokenizer_path else None

    def clone(self) -> "LocalQwenTokenizer":
        """创建独立 encoder，并复用已下载的 tokenizer 文件。"""
        self._load()
        assert self._tokenizer_path is not None
        return LocalQwenTokenizer(repo_id=self._repo_id, tokenizer_path=self._tokenizer_path)
