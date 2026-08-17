#!/bin/bash
# Stage 2 (backtest-change-rollout skill) sweep for the min_hold_hours=112
# compliance-hold floor (2026-08-17 backlog item, see docs/backlog_cache.md and
# scripts/run_soxl_minhold112_widehold_sweep.sh) -- extended to SPCL/QPUX per
# user's explicit call: this policy is only worth the compliance friction for a
# security with massive upside potential, and SPCL/QPUX are the only two that
# remotely qualify.
#
# Real data-length caveat, flagged and accepted by the user before running
# (2026-08-17): SPCL has only ~66 trading days of cached hourly history
# (2026-04-16 through 2026-08-14, likely a recently-listed product) -- barely
# enough for even ONE 16-36-day hold to complete inside the data window. Any
# SPCL result here is thin-sample/illustrative, not a real backtest in the
# normal sense -- treat accordingly, don't over-read it. QPUX has ~1 year
# (2025-08-07 through 2026-08-14) -- workable but still much shorter than
# SOXL's ~3-year history, expect a noisier/smaller-sample result there too.
#
# Same hold_time_caps correction as the SOXL widehold run: 112h through 252h
# in 7-hour (1-trading-day-bar) steps (16 through 36 trading days) -- so
# max_hold_hours can act as a real ceiling above the 112h floor instead of
# being swallowed by it (see that script's header for the full diagnosis).
#
# fixed_sl=2% for both -- neither ticker has a real live node/established SL
# value (unlike SOXL's 2%, which IS its real live config) -- user's explicit
# choice to match the SOXL convention rather than sweep fixed_sl too.
#
# TrailingBothZScoreBreakout only (the only kernel supporting min_hold_hours),
# full existing v5 grid (campaign_config.py) on take_profits/stop_losses/
# trail_pcts, full-history (no date-windowing combined with min_hold_hours --
# untested/unsupported per dispatch_parallel_grid's own comment).
#
# Version: base "v5-widehold", CLI auto-appends min_hold_version_suffix(112) ->
# "v5-widehold-minhold112" -- SAME version as the SOXL widehold run, since
# backtest_cache's version scoping is by (strategy, version, ticker, ...) --
# a shared version string across tickers is fine, no collision (this is the
# same pattern the real v5 campaign already uses across the whole watchlist).
#
# Per docs/backtest-change-rollout skill convention: the user runs this
# script themselves, not an agent.
#
# Usage: ./scripts/run_spcl_qpux_minhold112_sweep.sh [--dry-run]

set -e
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
TICKERS="SPCL QPUX"
STRATEGY="TrailingBothZScoreBreakout"
FIXED_SL="2"
BASE_VERSION="v5-widehold"
MIN_HOLD_HOURS="112"
# 112h through 252h in 7-hour (1-trading-day-bar) steps -- 16 through 36 trading
# days @ 7 bars/day (21 values). Same as the SOXL widehold run.
HOLD_TIME_CAPS="112 119 126 133 140 147 154 161 168 175 182 189 196 203 210 217 224 231 238 245 252"

dry_run=false
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=true; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

echo "SPCL/QPUX min_hold_hours=${MIN_HOLD_HOURS} campaign -- strategy=$STRATEGY, fixed_sl=${FIXED_SL}%"
echo "hold_time_caps (hours): $HOLD_TIME_CAPS"
echo "Final version will be: ${BASE_VERSION}-minhold${MIN_HOLD_HOURS}"
echo "CAVEAT: SPCL has only ~66 trading days of data -- expect very few/thin closed trades."

if [ "$dry_run" = true ]; then
  exit 0
fi

mkdir -p logs output
LOG="logs/spcl_qpux_minhold${MIN_HOLD_HOURS}_sweep_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to $LOG"

cp config.json config.json.bak
trap 'cp config.json.bak config.json; echo "config restored"' EXIT

{
  echo "======================================================"
  echo " SPCL/QPUX min_hold_hours=${MIN_HOLD_HOURS} campaign start — $(date)"
  echo "======================================================"

  campaign_start=$(date +%s)

  entry_timings=$($PYTHON scripts/campaign_config.py entry_timings "$STRATEGY")
  for et in $entry_timings; do
    $PYTHON scripts/campaign_config.py patch "$STRATEGY" "$FIXED_SL"
    $PYTHON -c "
import json
with open('config.json') as f:
    c = json.load(f)
c['hyperparameters']['hold_time_caps'] = [${HOLD_TIME_CAPS// /, }]
with open('config.json', 'w') as f:
    json.dump(c, f, indent=4)
print('Patched hold_time_caps:', c['hyperparameters']['hold_time_caps'])
"
    $PYTHON run_optimization_sweep.py --version "$BASE_VERSION" --entry-timing "$et" --max-phase 2.5 \
        --tickers $TICKERS --min-hold-hours "$MIN_HOLD_HOURS"
  done

  campaign_end=$(date +%s)
  echo ""
  echo "======================================================"
  echo " Campaign done — $(date). Total: $((campaign_end - campaign_start))s"
  echo " Final version: ${BASE_VERSION}-minhold${MIN_HOLD_HOURS}"
  echo "======================================================"
} 2>&1 | tee "$LOG"
