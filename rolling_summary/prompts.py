"""Versioned prompts for the Rolling Summary baseline.

The summary prompt is query-independent by construction: it is rendered from
the previous memory and the evicted history only, and never receives the
question, the question date, or any gold field.
"""
from __future__ import annotations

PROMPT_VERSION = "rolling-summary-v1"

SUMMARY_SYSTEM = (
    "You maintain a long-term memory of an ongoing conversation between a user and an "
    "assistant. You are shown the current memory and a block of older conversation history "
    "that is about to be discarded, and you rewrite them into one consolidated memory."
)

SUMMARY_INSTRUCTIONS = """Rewrite the current memory and the older history below into a single consolidated memory of at most {budget} tokens.

Requirements:
- Faithfully preserve user facts, preferences, plans, dates and times, relationships, numbers, and state changes.
- Preserve information the assistant explicitly provided that the user may refer back to later.
- When something is contradicted or updated, keep both the old and the new value and say when each was stated.
- Keep the session date attached to the facts that came from that session.
- Do not invent anything, do not speculate about what may be asked later, and do not filter the content toward any particular topic.
- Carry forward everything already in the current memory that is not superseded; the discarded history will not be available again.
- Output the memory itself as compact notes for a future assistant. Do not explain the compression, and do not add a preamble."""

SUMMARY_USER = """{instructions}

# Current memory
{memory}

# Older conversation history to fold in
{history}"""

EMPTY_MEMORY = "(empty — this is the first compression)"

ANSWER_SYSTEM = (
    "You answer a question about a long conversation history between a user and you. You are "
    "given a compressed memory of the older history and the verbatim most recent history."
)

ANSWER_USER = """# Memory of older conversation history
{memory}

# Most recent conversation history (verbatim)
{raw_tail}

# Current date
{question_date}

# Question
{question}

Answer the question using only the memory and history above. Reply with the answer itself, concisely and directly, with no preamble and no explanation of the context."""

NO_MEMORY = "(no older history was compressed; the full history is shown below)"
NO_RAW_TAIL = "(none)"

FULL_CONTEXT_SYSTEM = (
    "You answer a question about a long conversation history between a user and you. You are "
    "given the complete history verbatim."
)

FULL_CONTEXT_USER = """# Conversation history (verbatim)
{history}

# Current date
{question_date}

# Question
{question}

Answer the question using only the history above. Reply with the answer itself, concisely and directly, with no preamble and no explanation of the context."""


def summary_messages(memory: str, history: str, budget_tokens: int) -> list[dict[str, str]]:
    instructions = SUMMARY_INSTRUCTIONS.format(budget=budget_tokens)
    user = SUMMARY_USER.format(
        instructions=instructions,
        memory=memory.strip() or EMPTY_MEMORY,
        history=history,
    )
    return [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": user},
    ]


def answer_messages(memory: str, raw_tail: str, question_date: str, question: str) -> list[dict[str, str]]:
    user = ANSWER_USER.format(
        memory=memory.strip() or NO_MEMORY,
        raw_tail=raw_tail.strip() or NO_RAW_TAIL,
        question_date=question_date,
        question=question,
    )
    return [
        {"role": "system", "content": ANSWER_SYSTEM},
        {"role": "user", "content": user},
    ]


def full_context_messages(history: str, question_date: str, question: str) -> list[dict[str, str]]:
    user = FULL_CONTEXT_USER.format(
        history=history,
        question_date=question_date,
        question=question,
    )
    return [
        {"role": "system", "content": FULL_CONTEXT_SYSTEM},
        {"role": "user", "content": user},
    ]
