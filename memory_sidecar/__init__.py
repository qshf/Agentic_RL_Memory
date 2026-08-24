"""Memory Sidecar 实验实现。

本包负责 Sidecar 专有策略：事件 schema、记忆路由、上下文预算和单样本流程。
不得导入 ``rolling_summary``；传输、数据集、历史、tokenizer 和轨迹能力统一放在
仓库根目录的 ``utils`` 包中。
"""

from .budget import chunks, history_token_count, manager_prompt, trim_tail, turn_units
from .data import load_baseline_tail, record_sidecar_context
from .process import process_one
from .protocol import (
    MemoryState,
    ParsedEvent,
    answer_messages,
    chunk_text,
    manager_messages,
    parse_event,
    state_sha256,
)

__all__ = [
    "MemoryState",
    "ParsedEvent",
    "answer_messages",
    "chunk_text",
    "chunks",
    "history_token_count",
    "load_baseline_tail",
    "manager_messages",
    "manager_prompt",
    "parse_event",
    "process_one",
    "record_sidecar_context",
    "state_sha256",
    "trim_tail",
    "turn_units",
]
