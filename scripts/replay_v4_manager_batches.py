"""Replay persisted V4 Manager batches without calling an LLM."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_sidecar.budget import chunks
from memory_sidecar.v3 import compile_evidence
from memory_sidecar.v4 import V4GraphState, normalize_v4_claim, parse_v4_manager_response, render_v4_manager_state
from utils.config import ROOT, sha256_text
from utils.dataset import load_source
from utils.history import build_message_stream, chronological_sessions
from utils.local_tokenizer import LocalQwenTokenizer
from utils.store import TrajectoryStore


DEFAULT_SOURCE = ROOT / "data" / "official_longmemeval" / "longmemeval_s_cleaned.json"


def _canonical_edges(edges: list[dict]) -> str:
    return json.dumps(edges, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def replay_sample(store: TrajectoryStore, sample_row, source_row: dict, *, chunk_budget: int, max_edges: int) -> dict:
    sample_id = int(sample_row["id"])
    batch_rows = store.conn.execute(
        "SELECT * FROM sidecar_v4_batches WHERE sample_id=? ORDER BY batch_ordinal", (sample_id,)
    ).fetchall()
    stream = build_message_stream(chronological_sessions(source_row)).messages
    tokenizer = LocalQwenTokenizer()
    current_chunks = chunks(tuple(stream), chunk_budget, tokenizer)
    state = V4GraphState()
    statuses: list[str] = []
    snapshot_mismatches = 0
    unavailable = 0
    for index, batch in enumerate(batch_rows):
        if index >= len(current_chunks):
            statuses.append("missing_compiled_chunk")
            unavailable += 1
            continue
        compiled = compile_evidence(current_chunks[index])
        if sha256_text(compiled.text) != batch["input_hash"]:
            statuses.append("input_hash_mismatch")
            unavailable += 1
            continue
        try:
            before = json.loads(batch["memory_before_json"])
            saved_edges = before.get("edges")
            if saved_edges is None:
                statuses.append("missing_full_before_state")
                unavailable += 1
            elif _canonical_edges(saved_edges) != _canonical_edges(state.edges):
                statuses.append("before_state_mismatch")
                snapshot_mismatches += 1
            render_v4_manager_state(state, max_edges=max_edges, current_text=compiled.text)
            if not batch["raw_response"]:
                statuses.append("missing_raw_response")
                unavailable += 1
                continue
            claims = parse_v4_manager_response(batch["raw_response"], compiled)
            normalized = [normalize_v4_claim(claim, compiled, ordinal=ordinal, batch_ordinal=int(batch["batch_ordinal"])) for ordinal, claim in enumerate(claims)]
            for claim in normalized:
                state.route(claim)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            statuses.append(f"replay_error:{type(exc).__name__}")
            unavailable += 1
    expected = store.load_v4_edges(sample_id)
    edge_match = _canonical_edges(expected) == _canonical_edges(state.edges)
    status = "passed" if edge_match and unavailable == 0 and snapshot_mismatches == 0 else "failed"
    return {
        "sample_id": sample_id,
        "question_id": sample_row["question_id"],
        "status": status,
        "batch_count": len(batch_rows),
        "replayed_edge_count": len(state.edges),
        "persisted_edge_count": len(expected),
        "edge_match": edge_match,
        "snapshot_mismatches": snapshot_mismatches,
        "unavailable_batch_count": unavailable,
        "statuses": statuses,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--chunk-budget", type=int, default=2048)
    parser.add_argument("--manager-graph-max-edges", type=int, default=64)
    parser.add_argument("--question-id", nargs="*", default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    store = TrajectoryStore(args.db)
    query = "SELECT id, question_id, status FROM samples WHERE run_id=?"
    params: list[object] = [args.run_id]
    if args.question_id is not None:
        placeholders = ",".join("?" for _ in args.question_id) or "NULL"
        query += f" AND question_id IN ({placeholders})"
        params.extend(args.question_id)
    query += " ORDER BY id"
    sample_rows = store.conn.execute(query, params).fetchall()
    source = load_source(args.source, {row["question_id"] for row in sample_rows})
    results = [replay_sample(store, row, source[row["question_id"]], chunk_budget=args.chunk_budget, max_edges=args.manager_graph_max_edges) for row in sample_rows]
    output = {"run_id": args.run_id, "results": results}
    text = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)
    store.close()


if __name__ == "__main__":
    main()
