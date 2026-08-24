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
    role: str
    content: str
    is_split_degraded: bool = False

    @property
    def rendered(self) -> str:
        return f"{self.role.capitalize()}: {self.content}"

    @property
    def session_header(self) -> str:
        return f"## Session {self.session_index + 1} — {self.session_date}"

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
                role=message["role"],
                content=message.get("content") or "",
            )
            for message in row["haystack_sessions"][original_index]
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
            role=message.role,
            content=message.content,
        )
        for index, message in enumerate(messages)
    )


def build_message_stream(
    sessions: Sequence[Session],
    tokenizer: Tokenizer | None = None,
    max_unit_tokens: int | None = None,
    client=None,
) -> HistoryStream:
    """Concatenate the already-cleaned sessions into one message stream.

    ``max_unit_tokens`` is retained for API compatibility. Normal messages are
    never split merely because they exceed the raw-tail budget.
    """
    del tokenizer, max_unit_tokens
    normalized: list[HistoryMessage] = []
    for session in sessions:
        for source in session.messages:
            normalized.append(
                HistoryMessage(
                    unit_ordinal=len(normalized),
                    session_index=session.session_index,
                    session_id=session.session_id,
                    session_date=session.date,
                    role=source.role,
                    content=source.content,
                )
            )
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
