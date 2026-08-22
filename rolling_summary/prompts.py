"""Versioned prompts for the Rolling Summary baseline.

The summary prompt is query-independent by construction: it is rendered from
the previous memory and the evicted history only, and never receives the
question, the question date, or any gold field.
"""
from __future__ import annotations

PROMPT_VERSION = "rolling-summary-v1-atomic-facts-question-last"

SUMMARY_SYSTEM = (
    "You maintain a long-term memory of an ongoing conversation between a user and an "
    "assistant. You are shown the current memory and a block of older conversation history "
    "that is about to be discarded, and you rewrite them into one consolidated memory."
)

SUMMARY_INSTRUCTIONS = """Rewrite the current memory and the older history below into a single consolidated memory of at most {budget} tokens.

Use this priority order:
1. Atomic facts: preserve each answerable fact as a compact labeled record in the form
   [date if known | source] entity | attribute or relationship | exact value | qualifier.
   The qualifier carries scope such as "first order", "in the first three months",
   "by July", "last week", or "at time of death".
2. Exact user facts: names, places, organizations, products, quantities, prices,
   dates, times, durations, identifiers, and relationships.
3. Preferences and constraints: likes, dislikes, habits, goals, requirements,
   and the context in which each preference applies.
4. Temporal state: events in chronological order, plans, completed actions,
   current status, and changes from an older value to a newer value.
5. Multi-step facts: counts, comparisons, rankings, sequences, causes, and outcomes.
6. Concrete assistant-provided facts: preserve named entity definitions, lists,
   calculations, comparisons, and procedures even if the user did not explicitly
   accept them. Label these records "assistant-sourced" rather than converting them
   into user facts.

Rules:
- Preserve atomic facts before writing a broad profile or topical summary.
- Keep exact names, numbers, dates, units, and quoted terms unchanged.
- Never replace an exact value with a range, approximation, or plus sign. Do not
  drop a count's scope, date, comparison target, or ordering condition.
- Carry forward current memory unless it is superseded.
- When facts conflict, retain each incompatible value with its source and date.
  Do not silently choose one or merge them into a new value.
- Keep the session date attached to every atomic fact whose chronology or scope
  could affect a later answer.
- Remove greetings, small talk, generic advice, repeated confirmations, and filler.
- Do not invent, speculate, or filter toward a particular final question.
- The final question, answer, and evidence labels are unavailable and must not be inferred.
- Output only compact labeled notes for a future assistant. Do not explain the compression
  and do not add a preamble."""

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

Use only the memory and history above. For a direct factual, numeric, comparison,
ranking, or arithmetic question, reconcile the relevant evidence before answering and
give the exact final conclusion first. Do not replace an exact value with an estimate,
and never give a conclusion that contradicts the values stated in the same answer.
When dated facts conflict, use the latest applicable dated fact no later than the
current date; only mention the conflict when it prevents a determinate answer.
Reply with the answer itself, concisely and directly, with no preamble and no
explanation of the context.

# Question
{question}"""

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

Use only the conversation history above. For a direct factual, numeric, comparison,
ranking, or arithmetic question, reconcile the relevant evidence before answering and
give the exact final conclusion first. Do not replace an exact value with an estimate,
and never give a conclusion that contradicts the values stated in the same answer.
When dated facts conflict, use the latest applicable dated fact no later than the
current date; only mention the conflict when it prevents a determinate answer.
Reply with the answer itself, concisely and directly, with no preamble and no
explanation of the context.

# Question
{question}"""


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
