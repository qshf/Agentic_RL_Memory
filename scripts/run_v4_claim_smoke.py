"""Run the V4 minimal-claim parser/router on four real LongMemEval samples."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_sidecar.budget import chunks
from memory_sidecar.v3 import compile_evidence
from memory_sidecar.v4 import (
    V4GraphState,
    manager_v4_messages,
    normalize_v4_claim,
    parse_v4_manager_response,
)
from utils.client import QwenClient
from utils.config import DEFAULT_BASE_URL, DEFAULT_MODEL, ModelConfig, ROOT, sha256_text
from utils.dataset import load_source
from utils.history import build_message_stream, chronological_sessions
from utils.local_tokenizer import LocalQwenTokenizer
from utils.store import STATUS_COMPLETED, TrajectoryStore


DEFAULT_IDS = ("gpt4_d84a3211", "67e0d0f2", "dad224aa", "gpt4_2ba83207")
DEFAULT_SOURCE = ROOT / "data" / "official_longmemeval" / "longmemeval_s_cleaned.json"
DEFAULT_OUTPUT = ROOT / "results" / "memory_sidecar" / "sidecar-v4-claim-smoke-20260828.json"
DEFAULT_DB = ROOT / "results" / "memory_sidecar" / "sidecar-v4-claim-smoke-20260828" / "trajectory.sqlite3"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--run-id", default="sidecar-v4-claim-smoke-20260828")
    parser.add_argument("--chunk-budget", type=int, default=2048)
    parser.add_argument("--manager-max-tokens", type=int, default=2048)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--offline", action="store_true", help="只验证真实样本的 chunk/evidence/normalizer，不调用模型")
    parser.add_argument("--base-url", default=os.environ.get("QWEN38_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.environ.get("QWEN38_MODEL", DEFAULT_MODEL))
    parser.add_argument("--api-key-env", default="QWEN38_API_KEY")
    parser.add_argument("--question-id", nargs="*", default=list(DEFAULT_IDS))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows = load_source(args.source, set(args.question_id))
    tokenizer = LocalQwenTokenizer()
    client = None
    if not args.offline:
        api_key = os.environ.get(args.api_key_env, "").strip()
        if not api_key:
            raise SystemExit(f"{args.api_key_env} is not set")
        client = QwenClient(ModelConfig(base_url=args.base_url.rstrip("/"), model=args.model, temperature=0.0, enable_thinking=False, summary_max_tokens=args.manager_max_tokens, timeout_seconds=args.timeout_seconds), api_key)
        client.max_model_len
    config = {
        "method": "memory_sidecar_v4_claim_smoke", "model": {"model": args.model},
        "chunk_budget_tokens": args.chunk_budget, "offline": args.offline,
    }
    config["config_fingerprint"] = sha256_text(json.dumps(config, sort_keys=True))
    store = TrajectoryStore(args.db)
    store.start_run(args.run_id, config)
    output: dict[str, object] = {"chunk_budget": args.chunk_budget, "model": args.model, "db": str(args.db), "samples": []}
    for question_id in args.question_id:
        row = rows[question_id]
        stream = build_message_stream(chronological_sessions(row)).messages
        state = V4GraphState()
        sample_id = store.start_sample(args.run_id, question_id, dataset_index=row.get("dataset_index"), question_type=row["question_type"], config_fingerprint=config["config_fingerprint"], code_version="v4-smoke")
        sample: dict[str, object] = {"question_id": question_id, "question_type": row["question_type"], "chunks": [], "edges": [], "raw_claims": [], "quarantine_claims": []}
        for chunk_ordinal, current in enumerate(chunks(tuple(stream), args.chunk_budget, tokenizer), 1):
            compiled = compile_evidence(current)
            chunk_result: dict[str, object] = {"chunk_ordinal": chunk_ordinal, "unit_ordinals": [message.unit_ordinal for message in current], "evidence_count": len(compiled.evidence), "evidence_chars": len(compiled.text)}
            if args.offline:
                store.record_v4_batch(sample_id, batch_ordinal=chunk_ordinal, input_hash=sha256_text(compiled.text), source_unit_ordinals=[message.unit_ordinal for message in current], input_text=compiled.text, memory_before_json="{}", raw_response=None, parse_status="offline", claims=[], edges=[], raw_claims=[], quarantine_claims=[])
                sample["chunks"].append(chunk_result)
                continue
            assert client is not None
            response = client.chat(manager_v4_messages(state, compiled), max_tokens=args.manager_max_tokens)
            chunk_result.update({"finish_reason": response.finish_reason, "input_tokens": response.input_tokens, "output_tokens": response.output_tokens})
            try:
                claims = parse_v4_manager_response(response.content, compiled)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                chunk_result["parse_error"] = f"{type(exc).__name__}: {exc}"
                store.record_v4_batch(sample_id, batch_ordinal=chunk_ordinal, input_hash=sha256_text(compiled.text), source_unit_ordinals=[message.unit_ordinal for message in current], input_text=compiled.text, memory_before_json="{}", raw_response=response.content, parse_status="parse_error", claims=[], edges=state.edges, raw_claims=state.raw_claims, quarantine_claims=state.quarantine_claims)
                sample["chunks"].append(chunk_result)
                continue
            normalized = [normalize_v4_claim(claim, compiled, ordinal=index) for index, claim in enumerate(claims)]
            routes = [state.route(claim) for claim in normalized]
            claim_rows = []
            for claim, route in zip(normalized, routes, strict=True):
                claim_rows.append({"claim_id": claim.claim_id, "model_claim": claim.model_claim, "normalized_claim": {"parse_status": claim.parse_status, "predicate": claim.predicate, "object": claim.object, "attributes": claim.attributes, "time_json": claim.time_json, "scope_json": claim.scope_json}, "parse_status": claim.parse_status, "route_status": route["route_status"], "route_result": route, "source_refs": list(claim.source_refs), "normalization_actions": list(claim.normalization_actions)})
            store.record_v4_batch(sample_id, batch_ordinal=chunk_ordinal, input_hash=sha256_text(compiled.text), source_unit_ordinals=[message.unit_ordinal for message in current], input_text=compiled.text, memory_before_json="{}", raw_response=response.content, parse_status="ok", claims=claim_rows, edges=state.edges, raw_claims=state.raw_claims, quarantine_claims=state.quarantine_claims)
            chunk_result["claim_statuses"] = [claim.parse_status for claim in normalized]
            chunk_result["route_statuses"] = [route["route_status"] for route in routes]
            sample["chunks"].append(chunk_result)
        sample["edges"] = state.edges
        sample["raw_claims"] = state.raw_claims
        sample["quarantine_claims"] = state.quarantine_claims
        sample["summary"] = {"edge_count": len(state.edges), "raw_claim_count": len(state.raw_claims), "quarantine_count": len(state.quarantine_claims), "predicate_counts": dict(Counter(edge["predicate"] for edge in state.edges))}
        output["samples"].append(sample)
        store.finish_sample(sample_id, STATUS_COMPLETED, full_history_tokens=sum(int(chunk["evidence_chars"]) for chunk in sample["chunks"]), call_count=sum(1 for chunk in sample["chunks"] if "finish_reason" in chunk))
        print(question_id, sample["summary"], flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    store.close()
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
