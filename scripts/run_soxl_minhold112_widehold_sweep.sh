#!/bin/bash
# Follow-up to scripts/run_soxl_minhold_sweep.sh (Stage 2, backtest-change-rollout
# skill) -- corrects a real finding from that first run's top-10 review, 2026-08-17:
# every top node shared max_hold_hours=21, well BELOW the min_hold_hours=112 floor.
# Whenever max_hold_hours < min_hold_hours, the TIME-exit condition goes true early
# (hour 21) and then sits blocked until 112h, firing almost every time right at the
# floor -- so the entire hold_time_caps axis below 112 is degenerate under this
# floor (collapses to "wait exactly until 112h, exit at whatever price is there"),
# and the "top 10" nodes from that run weren't 10 meaningfully different configs,
# just near-duplicates of the same one. Confirmed via direct trade-by-trade replay
# (16/17 closed trades landed at held=112-113h).
#
# hold_time_caps here is corrected to a dense sweep STARTING at the floor and
# extending out in 7-hour (1-trading-day-bar) steps -- user's explicit choice,
# 2026-08-17: 112h through 252h (16 through 36 trading days, 7 bars/day) -- so
# max_hold_hours can actually act as a real ceiling above the floor instead of
# being swallowed by it, with one grid point per trading day rather than sparse
# weekly checkpoints. (v5's own historical hold_time_caps grid topped out at
# 140h/20 days, confirmed directly from config.json -- this deliberately goes
# beyond that, into territory v5 never swept, per user's explicit call.)
# Old sub-112h hold_time_caps rows from the first run are untouched in
# backtest_cache (never deleted, just superseded) -- this campaign writes under
# a DISTINCT version so it can't be confused with or silently mixed into that
# first, degenerate-grid run.
#
# Same scope decisions as the first run otherwise: single ticker (SOXL), full
# existing v5 grid (campaign_config.py's STRATEGIES["TrailingBothZScoreBreakout"])
# for take_profits/stop_losses/trail_pcts, min_hold_hours FIXED at 112h (not swept
# as its own axis), TrailingBothZScoreBreakout only, full-history (no date window).
#
# Version: base "v5-widehold", CLI auto-appends min_hold_version_suffix(112) ->
# "v5-widehold-minhold112" -- distinct from the first run's "v5-minhold112".
#
# Per docs/backtest-change-rollout skill convention: the user runs this
# script themselves, not an agent.
#
# Usage: ./scripts/run_soxl_minhold112_widehold_sweep.sh [--dry-run]

set -e
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
TICKER="SOXL"
STRATEGY="TrailingBothZScoreBreakout"
FIXED_SL="2"   # SOXL's real live fixed_sl (CLAUDE.md: window=10, fixed_sl=2.0)
BASE_VERSION="v5-widehold"
MIN_HOLD_HOURS="112"
# 112h through 252h in 7-hour (1-trading-day-bar) steps -- 16 through 36 trading
# days @ 7 bars/day (21 values).
HOLD_TIME_CAPS="112 119 126 133 140 147 154 161 168 175 182 189 196 203 210 217 224 231 238 245 252"

dry_run=false
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=true; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

echo "SOXL min_hold_hours=${MIN_HOLD_HOURS} campaign (corrected hold_time_caps) -- strategy=$STRATEGY"
echo "hold_time_caps (hours): $HOLD_TIME_CAPS"
echo "Final version will be: ${BASE_VERSION}-minhold${MIN_HOLD_HOURS}"

if [ "$dry_run" = true ]; then
  exit 0
fi

mkdir -p logs output
LOG="logs/soxl_minhold${MIN_HOLD_HOURS}_widehold_sweep_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to $LOG"

cp config.json config.json.bak
trap 'cp config.json.bak config.json; echo "config restored"' EXIT

{
  echo "======================================================"
  echo " SOXL min_hold_hours=${MIN_HOLD_HOURS} widehold campaign start — $(date)"
  echo "======================================================"

  campaign_start=$(date +%s)

  entry_timings=$($PYTHON scripts/campaign_config.py entry_timings "$STRATEGY")
  for et in $entry_timings; do
    $PYTHON scripts/campaign_config.py patch "$STRATEGY" "$FIXED_SL"
    # campaign_config.py's patch doesn't touch hold_time_caps -- override it here.
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
        --tickers "$TICKER" --min-hold-hours "$MIN_HOLD_HOURS"
  done

  campaign_end=$(date +%s)
  echo ""
  echo "======================================================"
  echo " Campaign done — $(date). Total: $((campaign_end - campaign_start))s"
  echo " Final version: ${BASE_VERSION}-minhold${MIN_HOLD_HOURS}"
  echo "======================================================"
} 2>&1 | tee "$LOG"
