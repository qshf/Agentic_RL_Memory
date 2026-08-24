"""通用本地 tokenizer 协议和消息计数工具。"""
from __future__ import annotations

from typing import Protocol, Sequence


class Tokenizer(Protocol):
    def count(self, text: str) -> int: ...

    def encode(self, text: str) -> list[int]: ...

    def decode(self, token_ids: list[int]) -> str: ...


def count_messages(messages: Sequence[dict[str, str]], tokenizer: Tokenizer) -> int:
    """使用注入的本地 tokenizer 统计消息正文。"""
    return sum(tokenizer.count(message["content"]) for message in messages)
