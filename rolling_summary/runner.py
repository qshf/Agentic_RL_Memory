"""Run the Rolling Summary (or Full Context) baseline over a frozen manifest.

Compression happens before the question is ever loaded into a request, so the
history-processing stage is query-independent by construction.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from queue import SimpleQueue
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
from utils.dataset import load_source, read_manifest

DEFAULT_SOURCE = ROOT / "data" / "official_longmemeval" / "longmemeval_s_cleaned.json"
DEFAULT_RESULTS_ROOT = ROOT / "results" / "rolling_summary"

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
        detail = dict(detail)
        raw_text = detail.pop("raw_text", None)
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
            summary_text=state.summary or None,
            raw_text=raw_text,
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
    # 收尾不做兜底压缩；rolling trigger 只在 assistant ingest 时触发，末尾 user 仅标记 terminal_issue。
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
    max_concurrency: int,
) -> dict[str, Any]:
    return {
        "method": method,
        "prompt_version": prompts.PROMPT_VERSION,
        "config_fingerprint": fingerprint(model, budgets, method, prompts.PROMPT_VERSION),
        "code_version": code_version(),
        "model": asdict(model),
        "budgets": asdict(budgets),
        "budget_unit_tokens": 1024,
        "execution": {"max_concurrency": max_concurrency},
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
    max_concurrency: int = 1,
) -> dict[str, Any]:
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")
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
        max_concurrency=max_concurrency,
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
        if existing.get("execution", {}).get("max_concurrency", 1) != max_concurrency:
            raise ValueError(
                f"run {run_id} was created with max_concurrency="
                f"{existing.get('execution', {}).get('max_concurrency', 1)}; use a new --run-id."
            )
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    store = TrajectoryStore(run_dir / "trajectory.sqlite3")
    store.start_run(run_id, config)
    # 断点续跑：跳过同一指纹下已经完成的问题
    done = store.completed_question_ids(run_id, config["config_fingerprint"])
    pending = [row for row in manifest if row["question_id"] not in done]
    print(f"{len(manifest)} samples in manifest, {len(done)} already done, {len(pending)} to run")
    store.close()

    # --- 2) 回源取数：按“待跑”的 question_id 从大 JSON 里捞真实对话（只捞本批） ---
    source = load_source(source_path, {row["question_id"] for row in pending}) if pending else {}
    # Each worker owns its HTTP session and SQLite connection. The server can
    # then continuously batch independent requests without sharing clients or
    # SQLite connections across threads.
    if max_concurrency > 1 and not isinstance(client, QwenClient):
        raise ValueError("max_concurrency > 1 requires a QwenClient")
    worker_tokenizers: SimpleQueue[Tokenizer] | None = None
    if max_concurrency > 1 and tokenizer is not None:
        clone = getattr(tokenizer, "clone", None)
        if not callable(clone):
            raise ValueError("max_concurrency > 1 requires a cloneable tokenizer")
        worker_tokenizers = SimpleQueue()
        for _ in range(max_concurrency):
            worker_tokenizers.put(clone())

    db_path = run_dir / "trajectory.sqlite3"

    def run_one(position: int, manifest_row: dict[str, str]) -> tuple[int, str, str, dict[str, Any]]:
        question_id = manifest_row["question_id"]
        worker_store = TrajectoryStore(db_path)
        worker_client = client if max_concurrency == 1 else QwenClient(model, read_api_key())
        worker_tokenizer = worker_tokenizers.get() if worker_tokenizers is not None else tokenizer
        try:
            sample_id = worker_store.start_sample(
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
                    client=worker_client,
                    budgets=budgets,
                    store=worker_store,
                    sample_id=sample_id,
                    method=method,
                    tokenizer=worker_tokenizer,
                )
            except (ApiError, ValueError, KeyError) as error:
                worker_store.finish_sample(sample_id, STATUS_FAILED, error=f"{type(error).__name__}: {error}")
                return position, question_id, STATUS_FAILED, {"error": str(error)}
            status = outcome.pop("status")
            worker_store.finish_sample(sample_id, status, **outcome)
            return position, question_id, status, outcome
        finally:
            if worker_tokenizers is not None:
                worker_tokenizers.put(worker_tokenizer)
            worker_store.close()

    processing_started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        futures = [
            executor.submit(run_one, position, manifest_row)
            for position, manifest_row in enumerate(pending, start=1)
        ]
        for future in as_completed(futures):
            position, question_id, status, outcome = future.result()
            if status == STATUS_FAILED:
                print(f"[{position}/{len(pending)}] {question_id} FAILED: {outcome['error']}")
                continue
            print(
                f"[{position}/{len(pending)}] {question_id} {status} "
                f"compressions={outcome['compression_count']} "
                f"answer_in={outcome['answer_input_tokens']} out={outcome['answer_output_tokens']}"
            )
    wall_time_ms = int((time.monotonic() - processing_started) * 1000)

    store = TrajectoryStore(db_path)
    exported = store.export_hypotheses(run_id, run_dir / "hypotheses.jsonl")
    statistics = store.run_statistics(run_id)
    statistics["run_id"] = run_id
    statistics["method"] = method
    statistics["config_fingerprint"] = config["config_fingerprint"]
    statistics["hypotheses_exported"] = exported
    statistics["execution"] = {
        "max_concurrency": max_concurrency,
        "wall_time_ms": wall_time_ms,
        "samples_per_second": len(pending) / (wall_time_ms / 1000) if wall_time_ms else 0.0,
        "model_tokens_per_second": (
            (statistics["total_input_tokens"] + statistics["total_output_tokens"]) / (wall_time_ms / 1000)
            if wall_time_ms
            else 0.0
        ),
    }
    (run_dir / "run_summary.json").write_text(
        json.dumps(statistics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    store.close()
    print(f"Wrote {exported} hypotheses to {run_dir / 'hypotheses.jsonl'}")
    return statistics
