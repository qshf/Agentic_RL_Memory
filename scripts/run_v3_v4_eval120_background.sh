#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
set -a
source .env
set +a

MANIFEST=data/samples/longmemeval_s_eval_120_seed_20260821.csv
SOURCE=data/official_longmemeval/longmemeval_s_cleaned.json
BASELINE_DB=results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/trajectory.sqlite3
LOG_DIR=results/memory_sidecar/eval120-background-20260830
mkdir -p "$LOG_DIR"

V3_RUN=sidecar-v3-eval120-c2048-20260830-new96
V3_IDS=( $(python - <<'PY'
import csv, sqlite3
full=[r['question_id'] for r in csv.DictReader(open('data/samples/longmemeval_s_eval_120_seed_20260821.csv'))]
old={r[0] for r in sqlite3.connect('results/memory_sidecar/sidecar-v3-memory-summary-off-24-20260828-c2048/trajectory.sqlite3').execute("select question_id from samples where status='completed'")}
print('\n'.join(q for q in full if q not in old))
PY
) )

echo "[$(date)] V3 start: ${#V3_IDS[@]} missing ids" | tee -a "$LOG_DIR/runner.log"
PYTHONUNBUFFERED=1 python -u scripts/run_memory_sidecar_strong.py \
  --manifest "$MANIFEST" --limit 120 --question-id "${V3_IDS[@]}" \
  --source "$SOURCE" --baseline-db "$BASELINE_DB" \
  --baseline-run-id rolling-summary-eval120-v1-atomic-c2 \
  --run-id "$V3_RUN" --results-root results/memory_sidecar \
  --protocol v3 --chunk-budget-tokens 2048 \
  --manager-context-budget-tokens 12288 --active-memory-budget-tokens 8192 \
  --update-ledger-budget-tokens 2048 --recent-tail-budget-tokens 16384 \
  --shared-context-budget-tokens 81920 --max-concurrency 2 \
  --compactor off --answer-max-tokens 1024 \
  >> "$LOG_DIR/v3.log" 2>&1

V4_RUN=sidecar-v4-eval120-c2048-20260830-new96
V4_IDS=( $(python - <<'PY'
import csv, json
full=[r['question_id'] for r in csv.DictReader(open('data/samples/longmemeval_s_eval_120_seed_20260821.csv'))]
done=set()
for path in ('results/memory_sidecar/sidecar-v4-pilot24-shard1-20260829/e2e.json', 'results/memory_sidecar/sidecar-v4-pilot24-shard2-20260829/e2e.json'):
    for row in json.load(open(path)).get('samples', []):
        if row.get('status') == 'completed': done.add(row['question_id'])
print('\n'.join(q for q in full if q not in done))
PY
) )

echo "[$(date)] V4 start: ${#V4_IDS[@]} missing ids" | tee -a "$LOG_DIR/runner.log"
PYTHONUNBUFFERED=1 python -u scripts/run_v4_e2e.py \
  --question-id "${V4_IDS[@]}" --projection graph-all \
  --run-id "$V4_RUN" \
  --db "results/memory_sidecar/$V4_RUN/trajectory.sqlite3" \
  --output "results/memory_sidecar/$V4_RUN/e2e.json" \
  >> "$LOG_DIR/v4.log" 2>&1

echo "[$(date)] V3 and V4 completed" | tee -a "$LOG_DIR/runner.log"
