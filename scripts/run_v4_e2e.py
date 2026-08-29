"""Run the online V4 graph-all pipeline and answer selected samples."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_sidecar.budget import chunks
from memory_sidecar.data import load_baseline_tail, record_sidecar_context
from memory_sidecar.v3 import compile_evidence
from memory_sidecar.v4 import (
    V4GraphState,
    answer_v4_messages,
    manager_v4_messages,
    normalize_v4_claim,
    parse_v4_manager_response,
    render_v4_graph_all,
    render_v4_manager_state,
    render_v4_numeric_projection,
    render_v4_query_projection,
)
from utils.client import ApiError, QwenClient
from utils.config import DEFAULT_BASE_URL, DEFAULT_MODEL, ModelConfig, ROOT, sha256_text
from utils.dataset import load_source
from utils.history import build_message_stream, chronological_sessions
from utils.local_tokenizer import LocalQwenTokenizer
from utils.store import STATUS_COMPLETED, STATUS_FAILED, STATUS_NOT_RUNNABLE, TrajectoryStore
from utils.trajectory import record_call


DEFAULT_IDS = ("gpt4_d84a3211", "67e0d0f2")
DEFAULT_SOURCE = ROOT / "data" / "official_longmemeval" / "longmemeval_s_cleaned.json"
DEFAULT_BASELINE_DB = ROOT / "results" / "rolling_summary" / "rolling-summary-eval120-v1-atomic-c2" / "trajectory.sqlite3"
DEFAULT_DB = ROOT / "results" / "memory_sidecar" / "sidecar-v4-e2e-2samples-20260828" / "trajectory.sqlite3"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--baseline-db", type=Path, default=DEFAULT_BASELINE_DB)
    parser.add_argument("--baseline-run-id", default="rolling-summary-eval120-v1-atomic-c2")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--run-id", default="sidecar-v4-e2e-2samples-20260828")
    parser.add_argument("--chunk-budget", type=int, default=2048)
    parser.add_argument("--manager-max-tokens", type=int, default=2048)
    parser.add_argument("--answer-max-tokens", type=int, default=1024)
    parser.add_argument("--raw-tail-budget", type=int, default=16 * 1024)
    parser.add_argument(
        "--projection",
        choices=("graph-all", "numeric-all", "query-auto"),
        default="graph-all",
        help="Answer projection arm. numeric-all renders unfiltered numeric facts and aggregates only.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--base-url", default=os.environ.get("QWEN38_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.environ.get("QWEN38_MODEL", DEFAULT_MODEL))
    parser.add_argument("--api-key-env", default="QWEN38_API_KEY")
    parser.add_argument("--question-id", nargs="*", default=list(DEFAULT_IDS))
    return parser.parse_args()


def _claim_rows(claims, routes):
    rows = []
    for claim, route in zip(claims, routes, strict=True):
        rows.append({
            "claim_id": claim.claim_id,
            "model_claim": claim.model_claim,
            "normalized_claim": {
                "parse_status": claim.parse_status,
                "predicate": claim.predicate,
                "subject": claim.subject,
                "object": claim.object,
                "attributes": claim.attributes,
                "time_json": claim.time_json,
                "scope_json": claim.scope_json,
            },
            "parse_status": claim.parse_status,
            "route_status": route.get("route_status"),
            "route_result": route,
            "source_refs": list(claim.source_refs),
            "normalization_actions": list(claim.normalization_actions),
        })
    return rows


def main() -> None:
    args = parse_args()
    if not args.question_id:
        raise SystemExit("at least one --question-id is required")
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise SystemExit(f"{args.api_key_env} is not set")
    rows = load_source(args.source, set(args.question_id))
    tokenizer = LocalQwenTokenizer()
    model = ModelConfig(
        base_url=args.base_url.rstrip("/"), model=args.model, temperature=0.0,
        enable_thinking=False, summary_max_tokens=args.manager_max_tokens,
        answer_max_tokens=args.answer_max_tokens, timeout_seconds=args.timeout_seconds,
    )
    client = QwenClient(model, api_key)
    preflight = client.preflight()
    config = {
        "method": "memory_sidecar_v4_e2e",
        "model": {"model": args.model, "base_url": args.base_url},
        "chunk_budget_tokens": args.chunk_budget,
        "manager_max_tokens": args.manager_max_tokens,
        "manager_state": "stateless_program_routed",
        "manager_prompt_version": "v4-minimal-claims-stateless-v2",
        "answer_max_tokens": args.answer_max_tokens,
        "raw_tail_budget": args.raw_tail_budget,
        "baseline_run_id": args.baseline_run_id,
        "projection": args.projection,
        "preflight": preflight,
    }
    config["config_fingerprint"] = sha256_text(json.dumps(config, sort_keys=True))
    args.db.parent.mkdir(parents=True, exist_ok=True)
    store = TrajectoryStore(args.db)
    store.start_run(args.run_id, config)
    output = {"run_id": args.run_id, "chunk_budget": args.chunk_budget, "samples": []}

    for question_id in args.question_id:
        row = rows[question_id]
        started = time.monotonic()
        sample_id = store.start_sample(
            args.run_id, question_id, dataset_index=row.get("dataset_index"),
            question_type=row["question_type"], config_fingerprint=config["config_fingerprint"],
            code_version=f"v4-e2e-{args.projection}",
        )
        state = V4GraphState()
        stream = build_message_stream(chronological_sessions(row)).messages
        tail = load_baseline_tail(args.baseline_db, args.baseline_run_id, question_id, args.raw_tail_budget, tokenizer)
        record_sidecar_context(store, sample_id, tail)
        chunk_rows = []
        manager_input = manager_output = call_ordinal = 0
        try:
            for chunk_ordinal, current in enumerate(chunks(tuple(stream), args.chunk_budget, tokenizer), 1):
                compiled = compile_evidence(current)
                manager_context = render_v4_manager_state(state, current_text=compiled.text)
                before = json.dumps({
                    "edges": state.edges,
                    "raw_claims": state.raw_claims,
                    "manager_context": json.loads(manager_context),
                }, ensure_ascii=False, sort_keys=True)
                call_ordinal += 1
                response = client.chat(manager_v4_messages(state, compiled), max_tokens=args.manager_max_tokens)
                manager_input += response.input_tokens
                manager_output += response.output_tokens
                record_call(store, sample_id, call_ordinal, "sidecar_manager", response)
                try:
                    claims = parse_v4_manager_response(response.content, compiled)
                    normalized = [normalize_v4_claim(claim, compiled, ordinal=index) for index, claim in enumerate(claims)]
                    routes = [state.route(claim) for claim in normalized]
                    parse_status = "ok"
                    claim_rows = _claim_rows(normalized, routes)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    claims, normalized, routes, claim_rows = [], [], [], []
                    parse_status = f"parse_error:{type(exc).__name__}"
                store.record_v4_batch(
                    sample_id, batch_ordinal=chunk_ordinal, input_hash=sha256_text(compiled.text),
                    source_unit_ordinals=[message.unit_ordinal for message in current], input_text=compiled.text,
                    memory_before_json=before, raw_response=response.content, parse_status=parse_status,
                    claims=claim_rows, edges=state.edges, raw_claims=state.raw_claims,
                    quarantine_claims=state.quarantine_claims,
                )
                chunk_rows.append({
                    "chunk_ordinal": chunk_ordinal, "evidence_count": len(compiled.evidence),
                    "evidence_chars": len(compiled.text), "claims": len(normalized),
                    "routes": Counter(route.get("route_status") for route in routes),
                    "input_tokens": response.input_tokens, "output_tokens": response.output_tokens,
                    "parse_status": parse_status,
                    "manager_graph_edge_count": len(json.loads(manager_context)["edges"]),
                    "manager_graph_truncated": json.loads(manager_context)["truncated"],
                })

            if args.projection == "graph-all":
                graph_context, projection_meta = render_v4_graph_all(state)
                projection_kind = "graph_all"
                projection_filter = {"kind": "all"}
                projection_edges = [edge for edge in state.edges if edge.get("status") != "superseded"]
            elif args.projection == "numeric-all":
                graph_context, projection_meta = render_v4_numeric_projection(state)
                projection_kind = "numeric_all"
                projection_filter = {
                    "kind": "numeric_all",
                    "unfiltered": True,
                    "aggregate_count": projection_meta["aggregate_count"],
                }
                projection_edge_ids = set(projection_meta["input_edge_ids"])
                projection_edges = [edge for edge in state.edges if str(edge.get("edge_id")) in projection_edge_ids]
            else:
                graph_context, projection_meta = render_v4_query_projection(state, row["question"])
                projection_kind = f"query_auto_{projection_meta['projection_kind']}"
                projection_filter = {"kind": "query_auto", "query_shape": projection_meta["projection_kind"]}
                projection_edge_ids = set(projection_meta.get("input_edge_ids", []))
                projection_edges = [edge for edge in state.edges if str(edge.get("edge_id")) in projection_edge_ids]
            store.record_v4_projection(
                sample_id, projection_ordinal=1, projection_kind=projection_kind,
                question_signature=sha256_text(row["question"]), filter_spec=projection_filter,
                content=graph_context, input_edge_ids=[str(edge["edge_id"]) for edge in projection_edges],
                graph_truncated=False,
            )
            answer_prompt = answer_v4_messages(graph_context, tail["raw_tail"], row["question_date"], row["question"])
            answer_input_tokens = client.tokenize_messages(answer_prompt)
            if answer_input_tokens + args.answer_max_tokens >= client.max_model_len:
                reason = f"answer prompt plus output {answer_input_tokens + args.answer_max_tokens} >= max_model_len {client.max_model_len}"
                call_ordinal += 1
                store.record_call(sample_id, call_ordinal=call_ordinal, attempt=0, kind="answer", status="not_runnable", request_params={}, prompt_tokens_estimated=answer_input_tokens, error=reason)
                store.finish_sample(sample_id, STATUS_NOT_RUNNABLE, answer_input_tokens=answer_input_tokens, total_input_tokens=manager_input, total_output_tokens=manager_output, call_count=call_ordinal, error=reason)
                result = {"question_id": question_id, "status": STATUS_NOT_RUNNABLE, "error": reason, "projection": projection_meta}
            else:
                call_ordinal += 1
                answer = client.chat(answer_prompt, max_tokens=args.answer_max_tokens)
                record_call(store, sample_id, call_ordinal, "answer", answer)
                store.finish_sample(
                    sample_id, STATUS_COMPLETED, full_history_tokens=tokenizer.count("\n".join(message.content for message in stream)),
                    summary_tokens=tokenizer.count(graph_context), raw_tail_tokens=tail["raw_tail_tokens"],
                    answer_input_tokens=answer.input_tokens, answer_output_tokens=answer.output_tokens,
                    total_input_tokens=manager_input + answer.input_tokens, total_output_tokens=manager_output + answer.output_tokens,
                    call_count=call_ordinal, latency_ms=int((time.monotonic() - started) * 1000), hypothesis=answer.content.strip(),
                )
                result = {"question_id": question_id, "status": STATUS_COMPLETED, "hypothesis": answer.content.strip(), "projection": projection_meta}
        except (ApiError, ValueError, OSError) as exc:
            store.finish_sample(sample_id, STATUS_FAILED, error=f"{type(exc).__name__}: {exc}", call_count=call_ordinal)
            result = {"question_id": question_id, "status": STATUS_FAILED, "error": f"{type(exc).__name__}: {exc}"}
        output["samples"].append({**result, "chunks": chunk_rows, "edge_count": len(state.edges), "raw_claim_count": len(state.raw_claims), "quarantine_count": len(state.quarantine_claims)})
        print(json.dumps(output["samples"][-1], ensure_ascii=False), flush=True)

    run_dir = args.db.parent
    output_path = args.output or (run_dir / "e2e.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=list), encoding="utf-8")
    hypotheses = run_dir / "hypotheses.jsonl"
    store.export_hypotheses(args.run_id, hypotheses)
    store.close()
    print(f"wrote {output_path}")
    print(f"hypotheses: {hypotheses}")


if __name__ == "__main__":
    main()
