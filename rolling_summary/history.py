"""Normalize LongMemEval sessions into one chronological message stream.

The final question and all evidence annotations are deliberately ignored here.
Session boundaries are rendering metadata only; they are never compression
boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Protocol, Sequence

from .config import sha256_text


class Tokenizer(Protocol):
    def count(self, text: str) -> int: ...


@dataclass(frozen=True)
class SourceMessage:
    session_index: int
    message_index: int
    role: str
    content: str

    @property
    def rendered(self) -> str:
        return f"{self.role.capitalize()}: {self.content}"


@dataclass(frozen=True)
class Session:
    session_index: int
    session_id: str
    date: str
    original_index: int
    messages: tuple[SourceMessage, ...]

    @property
    def header(self) -> str:
        return f"## Session {self.session_index + 1} — {self.date}"


@dataclass(frozen=True)
class HistoryMessage:
    """One normalized logical message in the continuous history stream."""

    unit_ordinal: int
    session_index: int
    session_id: str
    session_date: str
    first_message_index: int
    last_message_index: int
    role: str
    content: str
    source_message_indices: tuple[int, ...]
    is_split_degraded: bool = False

    @property
    def rendered(self) -> str:
        return f"{self.role.capitalize()}: {self.content}"

    @property
    def content_sha256(self) -> str:
        return sha256_text(self.content)


# Kept as an internal compatibility name for callers that used the old type.
HistoryUnit = HistoryMessage


@dataclass(frozen=True)
class HistoryStream:
    messages: tuple[HistoryMessage, ...]

    @property
    def legal_cuts(self) -> tuple[int, ...]:
        """Cuts at the start or immediately after a logical assistant message."""
        return (0, *(index for index, message in enumerate(self.messages, 1) if message.role == "assistant"))

    def render(self, start: int = 0, end: int | None = None) -> str:
        return render_messages(self.messages[start:end])

    @property
    def text(self) -> str:
        return self.render()


def parse_session_date(date: str) -> datetime:
    calendar, _weekday, clock = date.split(" ", 2)
    return datetime.strptime(f"{calendar} {clock}", "%Y/%m/%d %H:%M")


def chronological_sessions(row: dict[str, Any]) -> list[Session]:
    """Sort sessions oldest-first while preserving source order on ties."""
    dates = row["haystack_dates"]
    session_ids = row.get("haystack_session_ids") or [""] * len(dates)
    order = sorted(range(len(dates)), key=lambda index: (parse_session_date(dates[index]), index))
    sessions: list[Session] = []
    for position, original_index in enumerate(order):
        messages = tuple(
            SourceMessage(
                session_index=position,
                message_index=message_index,
                role=message["role"],
                content=message.get("content") or "",
            )
            for message_index, message in enumerate(row["haystack_sessions"][original_index])
        )
        sessions.append(Session(position, session_ids[original_index], dates[original_index], original_index, messages))
    return sessions


def render_session(session: Session) -> str:
    return render_messages(_as_history_messages(session.messages, session))


def render_messages(messages: Sequence[HistoryMessage]) -> str:
    """Canonical plain-text history renderer, including headers at session changes."""
    lines: list[str] = []
    previous_session: int | None = None
    for message in messages:
        if message.session_index != previous_session:
            lines.append(f"## Session {message.session_index + 1} — {message.session_date}")
            previous_session = message.session_index
        lines.append(message.rendered)
    return "\n".join(lines)


def render_history(messages: Sequence[HistoryMessage]) -> str:
    return render_messages(messages)


def _as_history_messages(messages: Sequence[SourceMessage], session: Session) -> tuple[HistoryMessage, ...]:
    """Render a session for display without changing its original messages."""
    return tuple(
        HistoryMessage(
            unit_ordinal=index,
            session_index=session.session_index,
            session_id=session.session_id,
            session_date=session.date,
            first_message_index=message.message_index,
            last_message_index=message.message_index,
            role=message.role,
            content=message.content,
            source_message_indices=(message.message_index,),
        )
        for index, message in enumerate(messages)
    )


def build_message_stream(
    sessions: Sequence[Session],
    tokenizer: Tokenizer | None = None,
    max_unit_tokens: int | None = None,
    client=None,
) -> HistoryStream:
    """Merge same-role messages within each session and concatenate sessions.

    ``max_unit_tokens`` is retained for API compatibility. Normal messages are
    never split merely because they exceed the raw-tail budget. A tokenizer
    boundary split is only used when a caller explicitly supplies a client and
    the message itself exceeds that client's model context.
    """
    del tokenizer, max_unit_tokens
    normalized: list[HistoryMessage] = []
    ordinal = 0
    for session in sessions:
        current: HistoryMessage | None = None
        for source in session.messages:
            # 连续同角色消息合并成一个逻辑单元；跨 session 永不合并。
            if current is not None and current.role == source.role:
                current = HistoryMessage(
                    # 合并：单元序号和 session 元数据沿用旧值，
                    # 只扩展 last_message_index、拼接 content 并追加来源索引。
                    unit_ordinal=current.unit_ordinal,
                    session_index=current.session_index,
                    session_id=current.session_id,
                    session_date=current.session_date,
                    first_message_index=current.first_message_index,
                    last_message_index=source.message_index,
                    role=current.role,
                    content=f"{current.content}\n\n{source.content}",
                    source_message_indices=current.source_message_indices + (source.message_index,),
                )
            else:
                # 角色切换或进入新 session：先落盘上一个合并单元，再开启新单元。
                if current is not None:
                    normalized.append(current)
                current = HistoryMessage(
                    unit_ordinal=ordinal,
                    session_index=session.session_index,
                    session_id=session.session_id,
                    session_date=session.date,
                    first_message_index=source.message_index,
                    last_message_index=source.message_index,
                    role=source.role,
                    content=source.content,
                    source_message_indices=(source.message_index,),
                )
                ordinal += 1
        if current is not None:
            normalized.append(current)
    return HistoryStream(tuple(normalized))


def build_units(
    sessions: Sequence[Session],
    tokenizer: Tokenizer,
    max_unit_tokens: int,
    client=None,
) -> Iterator[HistoryMessage]:
    """Compatibility wrapper: yield continuous logical messages, not session chunks."""
    del client
    yield from build_message_stream(sessions, tokenizer, max_unit_tokens).messages
