"""Memory Sidecar 实验实现。

本包负责 Sidecar 专有策略：事件 schema、记忆路由、上下文预算和单样本流程。
不得导入 ``rolling_summary``；传输、数据集、历史、tokenizer 和轨迹能力统一放在
仓库根目录的 ``utils`` 包中。
"""

from .budget import chunks, history_token_count, manager_prompt, trim_tail, turn_units
from .data import load_baseline_tail, record_sidecar_context
from .process import process_one
from .process_v2 import process_one_v2
from .process_v3 import process_one_v3
from .protocol import (
    MemoryState,
    ParsedEvent,
    answer_messages,
    chunk_text,
    manager_messages,
    parse_event,
    state_sha256,
)
from .v2 import (
    CompiledEvidence,
    Evidence,
    EvidenceCompiler,
    V2MemoryState,
    V2State,
    compile_evidence,
    fit_answer_v2_context,
    manager_v2_messages,
    parse_manager_response,
    parse_reconciliation_response,
    reconciler_v2_messages,
    parse_v2_response,
    resolve_time,
    state_sha256_v2,
    validate_event,
)
from .v3 import V3MemoryState, state_sha256_v3
from .v4 import (
    NormalizedV4Claim,
    V4Claim,
    V4GraphState,
    answer_v4_messages,
    merge_v4_edges,
    normalize_v4_claim,
    parse_v4_manager_response,
    render_v4_graph_all,
    render_v4_numeric_projection,
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
    "process_one_v2",
    "process_one_v3",
    "record_sidecar_context",
    "state_sha256",
    "trim_tail",
    "turn_units",
    "CompiledEvidence",
    "Evidence",
    "EvidenceCompiler",
    "V2MemoryState",
    "V2State",
    "compile_evidence",
    "fit_answer_v2_context",
    "manager_v2_messages",
    "parse_manager_response",
    "parse_reconciliation_response",
    "reconciler_v2_messages",
    "parse_v2_response",
    "resolve_time",
    "state_sha256_v2",
    "validate_event",
    "V3MemoryState",
    "state_sha256_v3",
    "V4Claim",
    "NormalizedV4Claim",
    "V4GraphState",
    "normalize_v4_claim",
    "parse_v4_manager_response",
    "merge_v4_edges",
    "render_v4_graph_all",
    "render_v4_numeric_projection",
    "answer_v4_messages",
]
