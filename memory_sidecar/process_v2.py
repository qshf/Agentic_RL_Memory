"""Small V2 runner used by pilots and offline integration tests.

It intentionally shares the existing history, tokenizer, tail and Answer Model
components; only manager/reconciliation and persistence use the V2 protocol.
"""
from __future__ import annotations

import json
import time
from argparse import Namespace
from pathlib import Path
from typing import Any

from utils.client import ApiError, QwenClient
from utils.config import ModelConfig
from utils.history import build_message_stream, chronological_sessions
from utils.local_tokenizer import LocalQwenTokenizer
from utils.store import STATUS_COMPLETED, STATUS_FAILED, TrajectoryStore
from utils.trajectory import record_call
from .budget import chunks, history_token_count
from .data import load_baseline_tail, record_sidecar_context
from .v2 import (
    V2MemoryState,
    compile_evidence,
    fit_answer_v2_context,
    manager_money_repair_messages,
    manager_v2_messages,
    merge_money_repair_events,
    parse_manager_response,
    parse_reconciliation_response,
    reconciler_v2_messages,
    state_sha256_v2,
    uncovered_usd_amounts,
)


def process_one_v2(row: dict[str, Any], *, args: Namespace, config: dict[str, Any], db_path: Path, api_key: str) -> dict[str, Any]:
    """Process one sample with V2 manager batches and one final reconciler call."""
    model = ModelConfig(base_url=args.base_url.rstrip("/"), model=args.model, temperature=0.0,
                        enable_thinking=False, summary_max_tokens=args.manager_max_tokens,
                        answer_max_tokens=args.answer_max_tokens, timeout_seconds=args.timeout_seconds)
    client = QwenClient(model, api_key)
    tokenizer = LocalQwenTokenizer(tokenizer_path=str(args.tokenizer_path) if getattr(args, "tokenizer_path", None) else None)
    store = TrajectoryStore(db_path)
    sample_id = store.start_sample(args.run_id, row["question_id"], dataset_index=row.get("dataset_index"),
                                   question_type=row.get("question_type"), config_fingerprint=config["config_fingerprint"], code_version=config["code_version"])
    started = time.monotonic(); calls = 0; input_tokens = 0; output_tokens = 0
    state = V2MemoryState()
    try:
        # 基线 tail 只在最终回答时提供近期原文；Manager 仅从完整历史构建持久记忆。
        tail = load_baseline_tail(args.baseline_db, args.baseline_run_id, row["question_id"], args.recent_tail_budget_tokens, tokenizer)
        record_sidecar_context(store, sample_id, tail)
        stream = build_message_stream(chronological_sessions(row))
        for batch_ordinal, current in enumerate(chunks(stream.messages, args.chunk_budget_tokens, tokenizer), 1):
            if args.debug_max_chunks and batch_ordinal > args.debug_max_chunks:
                break
            compiled = compile_evidence(current)
            before = state.to_json()
            # batch 必须串行：本 batch 接受的事件会成为下一个 batch 的 ADD/PATCH 候选目标。
            response = client.chat(manager_v2_messages(state.render_for_manager(), compiled), max_tokens=args.manager_max_tokens)
            calls += 1; input_tokens += response.input_tokens; output_tokens += response.output_tokens
            record_call(store, sample_id, calls, "sidecar_manager_v2", response)
            try:
                events = parse_manager_response(response.content); parse_status = "ok"
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                events = [{"_error": f"{type(exc).__name__}: {exc}"}]; parse_status = "error"
            raw_response = response.content
            missing_amounts = uncovered_usd_amounts(events, compiled)
            if missing_amounts:
                # 明确的美元金额可由程序检测。repair 只返回缺失金额对应事件；程序
                # 按同一 target/ADD 槽位替换首轮事件，再统一路由合并结果。
                repair = client.chat(
                    manager_money_repair_messages(
                        state.render_for_manager(), compiled, response.content, missing_amounts,
                    ),
                    max_tokens=args.manager_max_tokens,
                )
                calls += 1; input_tokens += repair.input_tokens; output_tokens += repair.output_tokens
                record_call(store, sample_id, calls, "sidecar_manager_v2_money_repair", repair)
                raw_response = json.dumps(
                    {"initial_response": response.content, "money_repair_response": repair.content},
                    ensure_ascii=False,
                )
                try:
                    repaired_events = parse_manager_response(repair.content)
                except (TypeError, ValueError, json.JSONDecodeError):
                    repaired_events = []
                try:
                    merged_events = merge_money_repair_events(events, repaired_events)
                except ValueError:
                    merged_events = []
                if merged_events and not uncovered_usd_amounts(merged_events, compiled):
                    events = merged_events
                    parse_status = "money_repaired"
                else:
                    parse_status = "money_repair_incomplete"
            # 落库本 batch 的审计轨迹：证据文本、内存前后快照、原始响应与解析状态。
            batch_id = store.record_sidecar_batch(sample_id, batch_ordinal=batch_ordinal,
                source_unit_ordinals=[m.unit_ordinal for m in current], input_text=compiled.text,
                memory_before_json=before, raw_response=raw_response, parse_status=parse_status)
            # 确定性路由：把本 batch 的 events 应用进 V2 状态（新增/覆盖/合并 record）。
            routes = state.route_batch(events, compiled, batch_ordinal)
            for index, route in enumerate(routes):
                created = route.get("created_record_ref")
                # 逐条记录每个 event 的路由结果；坏事件标记为 error，仍落库便于审计。
                item_id = store.record_sidecar_event_item(batch_id, item_ordinal=index,
                    model_event=events[index], parse_status="error" if "_error" in events[index] else "ok",
                    route_status=route["route_status"], route_result=route, created_record_ref=created)
                if created:
                    # 回填 record 的 created_by_event_item_id，建立「record ← 由哪条 event item 创建」的追溯链。
                    for record in state.records:
                        if record.get("record_ref") == created:
                            record["created_by_event_item_id"] = item_id
            # 把本 batch 路由后的全部 record 同步进「当前事实表」（upsert 只更新
            # lifecycle/superseded_by，旧版本不删除）。
            store.sync_sidecar_memory_v2(sample_id, state.records)
            # 追加一行「本 batch 结束时的完整状态快照 + SHA-256 指纹」，作为只增的审计日志。
            store.record_sidecar_state_v2(sample_id, batch_ordinal=batch_ordinal, state_json=state.to_json(), state_sha256=state_sha256_v2(state))

        # 全局去重在全部时间顺序 batch 路由完成后才执行，才能比较所有 current occurrence。
        before = state.to_json()
        rec_response = client.chat(
            reconciler_v2_messages(state.render_for_reconciler()),
            max_tokens=args.manager_max_tokens,
        )
        calls += 1; input_tokens += rec_response.input_tokens; output_tokens += rec_response.output_tokens
        record_call(store, sample_id, calls, "sidecar_reconciler_v2", rec_response)
        try:
            groups = parse_reconciliation_response(rec_response.content); rec_status = "ok"
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            groups = [{"_error": f"{type(exc).__name__}: {exc}"}]; rec_status = "error"
        routes = state.reconcile(groups)
        rec_id = store.record_sidecar_reconciliation_batch(sample_id, memory_before_json=before, raw_response=rec_response.content,
            parse_status=rec_status, state_after_json=state.to_json(), state_after_sha256=state_sha256_v2(state))
        for index, route in enumerate(routes):
            reconciliation_item_id = store.record_sidecar_reconciliation_item(rec_id, group_ordinal=index, model_group=groups[index], route_status=route["route_status"], route_result=route, created_record_ref=route.get("created_record_ref"))
            if route.get("created_record_ref"):
                for record in state.records:
                    if record.get("record_ref") == route["created_record_ref"]:
                        record["created_by_reconciliation_item_id"] = reconciliation_item_id
        store.sync_sidecar_memory_v2(sample_id, state.records)
        # Answer 始终看到全部 current record；fit_answer 只会在预留输出空间后裁剪 raw tail。
        answer_prompt, fitted_tail, fitted_tail_tokens = fit_answer_v2_context(
            state.render_for_answer(), tail["raw_tail"], row["question_date"], row["question"],
            tokenizer, client.max_model_len, args.answer_max_tokens,
        )
        if answer_prompt is None:
            reason = "current V2 memory plus answer prompt/output reserve exceeds context window"
            store.finish_sample(sample_id, "not_runnable", error=reason, call_count=calls)
            return {"question_id": row["question_id"], "status": "not_runnable", "error": reason}
        answer = client.chat(answer_prompt, max_tokens=args.answer_max_tokens)
        calls += 1; input_tokens += answer.input_tokens; output_tokens += answer.output_tokens
        record_call(store, sample_id, calls, "answer", answer)
        store.finish_sample(sample_id, STATUS_COMPLETED, full_history_tokens=history_token_count(stream, tokenizer),
            summary_tokens=tokenizer.count(state.render_for_answer()), raw_tail_tokens=fitted_tail_tokens,
            answer_input_tokens=answer.input_tokens, answer_output_tokens=answer.output_tokens,
            total_input_tokens=input_tokens, total_output_tokens=output_tokens, call_count=calls,
            latency_ms=int((time.monotonic() - started) * 1000), hypothesis=answer.content.strip())
        return {"question_id": row["question_id"], "status": STATUS_COMPLETED, "hypothesis": answer.content.strip()}
    except ApiError as exc:
        store.finish_sample(sample_id, STATUS_FAILED, error=str(exc), call_count=calls)
        return {"question_id": row["question_id"], "status": STATUS_FAILED, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        store.finish_sample(sample_id, STATUS_FAILED, error=f"{type(exc).__name__}: {exc}", call_count=calls)
        return {"question_id": row["question_id"], "status": STATUS_FAILED, "error": repr(exc)}
    finally:
        store.close()


process_one = process_one_v2
