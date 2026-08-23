#!/bin/bash
# Resumable GT (v6) campaign queue -- mirrors run_sweep_queue.sh's pattern for the
# legacy hourly kernel: one ticker's full Phase1->2->2.5 chain per
# run_ground_truth_phase1.py invocation, --skip-cache-refresh on every ticker except
# the final rebuild_indexes() call once at the end of the whole batch.
#
# Usage:
#   TICKERS="AGQ KORU SOXL" START=2021-08-23 END=2026-08-21 DATA_SOURCE=massive WORKERS=10 \
#     ./scripts/run_ground_truth_sweep_queue.sh [--skip-cache-refresh]
#
# Defaults: 12 real live tickers, full 5yr massive window, 10 workers.

set -eo pipefail
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
# Default list is all 12 real live tickers (state='live', archived_at IS NULL,
# starting_notional >= 5000; verified fresh via watch_list, 2026-08-22) -- 6
# TrailingBothZScoreBreakout (DPST/HIBL/JNUG/KORU/LABU/SOXL) + 6
# TrailingExitZScoreBreakout (AGQ/DFEN/GDXU/NUGT/UGL/WEBL). run_ground_truth_phase1.py
# now looks up the correct per-strategy grid from campaign_config.STRATEGIES for both
# strategies, so the TrailingExit six no longer need to be excluded (the prior
# 8-ticker default's comment claiming only AGQ/NUGT/UGL/WEBL were TrailingExit was
# itself wrong -- DFEN/GDXU are too, per paired review 2026-08-22 -- and traced back to
# load_live_node returning a stale archived row; see the fix in
# scripts/run_ground_truth_neighborhood.py). Override TICKERS= explicitly for a
# different/narrower set.
TICKERS="${TICKERS:-AGQ DFEN DPST GDXU HIBL JNUG KORU LABU NUGT SOXL UGL WEBL}"
START="${START:-2021-08-23}"
END="${END:-2026-08-21}"
DATA_SOURCE="${DATA_SOURCE:-massive}"
WORKERS="${WORKERS:-10}"

skip_refresh_flag=""
[ "$1" = "--skip-cache-refresh" ] && skip_refresh_flag="--skip-cache-refresh"

mkdir -p logs
LOG="logs/gt_sweep_queue_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to $LOG (console + file via tee)"

{
  echo "======================================================"
  echo " GT sweep queue start -- $(date)"
  echo " Tickers: $TICKERS"
  echo " Window: $START..$END  data_source=$DATA_SOURCE  workers=$WORKERS"
  echo "======================================================"

  failed_tickers=""
  for ticker in $TICKERS; do
    echo ""
    echo "=== $ticker -- $(date) ==="
    # Isolated per-ticker: a real failure (e.g. an incomplete-Phase1 RuntimeError, or
    # an unsupported strategy assertion) logs and moves to the next ticker instead of
    # aborting the whole multi-hour batch (real review finding, 2026-08-22 -- `set -e`
    # alone would kill every remaining ticker on the first failure).
    if ! $PYTHON scripts/run_ground_truth_phase1.py --ticker "$ticker" \
        --data-source "$DATA_SOURCE" --start "$START" --end "$END" \
        --workers "$WORKERS" --skip-cache-refresh; then
      echo "!!! $ticker FAILED -- continuing with remaining tickers !!!"
      failed_tickers="$failed_tickers $ticker"
    fi
  done

  if [ "$skip_refresh_flag" = "--skip-cache-refresh" ]; then
    echo ""
    echo "Skipping final index rebuild (--skip-cache-refresh)."
  else
    echo ""
    echo "Rebuilding indexes..."
    $PYTHON -c "
from run_optimization_sweep import rebuild_indexes
rebuild_indexes()
"
  fi

  echo ""
  if [ -n "$failed_tickers" ]; then
    echo "All done -- $(date) -- FAILED:$failed_tickers"
  else
    echo "All done -- $(date) -- all tickers succeeded"
  fi
  # Last command in the group -- its exit status is what ${PIPESTATUS[0]} reports below.
  [ -z "$failed_tickers" ]
} 2>&1 | tee "$LOG"
# With pipefail, ${PIPESTATUS[0]} is the brace-group's own exit code (not tee's) --
# a real per-ticker failure or an unhandled error inside the group now makes the whole
# script exit non-zero instead of always reporting success (real review finding,
# 2026-08-22 -- tee alone always exits 0 regardless of the piped command's status).
exit "${PIPESTATUS[0]}"
