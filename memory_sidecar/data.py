"""Memory Sidecar 的数据集与基线 tail 读取。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from utils.config import sha256_text
from utils.local_tokenizer import LocalQwenTokenizer


def load_baseline_tail(
    db_path: Path,
    run_id: str,
    question_id: str,
    tail_budget_tokens: int,
    tokenizer: LocalQwenTokenizer,
) -> dict[str, Any]:
    """重建最终 V1 tail，并使用本地 tokenizer 裁剪。

    Sidecar 不会把 V1 tail 正文复制到自己的 context 表，只保存本次重建的 provenance 和
    hash。正文仍位于基线数据库，可由记录的源行复现。
    """
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        sample = connection.execute(
            "SELECT id FROM samples WHERE run_id=? AND question_id=? "
            "ORDER BY attempt DESC, id DESC LIMIT 1",
            (run_id, question_id),
        ).fetchone()
        if sample is None:
            raise ValueError(f"baseline sample not found: {run_id}/{question_id}")
        sample_id = int(sample["id"])
        final = connection.execute(
            "SELECT step_ordinal, summary_text FROM states WHERE sample_id=? AND event='final' "
            "ORDER BY step_ordinal DESC LIMIT 1",
            (sample_id,),
        ).fetchone()
        if final is None:
            raise ValueError(f"baseline final state not found for sample {sample_id}")
        compression = connection.execute(
            "SELECT step_ordinal, detail FROM states WHERE sample_id=? AND event='rolling_compression' "
            "ORDER BY step_ordinal DESC LIMIT 1",
            (sample_id,),
        ).fetchone()
        cut_index = -1
        compression_step = None
        if compression is not None:
            compression_step = int(compression["step_ordinal"])
            detail = json.loads(compression["detail"] or "{}")
            cut_index = int(detail.get("cut_index", -1))

        ingest_rows = connection.execute(
            "SELECT step_ordinal, raw_text, detail FROM states WHERE sample_id=? AND event='ingest' "
            "ORDER BY step_ordinal",
            (sample_id,),
        ).fetchall()
        tail_parts: list[str] = []
        source_ordinals: list[int] = []
        # 最后一次 V1 compression 的 cut 定义仍可见的近期 tail；cut 之前（含 cut）的
        # ingest 行刻意排除。
        for row in ingest_rows:
            detail = json.loads(row["detail"] or "{}")
            ordinal = int(detail.get("unit_ordinal", -1))
            if ordinal > cut_index and row["raw_text"]:
                tail_parts.append(row["raw_text"])
                source_ordinals.append(ordinal)
        full_tail = "\n".join(tail_parts)
        full_tail_tokens = tokenizer.count(full_tail)
        if tail_budget_tokens > 0 and full_tail_tokens > tail_budget_tokens:
            token_ids = tokenizer.encode(full_tail)
            tail = tokenizer.decode(token_ids[-tail_budget_tokens:])
            trimmed = True
        else:
            tail = full_tail
            trimmed = False
        return {
            "baseline_sample_id": sample_id,
            "baseline_final_step": int(final["step_ordinal"]),
            "baseline_compression_step": compression_step,
            "baseline_cut_index": cut_index,
            "summary_text": final["summary_text"] or "",
            "raw_tail": tail,
            "raw_tail_sha256": sha256_text(tail),
            "raw_tail_tokens": tokenizer.count(tail),
            "raw_tail_full_tokens": full_tail_tokens,
            "raw_tail_trimmed": trimmed,
            "tail_source_ordinals": source_ordinals,
        }
    finally:
        connection.close()


def record_sidecar_context(store: Any, sample_id: int, tail: dict[str, Any]) -> None:
    """持久化基线 provenance，但不重复写入 raw tail 正文。"""
    store.record_sidecar_context(
        sample_id,
        baseline_sample_id=tail["baseline_sample_id"],
        baseline_final_step=tail["baseline_final_step"],
        baseline_compression_step=tail["baseline_compression_step"],
        baseline_cut_index=tail["baseline_cut_index"],
        raw_tail_sha256=tail["raw_tail_sha256"],
        raw_tail_tokens=tail["raw_tail_tokens"],
        raw_tail_full_tokens=tail["raw_tail_full_tokens"],
        raw_tail_trimmed=tail["raw_tail_trimmed"],
        tail_source_ordinals=tail["tail_source_ordinals"],
    )
