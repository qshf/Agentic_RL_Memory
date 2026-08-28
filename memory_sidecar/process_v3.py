"""V3 sample runner: serial multi-event batches with atomic persistence."""
from __future__ import annotations

from collections import Counter
import json
import time
from argparse import Namespace
from pathlib import Path
from typing import Any, Sequence

from utils.client import ApiError, QwenClient
from utils.config import ModelConfig, sha256_text
from utils.history import build_message_stream, chronological_sessions
from utils.local_tokenizer import LocalQwenTokenizer
from utils.store import STATUS_COMPLETED, STATUS_FAILED, STATUS_NOT_RUNNABLE, TrajectoryStore
from utils.tokenizer import count_messages
from utils.trajectory import record_call
from .budget import chunks, history_token_count
from .data import load_baseline_tail, record_sidecar_context
from .v3 import (
    COMPACTOR_V3_PROMPT_VERSION,
    V3MemoryState,
    compactor_v3_messages,
    compile_evidence,
    fit_answer_v3_context,
    is_truncated_json,
    manager_v3_messages,
    parse_compactor_response,
    parse_manager_response,
    render_compactor_v3_input,
    split_chunk_at_turn_boundary,
    state_sha256_v3,
    uncovered_usd_amounts,
)


def process_one_v3(row: dict[str, Any], *, args: Namespace, config: dict[str, Any], db_path: Path, api_key: str) -> dict[str, Any]:
    """Process a sample without invoking any V2 repair or reconciliation path."""
    model = ModelConfig(
        base_url=args.base_url.rstrip("/"), model=args.model, temperature=0.0,
        enable_thinking=False, summary_max_tokens=args.manager_max_tokens,
        answer_max_tokens=args.answer_max_tokens, timeout_seconds=args.timeout_seconds,
    )
    client = QwenClient(model, api_key)
    tokenizer = LocalQwenTokenizer(tokenizer_path=str(args.tokenizer_path) if getattr(args, "tokenizer_path", None) else None)
    store = TrajectoryStore(db_path)
    sample_id = store.start_sample(
        args.run_id, row["question_id"], dataset_index=row.get("dataset_index"),
        question_type=row.get("question_type"), config_fingerprint=config["config_fingerprint"],
        code_version=config["code_version"],
    )
    state = V3MemoryState()
    metrics: dict[str, Any] = {
        "terminal_leaf_manager_batches": 0,
        "manager_completion_requests": 0,
        "answer_completions": 0,
        "completion_attempts": 0,
        "event_items": 0,
        "route_status_counts": Counter(),
        "key_drift_candidates": 0,
        "multi_event_batch_rate": 0.0,
        "money_coverage_missing": 0,
        "updated_keys": 0,
        "rejected_duplicate_key_in_batch": 0,
        "truncated_parent_requests": 0,
        "manager_output_unfit": 0,
        "effective_leaf_chunk_count": 0,
        "current_memory_over_budget": 0,
        "recent_tail_trimmed_tokens": 0,
        "compactor_completions": 0,
        "compactor_reused": 0,
        "compactor_input_tokens": 0,
        "compactor_output_tokens": 0,
    }
    compactor_mode = getattr(args, "compactor", "off")
    if compactor_mode not in {"off", "on", "reuse"}:
        raise ValueError("compactor must be off, on or reuse")
    compaction_run_id = getattr(args, "compaction_run_id", None) or args.run_id
    replay_source_db = getattr(args, "replay_source_db", None)
    replay_source_run_id = getattr(args, "replay_source_run_id", None)
    if (replay_source_db is None) != (replay_source_run_id is None):
        raise ValueError("Answer-only replay requires both replay_source_db and replay_source_run_id")
    started = time.monotonic()
    calls = input_tokens = output_tokens = 0
    next_batch_ordinal = 0
    multi_event_batches = 0

    def next_batch() -> int:
        nonlocal next_batch_ordinal
        next_batch_ordinal += 1
        return next_batch_ordinal

    def call_manager(messages: list[dict[str, str]]) -> Any:
        nonlocal calls, input_tokens, output_tokens
        result = client.chat(messages, max_tokens=args.manager_max_tokens)
        calls += 1
        input_tokens += result.input_tokens
        output_tokens += result.output_tokens
        metrics["manager_completion_requests"] += 1
        metrics["completion_attempts"] += result.attempts
        record_call(store, sample_id, calls, "sidecar_manager_v3", result)
        return result

    def call_compactor(messages: list[dict[str, str]]) -> Any:
        nonlocal calls, input_tokens, output_tokens
        result = client.chat(messages, max_tokens=args.compactor_max_tokens)
        calls += 1
        input_tokens += result.input_tokens
        output_tokens += result.output_tokens
        metrics["compactor_completions"] += 1
        metrics["compactor_input_tokens"] += result.input_tokens
        metrics["compactor_output_tokens"] += result.output_tokens
        metrics["completion_attempts"] += result.attempts
        record_call(store, sample_id, calls, "sidecar_compactor_v3", result)
        return result

    def audit_batch(
        *, batch_ordinal: int, current: Sequence[Any], compiled: Any, before: str,
        raw_response: str | None, parse_status: str, items: list[dict[str, Any]],
        candidate: V3MemoryState, parent_chunk_ordinal: int | None, split_depth: int,
        split_reason: str | None,
    ) -> None:
        store.record_v3_batch(
            sample_id, batch_ordinal=batch_ordinal,
            source_unit_ordinals=[message.unit_ordinal for message in current], input_text=compiled.text,
            memory_before_json=before, raw_response=raw_response, parse_status=parse_status,
            items=items, records=candidate.records, state_json=candidate.to_json(),
            state_sha256=state_sha256_v3(candidate), parent_chunk_ordinal=parent_chunk_ordinal,
            split_depth=split_depth, split_reason=split_reason,
        )

    def process_chunk(
        current: tuple[Any, ...], *, root_chunk_ordinal: int, split_depth: int = 0,
        is_child: bool = False,
    ) -> None:
        """Process one source chunk, recursively splitting only true truncation."""
        nonlocal state, multi_event_batches
        batch_ordinal = next_batch()
        compiled = compile_evidence(current)
        before = state.to_json()
        manager_prompt = manager_v3_messages(state.render_for_manager(), compiled)
        manager_budget = min(args.shared_context_budget_tokens, client.max_model_len)
        if count_messages(manager_prompt, tokenizer) + args.manager_max_tokens > manager_budget:
            raise ValueError("complete current V3 memory plus manager chunk/output reserve exceeds shared or server context budget")
        response = call_manager(manager_prompt)
        truncated = response.finish_reason == "length" or is_truncated_json(response.content)
        if truncated:
            metrics["truncated_parent_requests"] += 1
            split = split_chunk_at_turn_boundary(current)
            if split is None:
                metrics["manager_output_unfit"] += 1
                audit_batch(
                    batch_ordinal=batch_ordinal, current=current, compiled=compiled, before=before,
                    raw_response=response.content, parse_status="manager_output_unfit", items=[], candidate=state,
                    parent_chunk_ordinal=root_chunk_ordinal if is_child else None,
                    split_depth=split_depth, split_reason="output_truncated",
                )
                return
            audit_batch(
                batch_ordinal=batch_ordinal, current=current, compiled=compiled, before=before,
                raw_response=response.content, parse_status="truncated_parent", items=[], candidate=state,
                parent_chunk_ordinal=root_chunk_ordinal if is_child else None,
                split_depth=split_depth, split_reason="output_truncated",
            )
            first, second = split
            process_chunk(first, root_chunk_ordinal=root_chunk_ordinal, split_depth=split_depth + 1, is_child=True)
            process_chunk(second, root_chunk_ordinal=root_chunk_ordinal, split_depth=split_depth + 1, is_child=True)
            return

        metrics["terminal_leaf_manager_batches"] += 1
        metrics["effective_leaf_chunk_count"] += 1
        try:
            parsed_items = parse_manager_response(response.content, compiled)
            parse_status = "ok"
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            parsed_items = []
            parse_status = "rejected_parse"
            top_level_item = {
                "model_event": {"_raw_response": response.content}, "parse_status": "error",
                "route_status": "rejected_parse", "route_result": {"reason": f"{type(exc).__name__}: {exc}"},
            }
        else:
            top_level_item = None
        candidate = state.copy()
        routes = candidate.route_batch(parsed_items, batch_ordinal=batch_ordinal)
        item_rows: list[dict[str, Any]] = []
        for item, route in zip(parsed_items, routes, strict=True):
            metrics["event_items"] += 1
            metrics["route_status_counts"][route["route_status"]] += 1
            if route.get("key_drift_candidate"):
                metrics["key_drift_candidates"] += 1
            if route["route_status"] == "rejected_duplicate_key_in_batch":
                metrics["rejected_duplicate_key_in_batch"] += 1
            if item.event and item.event.action == "UPDATE" and route["route_status"] == "applied":
                metrics["updated_keys"] += 1
            item_rows.append({
                "model_event": item.model_event, "parse_status": item.parse_status,
                "route_status": route["route_status"], "route_result": route,
            })
        if top_level_item is not None:
            metrics["event_items"] += 1
            metrics["route_status_counts"]["rejected_parse"] += 1
            item_rows.append(top_level_item)
        if len(parsed_items) >= 2:
            multi_event_batches += 1
        metrics["money_coverage_missing"] += len(uncovered_usd_amounts(parsed_items, routes, compiled))
        # record_v3_batch commits all write-side effects before the candidate is
        # promoted. An exception leaves both SQLite and process state unchanged.
        audit_batch(
            batch_ordinal=batch_ordinal, current=current, compiled=compiled, before=before,
            raw_response=response.content, parse_status=parse_status, items=item_rows, candidate=candidate,
            parent_chunk_ordinal=root_chunk_ordinal if is_child else None,
            split_depth=split_depth, split_reason="output_truncated" if is_child else None,
        )
        state = candidate

    try:
        # For V3, keep the full baseline tail in memory first. The shared 80K
        # budget is applied below after current memory has been rendered.
        tail = load_baseline_tail(args.baseline_db, args.baseline_run_id, row["question_id"], 0, tokenizer)
        stream = build_message_stream(chronological_sessions(row))
        if replay_source_db is not None:
            source_store = TrajectoryStore(replay_source_db)
            try:
                replayed_records = source_store.load_v3_memory_for_run(
                    run_id=replay_source_run_id, question_id=row["question_id"],
                )
            finally:
                source_store.close()
            if replayed_records is None:
                raise ValueError(f"no completed V3 memory for question_id={row['question_id']!r} in replay source run")
            state = V3MemoryState(records=replayed_records)
            metrics["manager_trajectory_reused"] = 1
        else:
            initial_chunks = chunks(stream.messages, args.chunk_budget_tokens, tokenizer)
            for root_chunk_ordinal, current in enumerate(initial_chunks, 1):
                if args.debug_max_chunks and root_chunk_ordinal > args.debug_max_chunks:
                    break
                process_chunk(current, root_chunk_ordinal=root_chunk_ordinal)

        memory_json = state.render_for_answer()
        compactor_input, compactor_record_ids = render_compactor_v3_input(state)
        # The final summary is a projection of canonical memory only. Recent
        # conversation remains a separate, identical Answer input in both arms.
        compactor_raw_tail = ""
        compactor_raw_tail_sha256 = sha256_text(compactor_raw_tail)
        memory_snapshot_sha256 = sha256_text(compactor_input)
        answer_memory = memory_json
        if compactor_mode == "on":
            compactor_prompt = compactor_v3_messages(compactor_input)
            compactor_budget = min(args.shared_context_budget_tokens, client.max_model_len)
            if count_messages(compactor_prompt, tokenizer) + args.compactor_max_tokens > compactor_budget:
                raise ValueError("complete current V3 memory plus compactor output reserve exceeds shared or server context budget")
            compactor = call_compactor(compactor_prompt)
            try:
                answer_memory = parse_compactor_response(compactor.content)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                store.record_v3_compaction(
                    sample_id, compaction_run_id=compaction_run_id,
                    memory_snapshot_sha256=memory_snapshot_sha256, input_memory_json=compactor_input,
                    input_raw_tail=compactor_raw_tail, input_raw_tail_sha256=compactor_raw_tail_sha256,
                    input_record_ids=compactor_record_ids, model=args.model,
                    prompt_version=COMPACTOR_V3_PROMPT_VERSION, raw_response=compactor.content,
                    parse_status="rejected_parse", summary_text=None, input_tokens=compactor.input_tokens,
                    output_tokens=compactor.output_tokens, latency_ms=compactor.latency_ms,
                )
                raise ValueError(f"invalid compactor response: {type(exc).__name__}: {exc}") from exc
            store.record_v3_compaction(
                sample_id, compaction_run_id=compaction_run_id,
                memory_snapshot_sha256=memory_snapshot_sha256, input_memory_json=compactor_input,
                input_raw_tail=compactor_raw_tail, input_raw_tail_sha256=compactor_raw_tail_sha256,
                input_record_ids=compactor_record_ids, model=args.model,
                prompt_version=COMPACTOR_V3_PROMPT_VERSION, raw_response=compactor.content,
                parse_status="ok", summary_text=answer_memory, input_tokens=compactor.input_tokens,
                output_tokens=compactor.output_tokens, latency_ms=compactor.latency_ms,
            )
        elif compactor_mode == "reuse":
            source_db = getattr(args, "compaction_source_db", None)
            source_run_id = getattr(args, "compaction_source_run_id", None)
            if source_db is None or not source_run_id:
                raise ValueError("compactor reuse requires --compaction-source-db and --compaction-source-run-id")
            source_store = TrajectoryStore(source_db)
            try:
                saved = source_store.load_v3_compaction(
                    run_id=source_run_id, question_id=row["question_id"], compaction_run_id=compaction_run_id,
                )
            finally:
                source_store.close()
            if saved is None:
                raise ValueError(f"no saved compaction for question_id={row['question_id']!r}, compaction_run_id={compaction_run_id!r}")
            if saved["parse_status"] != "ok" or not isinstance(saved["summary_text"], str):
                raise ValueError("saved compaction has no valid summary_text")
            if saved["memory_snapshot_sha256"] != memory_snapshot_sha256:
                raise ValueError("saved compaction memory_snapshot_sha256 does not match current V3 memory")
            answer_memory = saved["summary_text"]
            metrics["compactor_reused"] += 1
            store.record_v3_compaction(
                sample_id, compaction_run_id=compaction_run_id,
                memory_snapshot_sha256=memory_snapshot_sha256, input_memory_json=compactor_input,
                input_raw_tail=compactor_raw_tail, input_raw_tail_sha256=compactor_raw_tail_sha256,
                input_record_ids=compactor_record_ids, model=str(saved["model"]),
                prompt_version=str(saved["prompt_version"]), raw_response=saved["raw_response"],
                parse_status="reused", summary_text=answer_memory,
                input_tokens=int(saved["input_tokens"]), output_tokens=int(saved["output_tokens"]),
                latency_ms=int(saved["latency_ms"]), reused_from_db=str(source_db),
                reused_from_run_id=source_run_id,
            )
        shared_budget = min(args.shared_context_budget_tokens, client.max_model_len)
        raw_tail_for_answer = tail["raw_tail"]
        tail_blocks_for_answer = tail.get("raw_tail_blocks")
        answer_prompt, fitted_tail, fitted_tail_tokens, trimmed_tokens = fit_answer_v3_context(
            answer_memory, raw_tail_for_answer, row["question_date"], row["question"], tokenizer,
            shared_budget, args.answer_max_tokens, tail_blocks_for_answer,
        )
        metrics["recent_tail_trimmed_tokens"] = trimmed_tokens
        audited_tail = dict(tail)
        audited_tail["raw_tail"] = fitted_tail
        audited_tail["raw_tail_sha256"] = sha256_text(fitted_tail)
        audited_tail["raw_tail_tokens"] = fitted_tail_tokens
        audited_tail["raw_tail_trimmed"] = bool(trimmed_tokens)
        record_sidecar_context(store, sample_id, audited_tail)
        if answer_prompt is None:
            metrics["current_memory_over_budget"] = 1
            store.record_v3_metrics(sample_id, _serializable_metrics(metrics))
            reason = "current V3 memory plus answer prompt/output reserve exceeds shared or server context budget"
            store.finish_sample(sample_id, STATUS_NOT_RUNNABLE, error=reason, call_count=calls)
            return {"question_id": row["question_id"], "status": STATUS_NOT_RUNNABLE, "error": reason}
        answer = client.chat(answer_prompt, max_tokens=args.answer_max_tokens)
        calls += 1
        input_tokens += answer.input_tokens
        output_tokens += answer.output_tokens
        metrics["answer_completions"] += 1
        metrics["completion_attempts"] += answer.attempts
        record_call(store, sample_id, calls, "answer", answer)
        metrics["multi_event_batch_rate"] = multi_event_batches / metrics["terminal_leaf_manager_batches"] if metrics["terminal_leaf_manager_batches"] else 0.0
        store.record_v3_metrics(sample_id, _serializable_metrics(metrics))
        store.finish_sample(
            sample_id, STATUS_COMPLETED, full_history_tokens=history_token_count(stream, tokenizer),
            summary_tokens=tokenizer.count(answer_memory), raw_tail_tokens=fitted_tail_tokens,
            answer_input_tokens=answer.input_tokens, answer_output_tokens=answer.output_tokens,
            total_input_tokens=input_tokens, total_output_tokens=output_tokens, call_count=calls,
            latency_ms=int((time.monotonic() - started) * 1000), hypothesis=answer.content.strip(),
        )
        return {"question_id": row["question_id"], "status": STATUS_COMPLETED, "hypothesis": answer.content.strip()}
    except ApiError as exc:
        store.record_v3_metrics(sample_id, _serializable_metrics(metrics))
        store.finish_sample(sample_id, STATUS_FAILED, error=str(exc), call_count=calls)
        return {"question_id": row["question_id"], "status": STATUS_FAILED, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        store.record_v3_metrics(sample_id, _serializable_metrics(metrics))
        store.finish_sample(sample_id, STATUS_FAILED, error=f"{type(exc).__name__}: {exc}", call_count=calls)
        return {"question_id": row["question_id"], "status": STATUS_FAILED, "error": repr(exc)}
    finally:
        store.close()


def _serializable_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {key: dict(value) if isinstance(value, Counter) else value for key, value in metrics.items()}


process_one = process_one_v3
