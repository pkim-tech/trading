#!/bin/bash
# Generic resumable backfill-sweep queue -- see .claude/skills/backtest-change-rollout.
# One (ticker, fixed_sl, strategy, entry_timing) combo per run_optimization_sweep.py
# invocation, looped here in bash, so any single ticker's Phase1->2->2.5 finishes
# independently of the others (mirrors the historical run_backfill_queue.sh
# pattern) instead of batching all tickers through one shared Phase1+Checkpoint1
# pass.
#
# Resumability: NOT a campaign-level existence/count check (deliberately removed
# 2026-07-20 -- a `campaign_config.py done` presence check had the identical
# blind spot as the row-count skip we disabled in run_phase1_coarse the same
# session: it trusted a cache hit without confirming it reflects current code,
# and a code edit mid-campaign would make it silently serve stale rows again).
# Every combo is always invoked; `dispatch_parallel_grid`'s own per-node cache
# lookup (keyed on the exact param tuple) is the one trusted mechanism -- a
# combo that's already fully done just submits zero new tasks and returns
# fast, so this is still cheap to resume, just without the false confidence of
# a campaign-level shortcut.
#
# Usage:
#   VERSION=v5 TICKERS="AGQ GDXD" FIXED_SLS="1 2 3" STRATEGIES="TrailingBothZScoreBreakout TrailingExitZScoreBreakout" \
#     ./scripts/run_sweep_queue.sh [--skip-cache-refresh]
#
# Defaults: VERSION=v5, TICKERS=full 18-ticker watchlist (watch_list id=57),
# FIXED_SLS="1 2 3", STRATEGIES=both.

set -e
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
VERSION="${VERSION:-v5}"
TICKERS="${TICKERS:-AGQ DPST DUST GDXD GDXU HIBL KORU LABU NAIL NUGT RETL SOXL TQQQ UDOW USD UVIX YANG ZSL}"
FIXED_SLS="${FIXED_SLS:-1 2 3}"
STRATEGIES="${STRATEGIES:-TrailingBothZScoreBreakout TrailingExitZScoreBreakout}"

skip_refresh_flag=""
[ "$1" = "--skip-cache-refresh" ] && skip_refresh_flag="--skip-cache-refresh"

cp config.json config.json.bak
trap 'cp config.json.bak config.json; echo "config restored"' EXIT

mkdir -p logs
LOG="logs/sweep_queue_${VERSION}_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to $LOG (console + file via tee)"

{
  echo "======================================================"
  echo " Sweep queue start ($VERSION) — $(date)"
  echo " Tickers: $TICKERS"
  echo " Fixed SLs: $FIXED_SLS"
  echo " Strategies: $STRATEGIES"
  echo "======================================================"

  for strategy in $STRATEGIES; do
    entry_timings=$($PYTHON scripts/campaign_config.py entry_timings "$strategy")
    for ticker in $TICKERS; do
      for sl in $FIXED_SLS; do
        for et in $entry_timings; do
          echo ""
          echo "=== $ticker | $strategy | fixed_sl=$sl | $et — $(date) ==="
          $PYTHON scripts/campaign_config.py patch "$strategy" "$sl"
          $PYTHON run_optimization_sweep.py --version "$VERSION" --entry-timing "$et" --max-phase 2.5 \
              --tickers "$ticker" --skip-cache-refresh
        done
      done
    done
  done

  if [ "$skip_refresh_flag" = "--skip-cache-refresh" ]; then
    echo ""
    echo "Skipping final cache refresh (--skip-cache-refresh)."
  else
    echo ""
    echo "Rebuilding indexes + final cache refresh..."
    $PYTHON -c "
from run_optimization_sweep import rebuild_indexes
from db_cache import refresh_dropdown_cache, refresh_pivot_cache, refresh_cliff_grid_cache
rebuild_indexes()
refresh_dropdown_cache()
refresh_pivot_cache(versions=['$VERSION'])
refresh_cliff_grid_cache()
"
  fi

  echo ""
  echo "All done — $(date)"
} 2>&1 | tee "$LOG"
