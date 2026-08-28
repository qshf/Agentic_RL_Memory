"""Re-answer completed V4 samples from their persisted graph audit trail."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_sidecar.data import load_baseline_tail
from memory_sidecar.v4 import V4GraphState, answer_v4_messages, render_v4_graph_all, render_v4_numeric_projection
from utils.client import QwenClient
from utils.config import DEFAULT_BASE_URL, DEFAULT_MODEL, ModelConfig, ROOT
from utils.dataset import load_source
from utils.local_tokenizer import LocalQwenTokenizer
from utils.store import STATUS_COMPLETED, TrajectoryStore
from utils.trajectory import record_call


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source", type=Path, default=ROOT / "data/official_longmemeval/longmemeval_s_cleaned.json")
    parser.add_argument("--baseline-db", type=Path, default=ROOT / "results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/trajectory.sqlite3")
    parser.add_argument("--baseline-run-id", default="rolling-summary-eval120-v1-atomic-c2")
    parser.add_argument("--raw-tail-budget", type=int, default=16 * 1024)
    parser.add_argument("--projection", choices=("graph-all", "numeric-all"), default="graph-all")
    parser.add_argument("--answer-max-tokens", type=int, default=1024)
    parser.add_argument("--base-url", default=os.environ.get("QWEN38_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.environ.get("QWEN38_MODEL", DEFAULT_MODEL))
    parser.add_argument("--api-key-env", default="QWEN38_API_KEY")
    parser.add_argument("--question-id", nargs="*", default=None)
    args = parser.parse_args()
    ids = set(args.question_id or [])
    store = TrajectoryStore(args.db)
    for row in store.conn.execute("select id,question_id from samples where run_id=? and status='completed' order by id", (args.run_id,)):
        if args.question_id is None:
            ids.add(row["question_id"])
    sample_query = "select id,question_id from samples where run_id=? and status='completed'"
    sample_params = [args.run_id]
    if args.question_id is not None:
        placeholders = ",".join("?" for _ in args.question_id) or "NULL"
        sample_query += f" and question_id in ({placeholders})"
        sample_params.extend(args.question_id)
    sample_query += " order by id"
    source = load_source(args.source, ids)
    key = os.environ.get(args.api_key_env, "").strip()
    if not key:
        raise SystemExit(f"{args.api_key_env} is not set")
    tokenizer = LocalQwenTokenizer()
    client = QwenClient(ModelConfig(base_url=args.base_url.rstrip("/"), model=args.model, temperature=0.0, enable_thinking=False, answer_max_tokens=args.answer_max_tokens, timeout_seconds=1800), key)
    for sample_row in store.conn.execute(sample_query, sample_params):
        sample_id, question_id = int(sample_row["id"]), sample_row["question_id"]
        state = V4GraphState(edges=store.load_v4_edges(sample_id))
        for raw in store.conn.execute("select claim_json,source_refs_json,normalization_actions_json from sidecar_v4_raw_claims where sample_id=? order by id", (sample_id,)):
            state.raw_claims.append({"model_claim": json.loads(raw["claim_json"]), "source_refs": json.loads(raw["source_refs_json"]), "normalization_actions": json.loads(raw["normalization_actions_json"])})
        if args.projection == "graph-all":
            graph_context, meta = render_v4_graph_all(state)
            projection_kind = "graph_all_replay_merged_raw"
            projection_filter = {"kind": "all", "replay": True}
            input_edge_ids = [str(edge["edge_id"]) for edge in state.edges if edge.get("status") != "superseded"]
        else:
            graph_context, meta = render_v4_numeric_projection(state)
            projection_kind = "numeric_all_replay"
            projection_filter = {
                "kind": "numeric_all",
                "unfiltered": True,
                "replay": True,
                "aggregate_count": meta["aggregate_count"],
            }
            input_edge_ids = meta["input_edge_ids"]
        tail = load_baseline_tail(args.baseline_db, args.baseline_run_id, question_id, args.raw_tail_budget, tokenizer)
        row = source[question_id]
        prompt = answer_v4_messages(graph_context, tail["raw_tail"], row["question_date"], row["question"])
        answer = client.chat(prompt, max_tokens=args.answer_max_tokens)
        next_call = int(store.conn.execute("select coalesce(max(call_ordinal),0)+1 from calls where sample_id=?", (sample_id,)).fetchone()[0])
        record_call(store, sample_id, next_call, f"answer_{args.projection}_replay", answer)
        projection_ordinal = int(store.conn.execute("select coalesce(max(projection_ordinal), 0) + 1 from sidecar_v4_projections where sample_id=?", (sample_id,)).fetchone()[0])
        store.record_v4_projection(sample_id, projection_ordinal=projection_ordinal, projection_kind=projection_kind, question_signature=None, filter_spec=projection_filter, content=graph_context, input_edge_ids=input_edge_ids, graph_truncated=False)
        store.finish_sample(sample_id, STATUS_COMPLETED, answer_input_tokens=answer.input_tokens, answer_output_tokens=answer.output_tokens, hypothesis=answer.content.strip())
        print(json.dumps({"question_id": question_id, "answer": answer.content.strip(), "projection": meta, "input_tokens": answer.input_tokens}, ensure_ascii=False), flush=True)
    store.export_hypotheses(args.run_id, args.db.parent / "hypotheses-replay.jsonl")
    store.close()


if __name__ == "__main__":
    main()
