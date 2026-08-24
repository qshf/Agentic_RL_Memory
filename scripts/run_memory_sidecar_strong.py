"""运行 Memory Sidecar Strong 试验。

CLI 只负责参数、并发和汇总，具体功能位于：

* ``memory_sidecar.budget``：本地 tokenizer 预算与分块；
* ``memory_sidecar.data``：基线 tail 重建与 provenance；
* ``memory_sidecar.process``：单样本 manager、路由与回答流程。

本实验不导入 ``rolling_summary``。``--baseline-db`` 是只读数据输入，用于恢复已记录的
V1 近期 raw tail。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_sidecar.process import process_one  # noqa: E402
from utils.client import QwenClient  # noqa: E402
from utils.config import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ROOT,
    ModelConfig,
    code_version,
    file_sha256,
    sha256_text,
)
from utils.dataset import load_source, read_manifest  # noqa: E402
from utils.local_tokenizer import LocalQwenTokenizer  # noqa: E402
from utils.store import TrajectoryStore  # noqa: E402


DEFAULT_BASELINE_DB = ROOT / "results" / "rolling_summary" / "rolling-summary-eval120-v1-atomic-c2" / "trajectory.sqlite3"
DEFAULT_RESULTS_ROOT = ROOT / "results" / "memory_sidecar"
DEFAULT_MANIFEST = ROOT / "data" / "samples" / "longmemeval_s_eval_120_seed_20260821.csv"
DEFAULT_SOURCE = ROOT / "data" / "official_longmemeval" / "longmemeval_s_cleaned.json"
TAIL_BUDGET_TOKENS = 16 * 1024
PROMPT_VERSION = "memory-sidecar-strong-v1-json-events"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--baseline-db", type=Path, default=DEFAULT_BASELINE_DB)
    parser.add_argument("--baseline-run-id", default="rolling-summary-eval120-v1-atomic-c2")
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    parser.add_argument("--run-id", default="sidecar-strong-pilot-v1")
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--question-id", nargs="*", default=None)
    parser.add_argument("--chunk-budget-tokens", type=int, default=2048)
    parser.add_argument("--manager-context-budget-tokens", type=int, default=12288)
    parser.add_argument("--active-memory-budget-tokens", type=int, default=8192)
    parser.add_argument("--update-ledger-budget-tokens", type=int, default=2048)
    parser.add_argument(
        "--debug-max-chunks", type=int, default=0,
        help="仅调试：处理 N 个 manager chunk 后，用部分状态回答；0 表示全部处理",
    )
    parser.add_argument("--recent-tail-budget-tokens", type=int, default=TAIL_BUDGET_TOKENS)
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--manager-max-tokens", type=int, default=768)
    parser.add_argument("--answer-max-tokens", type=int, default=1024)
    parser.add_argument("--base-url", default=os.environ.get("QWEN38_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.environ.get("QWEN38_MODEL", DEFAULT_MODEL))
    parser.add_argument("--api-key-env", default="QWEN38_API_KEY")
    parser.add_argument("--tokenizer-path", type=Path, default=None, help="本地 tokenizer.json；省略时使用 HF 缓存")
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    return parser.parse_args()


def _api_key_from_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set; the key must never be committed")
    return value


def _select_manifest(args: argparse.Namespace) -> list[dict[str, Any]]:
    manifest = read_manifest(args.manifest)
    if args.question_id:
        wanted = set(args.question_id)
        selected = [item for item in manifest if item["question_id"] in wanted]
        missing = wanted - {item["question_id"] for item in selected}
        if missing:
            raise SystemExit(f"question ids not in manifest: {sorted(missing)}")
        return selected
    return manifest[: args.limit] if args.limit is not None else manifest


def _model(args: argparse.Namespace) -> ModelConfig:
    return ModelConfig(
        base_url=args.base_url.rstrip("/"),
        model=args.model,
        temperature=0.0,
        enable_thinking=False,
        summary_max_tokens=args.manager_max_tokens,
        answer_max_tokens=args.answer_max_tokens,
        timeout_seconds=args.timeout_seconds,
    )


def _run_config(args: argparse.Namespace, model: ModelConfig, preflight: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "method": "memory_sidecar_strong",
        "prompt_version": PROMPT_VERSION,
        "model": asdict(model),
        "chunk_budget_tokens": args.chunk_budget_tokens,
        "manager_context_budget_tokens": args.manager_context_budget_tokens,
        "active_memory_budget_tokens": args.active_memory_budget_tokens,
        "update_ledger_budget_tokens": args.update_ledger_budget_tokens,
        "debug_max_chunks": args.debug_max_chunks,
        "recent_tail_budget_tokens": args.recent_tail_budget_tokens,
        "baseline_db": str(args.baseline_db),
        "baseline_run_id": args.baseline_run_id,
        "tokenizer_path": str(args.tokenizer_path) if args.tokenizer_path else None,
    }
    return {
        **payload,
        "config_fingerprint": sha256_text(json.dumps(payload, sort_keys=True)),
        "code_version": code_version(),
        "manager_max_tokens": args.manager_max_tokens,
        "answer_max_tokens": args.answer_max_tokens,
        "manifest": {"path": str(args.manifest), "sha256": file_sha256(args.manifest)},
        "source": {"path": str(args.source)},
        "service": preflight,
    }


def main() -> None:
    args = parse_args()
    if args.chunk_budget_tokens < 1 or args.manager_context_budget_tokens < 1 or args.max_concurrency < 1:
        raise SystemExit("token budgets and --max-concurrency must be >= 1")
    if not args.baseline_db.exists():
        raise SystemExit(f"baseline database not found: {args.baseline_db}")
    manifest = _select_manifest(args)
    if not manifest:
        raise SystemExit("no samples selected")
    source = load_source(args.source, {item["question_id"] for item in manifest})
    api_key = _api_key_from_env(args.api_key_env)

    # 在主进程解析/下载一次，再把确定的缓存路径交给各 worker；token 计数保持本地执行，
    # worker 也不会重复下载 HF 文件。
    local_tokenizer = LocalQwenTokenizer(tokenizer_path=str(args.tokenizer_path) if args.tokenizer_path else None)
    local_tokenizer.count("warmup")
    args.tokenizer_path = local_tokenizer.tokenizer_path

    model = _model(args)
    probe_client = QwenClient(model, api_key)
    preflight = probe_client.preflight()
    config = _run_config(args, model, preflight)

    run_dir = args.results_root / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    db_path = run_dir / "trajectory.sqlite3"
    store = TrajectoryStore(db_path)
    store.start_run(args.run_id, config)
    done = store.completed_question_ids(args.run_id, config["config_fingerprint"])
    pending = [item for item in manifest if item["question_id"] not in done]
    store.close()
    print(f"{len(manifest)} selected, {len(done)} already complete, {len(pending)} pending")

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.max_concurrency) as executor:
        futures = {
            executor.submit(
                process_one,
                source[item["question_id"]],
                args=args,
                config=config,
                db_path=db_path,
                api_key=api_key,
            ): item["question_id"]
            for item in pending
        }
        for index, future in enumerate(as_completed(futures), 1):
            result = future.result()
            print(f"[{index}/{len(pending)}] {result['question_id']} {result['status']}", flush=True)

    store = TrajectoryStore(db_path)
    exported = store.export_hypotheses(args.run_id, run_dir / "hypotheses.jsonl")
    statistics = store.run_statistics(args.run_id)
    wall_ms = int((time.monotonic() - started) * 1000)
    statistics.update({
        "run_id": args.run_id,
        "method": "memory_sidecar_strong",
        "config_fingerprint": config["config_fingerprint"],
        "hypotheses_exported": exported,
        "execution": {
            "max_concurrency": args.max_concurrency,
            "wall_time_ms": wall_ms,
            "samples_per_second": len(pending) / (wall_ms / 1000) if wall_ms else 0.0,
        },
    })
    (run_dir / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "run_summary.json").write_text(json.dumps(statistics, ensure_ascii=False, indent=2), encoding="utf-8")
    store.close()
    print(json.dumps(statistics, ensure_ascii=False))


if __name__ == "__main__":
    main()
