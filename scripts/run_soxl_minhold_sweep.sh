#!/bin/bash
# Stage 2 (backtest-change-rollout skill) single-ticker sweep for the new
# min_hold_hours compliance-hold floor (2026-08-17, real backlog item relayed
# via peer session "planner" -- see docs/backlog_cache.md). Real firm
# compliance policy: 15-trading-day minimum hold, blocks ALL exits (incl. SL)
# during the window -- research/backtest-only, no live-automation path ever.
# 112h = 16 trading days (7 bars/day, confirmed against real SOXL_1h.csv data)
# -- user's explicit choice, stricter than the stated 15-day minimum, never
# short of it in any entry-timing case.
#
# Scope, per user's explicit decisions (2026-08-17): single ticker (SOXL),
# full existing v5 grid (campaign_config.py's STRATEGIES["TrailingBothZScoreBreakout"]
# -- no biasing/narrowing), min_hold_hours FIXED at 112h (not swept as its own
# grid axis -- compliance mandates a floor, not an optimization target).
# TrailingBothZScoreBreakout only -- the only strategy whose kernel
# (backtester.py::run_backtest_v110) supports the floor; run_backtest_dispatch
# raises ValueError for any other strategy given a nonzero min_hold_hours.
#
# Full-history run, no date-windowing -- combining --start-date/--end-date with
# --min-hold-hours is untested/unsupported per dispatch_parallel_grid's own
# comment (2026-08-17), so this deliberately doesn't attempt it.
#
# Version: base "v5", CLI auto-appends min_hold_version_suffix(112) ->
# "v5-minhold112" -- a brand new, fully isolated version string (no PK/schema
# migration needed, same precedent as window_version_suffix's date-windowing
# design, see run_optimization_sweep.py's min_hold_version_suffix()).
#
# Per docs/backtest-change-rollout skill convention: the user runs this
# script themselves, not an agent.
#
# Usage: ./scripts/run_soxl_minhold_sweep.sh [--dry-run]

set -e
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
TICKER="SOXL"
STRATEGY="TrailingBothZScoreBreakout"
FIXED_SL="2"   # SOXL's real live fixed_sl (CLAUDE.md: window=10, fixed_sl=2.0)
BASE_VERSION="v5"
MIN_HOLD_HOURS="112"

dry_run=false
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=true; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

echo "SOXL min_hold_hours=${MIN_HOLD_HOURS} campaign -- strategy=$STRATEGY, full v5 grid, --max-phase 2.5"
echo "Final version will be: ${BASE_VERSION}-minhold${MIN_HOLD_HOURS}"

if [ "$dry_run" = true ]; then
  exit 0
fi

mkdir -p logs output
LOG="logs/soxl_minhold${MIN_HOLD_HOURS}_sweep_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to $LOG"

cp config.json config.json.bak
trap 'cp config.json.bak config.json; echo "config restored"' EXIT

{
  echo "======================================================"
  echo " SOXL min_hold_hours=${MIN_HOLD_HOURS} campaign start — $(date)"
  echo "======================================================"

  campaign_start=$(date +%s)

  entry_timings=$($PYTHON scripts/campaign_config.py entry_timings "$STRATEGY")
  for et in $entry_timings; do
    $PYTHON scripts/campaign_config.py patch "$STRATEGY" "$FIXED_SL"
    $PYTHON run_optimization_sweep.py --version "$BASE_VERSION" --entry-timing "$et" --max-phase 2.5 \
        --tickers "$TICKER" --min-hold-hours "$MIN_HOLD_HOURS"
  done

  campaign_end=$(date +%s)
  echo ""
  echo "======================================================"
  echo " Campaign done — $(date). Total: $((campaign_end - campaign_start))s"
  echo " Final version: ${BASE_VERSION}-minhold${MIN_HOLD_HOURS}"
  echo "======================================================"
} 2>&1 | tee "$LOG"
