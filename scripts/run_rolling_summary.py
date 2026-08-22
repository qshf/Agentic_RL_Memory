"""Run the Rolling Summary V1 baseline (or the Full Context arm) on a manifest.

Examples:
    uv run python scripts/run_rolling_summary.py \
      --manifest data/samples/longmemeval_s_smoke_12_seed_20260822.csv \
      --run-id rolling-summary-smoke-v1

    uv run python scripts/run_rolling_summary.py \
      --manifest data/samples/longmemeval_s_smoke_12_seed_20260822.csv \
      --run-id full-context-smoke-v1 --method full_context
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rolling_summary.config import (  # noqa: E402
    METHOD_FULL_CONTEXT,
    METHOD_ROLLING_SUMMARY,
    BudgetConfig,
    ModelConfig,
)
from rolling_summary.local_tokenizer import LocalQwenTokenizer  # noqa: E402
from rolling_summary.runner import DEFAULT_RESULTS_ROOT, DEFAULT_SOURCE, run  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, required=True, help="Frozen CSV sample manifest")
    parser.add_argument("--run-id", required=True, help="Run directory name under the results root")
    parser.add_argument(
        "--method",
        choices=[METHOD_ROLLING_SUMMARY, METHOD_FULL_CONTEXT],
        default=METHOD_ROLLING_SUMMARY,
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Official S JSON path")
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N manifest rows")
    parser.add_argument(
        "--question-id",
        nargs="*",
        default=None,
        help="Only run these question ids (must be present in the manifest)",
    )
    parser.add_argument("--rolling-trigger-tokens", type=int, default=BudgetConfig.rolling_trigger_tokens)
    parser.add_argument("--summary-budget-tokens", type=int, default=BudgetConfig.summary_budget_tokens)
    parser.add_argument("--compress-prefix-tokens", type=int, default=BudgetConfig.compress_prefix_tokens)
    parser.add_argument(
        "--max-concurrency", type=int, default=1,
        help="Independent samples in flight; use 2 first for a single-GPU server",
    )
    args = parser.parse_args()

    budgets = BudgetConfig(
        rolling_trigger_tokens=args.rolling_trigger_tokens,
        summary_budget_tokens=args.summary_budget_tokens,
        compress_prefix_tokens=args.compress_prefix_tokens,
    )
    model = ModelConfig.from_env(summary_max_tokens=budgets.summary_budget_tokens)
    statistics = run(
        manifest_path=args.manifest,
        run_id=args.run_id,
        method=args.method,
        source_path=args.source,
        results_root=args.results_root,
        budgets=budgets,
        model=model,
        limit=args.limit,
        question_ids=args.question_id,
        tokenizer=LocalQwenTokenizer(),
        max_concurrency=args.max_concurrency,
    )
    print(f"Status counts: {statistics['status_counts']}")
    print(
        f"Tokens: input={statistics['total_input_tokens']} output={statistics['total_output_tokens']} "
        f"calls={statistics['total_calls']} compressions={statistics['total_compressions']}"
    )
    execution = statistics["execution"]
    print(
        f"Throughput: concurrency={execution['max_concurrency']} wall={execution['wall_time_ms']}ms "
        f"samples/s={execution['samples_per_second']:.3f} model_tokens/s={execution['model_tokens_per_second']:.1f}"
    )


if __name__ == "__main__":
    main()
