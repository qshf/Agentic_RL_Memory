"""Run the Rolling Summary (or Full Context) baseline over a frozen manifest.

Compression happens before the question is ever loaded into a request, so the
history-processing stage is query-independent by construction.
"""
from __future__ import annotations

import csv
import gc
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from . import prompts
from .client import ApiError, CallResult, QwenClient, TokenCounter
from .config import (
    METHOD_FULL_CONTEXT,
    METHOD_ROLLING_SUMMARY,
    ROOT,
    BudgetConfig,
    ModelConfig,
    code_version,
    file_sha256,
    fingerprint,
    read_api_key,
)
from .history import Tokenizer, build_message_stream, chronological_sessions
from .rolling import RollingState, RollingSummaryEngine
from .store import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_NOT_RUNNABLE,
    TrajectoryStore,
)

DEFAULT_SOURCE = ROOT / "data" / "official_longmemeval" / "longmemeval_s_cleaned.json"
DEFAULT_RESULTS_ROOT = ROOT / "results" / "rolling_summary"


def read_manifest(path: Path) -> list[dict[str, str]]:
    # manifest 是冻结的 CSV 样本清单（如 data/samples/longmemeval_s_smoke_12_*.csv），
    # 由 scripts/sample_longmemeval_s.py 从源 JSON 按 question_type 分层抽样生成。
    # 每行只含元信息（question_id / dataset_index / question_type / 证据诊断列），不含对话原文。
    # csv.DictReader 按表头把每行读成 dict[str, str]，返回的就是本批待跑样本的“指针清单”。
    with path.open(encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    if not rows:
        raise ValueError(f"manifest {path} is empty")
    return rows


def load_source(path: Path, question_ids: set[str]) -> dict[str, dict[str, Any]]:
    """Keep only the manifest's rows; the full S file is several GB in memory."""
    # 回源取数：manifest 只是指针，真实对话数据在 S 源 JSON（约 277MB）里。
    # 只按 question_id 过滤出本批要跑的行，避免把整个大文件常驻内存。
    with path.open(encoding="utf-8") as file:
        rows = json.load(file)
    selected = {row["question_id"]: row for row in rows if row["question_id"] in question_ids}
    del rows  # 立即释放源列表引用，配合下面的 gc.collect() 让内存尽快回落
    gc.collect()
    missing = question_ids - selected.keys()
    if missing:
        # manifest 引用了源文件中不存在的 question_id，说明清单与数据版本不一致
        raise ValueError(f"manifest references {len(missing)} unknown question ids: {sorted(missing)[:5]}")
    return selected


class SampleRunner:
    """One sample's worth of state: chunking, compression, answering, logging."""

    def __init__(
        self,
        *,
        client: QwenClient,
        tokenizer: Tokenizer,
        budgets: BudgetConfig,
        store: TrajectoryStore,
        sample_id: int,
    ) -> None:
        self.client = client
        self.tokenizer = tokenizer
        self.budgets = budgets
        self.store = store
        self.sample_id = sample_id
        self.call_ordinal = 0
        self.step_ordinal = 0
        self.parent_step_id: int | None = None
        self.input_tokens = 0
        self.output_tokens = 0

    def _next_call_ordinal(self) -> int:
        self.call_ordinal += 1
        return self.call_ordinal

    def _call(
        self,
        kind: str,
        messages: Sequence[dict[str, str]],
        *,
        max_tokens: int | None,
        prompt_tokens_estimated: int,
        source_unit_ordinals: Sequence[int] | None = None,
    ) -> CallResult:
        ordinal = self._next_call_ordinal()
        try:
            result = self.client.chat(messages, max_tokens=max_tokens)
        except ApiError as error:
            self.store.record_call(
                self.sample_id,
                call_ordinal=ordinal,
                attempt=error.attempts,
                kind=kind,
                status="error",
                request_params={"model": self.client.config.model, "max_tokens": max_tokens},
                prompt_tokens_estimated=prompt_tokens_estimated,
                error=str(error),
                source_unit_ordinals=source_unit_ordinals,
            )
            raise
        self.store.record_call(
            self.sample_id,
            call_ordinal=ordinal,
            attempt=result.attempts,
            kind=kind,
            status="ok",
            request_params=result.request_params,
            prompt_tokens_estimated=prompt_tokens_estimated,
            result=result,
            source_unit_ordinals=source_unit_ordinals,
        )
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        return result

    def summarize(self, memory: str, history: str, reason: str) -> str:
        messages = prompts.summary_messages(memory, history, self.budgets.summary_budget_tokens)
        estimated = self.tokenizer.count(messages[0]["content"] + messages[1]["content"])
        headroom = self.client.max_model_len - self.client.config.summary_max_tokens
        if estimated > headroom:
            raise ApiError(
                f"{reason}: summary prompt is {estimated} tokens, above the {headroom}-token "
                f"headroom left by the {self.client.config.summary_max_tokens}-token summary budget"
            )
        result = self._call(
            "summary",
            messages,
            max_tokens=self.client.config.summary_max_tokens,
            prompt_tokens_estimated=estimated,
            source_unit_ordinals=None,
        )
        return result.content.strip()

    def record_state(self, event: str, state: RollingState, detail: dict[str, Any]) -> None:
        self.step_ordinal += 1
        compression = detail.pop("compression", None)
        if compression is not None:
            detail = {**detail, **asdict(compression)}
            detail["evicted_ordinals"] = list(compression.evicted_ordinals)
        self.parent_step_id = self.store.record_state(
            self.sample_id,
            step_ordinal=self.step_ordinal,
            parent_step_id=self.parent_step_id,
            event=event,
            state=state,
            summary_text=state.summary if compression is not None or event == "final" else None,
            detail=detail or None,
        )

    def answer(self, messages: Sequence[dict[str, str]]) -> tuple[CallResult | None, int, str | None]:
        """Return the answer, its measured prompt size, and why it was skipped."""
        prompt_tokens = self.client.tokenize_messages(messages)
        if prompt_tokens >= self.client.max_model_len:
            reason = (
                f"answer prompt is {prompt_tokens} tokens, at or above the server "
                f"max_model_len of {self.client.max_model_len}"
            )
            self.store.record_call(
                self.sample_id,
                call_ordinal=self._next_call_ordinal(),
                attempt=0,
                kind="answer",
                status="not_runnable",
                request_params={"model": self.client.config.model, "max_tokens": None},
                prompt_tokens_estimated=prompt_tokens,
                error=reason,
            )
            return None, prompt_tokens, reason
        result = self._call(
            "answer",
            messages,
            max_tokens=self.client.config.answer_max_tokens,
            prompt_tokens_estimated=prompt_tokens,
        )
        return result, prompt_tokens, None


def process_sample(
    row: dict[str, Any],
    *,
    client: QwenClient,
    budgets: BudgetConfig,
    store: TrajectoryStore,
    sample_id: int,
    method: str,
    tokenizer: Tokenizer | None = None,
) -> dict[str, Any]:
    # 默认走服务端 /tokenize；传入本地 tokenizer（如 LocalQwenTokenizer）则零网络计数。
    tokenizer = tokenizer or TokenCounter(client)
    runner = SampleRunner(
        client=client,
        tokenizer=tokenizer,
        budgets=budgets,
        store=store,
        sample_id=sample_id,
    )
    started = time.monotonic()
    sessions = chronological_sessions(row)
    stream = build_message_stream(sessions)
    history_text = stream.text
    full_history_tokens = tokenizer.count(history_text) if history_text else 0
    shared = {
        "full_history_tokens": full_history_tokens,
    }

    if method == METHOD_FULL_CONTEXT:
        messages = prompts.full_context_messages(
            history_text, row["question_date"], row["question"]
        )
        result, prompt_tokens, skip_reason = runner.answer(messages)
        return _outcome(
            runner,
            started,
            tokenizer,
            compression_count=0,
            summary_tokens=0,
            raw_tail_tokens=full_history_tokens,
            result=result,
            prompt_tokens=prompt_tokens,
            skip_reason=skip_reason,
            **shared,
        )

    engine = RollingSummaryEngine(
        summarize=runner.summarize,
        tokenizer=tokenizer,
        budgets=budgets,
        on_state=runner.record_state,
    )
    # 流式驱动：把历史消息逐条投喂进引擎。ingest 内部做 O(1) 增量维护，
    # 且只在 assistant 消息到达并超 rolling_trigger_tokens 时触发压缩，
    # 保证切割点总落在完整 assistant 回合之后。全程不读取问题（query-independent）。
    for message in stream.messages:
        engine.ingest(message)
    # 收尾：若仍超最终上下文预算则强制压缩；末尾若为未配对 user 消息则标记 terminal_issue。
    state = engine.finalize()

    messages = prompts.answer_messages(
        state.summary, state.render_tail(), row["question_date"], row["question"]
    )
    result, prompt_tokens, skip_reason = runner.answer(messages)
    return _outcome(
        runner,
        started,
        tokenizer,
        compression_count=len(engine.compressions),
        summary_tokens=state.summary_tokens,
        raw_tail_tokens=state.tail_tokens,
        result=result,
        prompt_tokens=prompt_tokens,
        skip_reason=skip_reason,
        **shared,
    )


def _outcome(
    runner: SampleRunner,
    started: float,
    tokenizer: Tokenizer,
    *,
    result: CallResult | None,
    prompt_tokens: int,
    skip_reason: str | None,
    **fields: int,
) -> dict[str, Any]:
    return {
        "status": STATUS_NOT_RUNNABLE if result is None else STATUS_COMPLETED,
        "hypothesis": result.content.strip() if result else None,
        "error": skip_reason,
        "answer_input_tokens": result.input_tokens if result else prompt_tokens,
        "answer_output_tokens": result.output_tokens if result else 0,
        "total_input_tokens": runner.input_tokens,
        "total_output_tokens": runner.output_tokens,
        "call_count": runner.call_ordinal,
        "latency_ms": int((time.monotonic() - started) * 1000),
        **fields,
    }


def build_config(
    *,
    method: str,
    model: ModelConfig,
    budgets: BudgetConfig,
    manifest_path: Path,
    source_path: Path,
    preflight: dict[str, Any],
) -> dict[str, Any]:
    return {
        "method": method,
        "prompt_version": prompts.PROMPT_VERSION,
        "config_fingerprint": fingerprint(model, budgets, method, prompts.PROMPT_VERSION),
        "code_version": code_version(),
        "model": asdict(model),
        "budgets": asdict(budgets),
        "budget_unit_tokens": 1024,
        "manifest": {
            # 记录本次用哪份清单及其 sha256，保证结果可溯源（换清单 = 新实验）
            "path": str(manifest_path),
            "sha256": file_sha256(manifest_path),
        },
        "source": {
            "path": str(source_path),
            # 源文件太大，运行期不哈希，靠 path + 文档说明定位版本
            "sha256_note": "not hashed at run time; the S file is 277 MB",
        },
        "service": preflight,
    }


def run(
    *,
    manifest_path: Path,
    run_id: str,
    method: str = METHOD_ROLLING_SUMMARY,
    source_path: Path = DEFAULT_SOURCE,
    results_root: Path = DEFAULT_RESULTS_ROOT,
    budgets: BudgetConfig | None = None,
    model: ModelConfig | None = None,
    client: QwenClient | None = None,
    limit: int | None = None,
    question_ids: Sequence[str] | None = None,
    tokenizer: Tokenizer | None = None,
) -> dict[str, Any]:
    budgets = budgets or BudgetConfig()
    model = model or ModelConfig.from_env()
    client = client or QwenClient(model, read_api_key())
    model = client.config

    # --- 1) 读清单：CSV 每行 = 一条待跑样本（manifest 只是元信息，真实数据待回源） ---
    manifest = read_manifest(manifest_path)
    # --question-id 可选过滤：只跑清单里指定的几条（须存在于清单）
    if question_ids:
        wanted = set(question_ids)
        manifest = [row for row in manifest if row["question_id"] in wanted]
        missing = wanted - {row["question_id"] for row in manifest}
        if missing:
            raise ValueError(f"question ids not in {manifest_path}: {sorted(missing)}")
    # --limit 可选截断：只跑清单前 N 条（调试 / 冒烟用）
    if limit is not None:
        manifest = manifest[:limit]
    print(f"Preflight against {model.base_url} ({model.model})")
    preflight = client.preflight()
    print(f"  max_model_len={preflight['max_model_len']} probe={preflight['probe_output']!r}")

    config = build_config(
        method=method,
        model=model,
        budgets=budgets,
        manifest_path=manifest_path,
        source_path=source_path,
        preflight=preflight,
    )
    run_dir = results_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    config_path = run_dir / "config.json"
    if config_path.exists():
        existing = json.loads(config_path.read_text(encoding="utf-8"))
        if existing["config_fingerprint"] != config["config_fingerprint"]:
            raise ValueError(
                f"run {run_id} was created with fingerprint {existing['config_fingerprint']}; "
                f"the current config is {config['config_fingerprint']}. Use a new --run-id."
            )
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    store = TrajectoryStore(run_dir / "trajectory.sqlite3")
    store.start_run(run_id, config)
    # 断点续跑：跳过同一指纹下已经完成的问题
    done = store.completed_question_ids(run_id, config["config_fingerprint"])
    pending = [row for row in manifest if row["question_id"] not in done]
    print(f"{len(manifest)} samples in manifest, {len(done)} already done, {len(pending)} to run")

    # --- 2) 回源取数：按“待跑”的 question_id 从大 JSON 里捞真实对话（只捞本批） ---
    source = load_source(source_path, {row["question_id"] for row in pending}) if pending else {}
    # --- 3) 逐条跑：manifest 的“指针”在这里被解引用成 source[question_id] 的真实对话数据 ---
    for position, manifest_row in enumerate(pending, start=1):
        question_id = manifest_row["question_id"]
        sample_id = store.start_sample(
            run_id,
            question_id,
            dataset_index=int(manifest_row["dataset_index"]) if manifest_row.get("dataset_index") else None,
            question_type=manifest_row.get("question_type"),
            config_fingerprint=config["config_fingerprint"],
            code_version=config["code_version"],
        )
        try:
            outcome = process_sample(
                source[question_id],
                client=client,
                budgets=budgets,
                store=store,
                sample_id=sample_id,
                method=method,
                tokenizer=tokenizer,
            )
        except (ApiError, ValueError, KeyError) as error:
            store.finish_sample(sample_id, STATUS_FAILED, error=f"{type(error).__name__}: {error}")
            print(f"[{position}/{len(pending)}] {question_id} FAILED: {error}")
            continue
        status = outcome.pop("status")
        store.finish_sample(sample_id, status, **outcome)
        print(
            f"[{position}/{len(pending)}] {question_id} {status} "
            f"compressions={outcome['compression_count']} "
            f"answer_in={outcome['answer_input_tokens']} out={outcome['answer_output_tokens']}"
        )

    exported = store.export_hypotheses(run_id, run_dir / "hypotheses.jsonl")
    statistics = store.run_statistics(run_id)
    statistics["run_id"] = run_id
    statistics["method"] = method
    statistics["config_fingerprint"] = config["config_fingerprint"]
    statistics["hypotheses_exported"] = exported
    (run_dir / "run_summary.json").write_text(
        json.dumps(statistics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    store.close()
    print(f"Wrote {exported} hypotheses to {run_dir / 'hypotheses.jsonl'}")
    return statistics
