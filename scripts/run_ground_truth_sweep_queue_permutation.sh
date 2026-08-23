#!/bin/bash
# Resumable GT (v6) PERMUTATION campaign queue -- candidate-discovery/permutation
# sweep, NOT the live-config-validation queue (that's run_ground_truth_sweep_queue.sh,
# which fixes each ticker's strategy/fixed_sl to whatever's currently live). This
# script mirrors the legacy run_sweep_queue.sh's STRATEGIES x FIXED_SLS x TICKERS loop
# shape for the GT/v6 kernel: sweeps BOTH strategies and multiple fixed_sl values for
# every ticker, unconstrained by current live account/strategy assignment, to find the
# best real alpha regardless of what's live today.
#
# Uses run_ground_truth_phase1.py's --strategy/--fixed-sl permutation mode (added
# 2026-08-22), which bypasses load_live_node() entirely when both are passed --
# config.json is never touched (the GT engine doesn't read it at all, unlike the
# legacy campaign_config.py patch step run_sweep_queue.sh needs for the hourly kernel).
#
# Usage:
#   TICKERS="AGQ SOXL" STRATEGIES="TrailingBothZScoreBreakout" FIXED_SLS="1 2" \
#     START=2021-08-23 END=2026-08-21 DATA_SOURCE=massive WORKERS=10 MAX_PHASE=1 \
#     ./scripts/run_ground_truth_sweep_queue_permutation.sh [--skip-cache-refresh]
#
# Defaults: 12 real live tickers (same set as run_ground_truth_sweep_queue.sh, state=
# 'live', archived_at IS NULL, starting_notional>=5000, verified fresh 2026-08-22),
# both strategies, FIXED_SLS="1 2 3" (matches legacy run_sweep_queue.sh's default),
# full 5yr massive window, 10 workers, MAX_PHASE=1 (coarse-only first cut -- override to
# 2.5 for the full Phase1->2->2.5 chain once promising combos are known; 72 combos at
# the full chain is expensive for a first discovery pass). Flag for coordinator: this ticker universe is
# a judgment call (could instead be the broader Tranche-1/liquidity-screened set) --
# confirm/adjust if a wider net is wanted for permutation/candidate-discovery specifically,
# since that's a different question ("what SHOULD be live") than the validation queue's
# ("does the kernel match what IS live").

set -eo pipefail
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
TICKERS="${TICKERS:-AGQ DFEN DPST GDXU HIBL JNUG KORU LABU NUGT SOXL UGL WEBL}"
STRATEGIES="${STRATEGIES:-TrailingBothZScoreBreakout TrailingExitZScoreBreakout}"
FIXED_SLS="${FIXED_SLS:-1 2 3}"
START="${START:-2021-08-23}"
END="${END:-2026-08-21}"
DATA_SOURCE="${DATA_SOURCE:-massive}"
WORKERS="${WORKERS:-10}"
# Paired review, 2026-08-22: default full Phase1->2->2.5 for every combo (72 combos at
# the 12-ticker/both-strategy/3-fixed_sl default) is expensive for a first discovery
# pass -- MAX_PHASE=1 gives a cheap coarse-only first cut; override to 2.5 once
# promising (ticker,strategy,fixed_sl) combos are known.
MAX_PHASE="${MAX_PHASE:-1}"

skip_refresh_flag=""
[ "$1" = "--skip-cache-refresh" ] && skip_refresh_flag="--skip-cache-refresh"

mkdir -p logs
LOG="logs/gt_sweep_queue_permutation_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to $LOG (console + file via tee)"

{
  echo "======================================================"
  echo " GT permutation sweep queue start -- $(date)"
  echo " Tickers: $TICKERS"
  echo " Strategies: $STRATEGIES"
  echo " Fixed SLs: $FIXED_SLS"
  echo " Window: $START..$END  data_source=$DATA_SOURCE  workers=$WORKERS"
  echo "======================================================"

  failed_combos=""
  for strategy in $STRATEGIES; do
    for ticker in $TICKERS; do
      for sl in $FIXED_SLS; do
        echo ""
        echo "=== $ticker | $strategy | fixed_sl=$sl -- $(date) ==="
        # Isolated per-combo: a real failure (unsupported strategy, incomplete-Phase1
        # RuntimeError, etc.) logs and moves to the next combo instead of aborting the
        # whole multi-hour batch (same convention as run_ground_truth_sweep_queue.sh).
        if ! $PYTHON scripts/run_ground_truth_phase1.py --ticker "$ticker" \
            --strategy "$strategy" --fixed-sl "$sl" --max-phase "$MAX_PHASE" \
            --data-source "$DATA_SOURCE" --start "$START" --end "$END" \
            --workers "$WORKERS" --skip-cache-refresh; then
          echo "!!! $ticker | $strategy | fixed_sl=$sl FAILED -- continuing with remaining combos !!!"
          failed_combos="$failed_combos ${ticker}/${strategy}/${sl}"
        fi
      done
    done
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
  if [ -n "$failed_combos" ]; then
    echo "All done -- $(date) -- FAILED:$failed_combos"
  else
    echo "All done -- $(date) -- all combos succeeded"
  fi
  # Last command in the group -- its exit status is what ${PIPESTATUS[0]} reports below.
  [ -z "$failed_combos" ]
} 2>&1 | tee "$LOG"
# With pipefail, ${PIPESTATUS[0]} is the brace-group's own exit code (not tee's) --
# a real per-combo failure or an unhandled error inside the group now makes the whole
# script exit non-zero instead of always reporting success (same fix as
# run_ground_truth_sweep_queue.sh -- tee alone always exits 0 regardless of the piped
# command's status).
exit "${PIPESTATUS[0]}"
