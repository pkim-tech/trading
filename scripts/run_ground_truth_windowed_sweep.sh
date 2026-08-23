#!/bin/bash
# 5-window rolling-start GT (v6) sweep, for testing whether a candidate's edge holds
# up across different historical starting points, not just the current default window.
#
# Windows: 5 total, each 4 years long. Oldest window starts 5 years ago (today's date
# minus 5y); each subsequent window's start moves 90 days later (so 5y, ~4.75y, ~4.5y,
# ~4.25y, ~4y ago), window length held fixed at 4 years. Computed dynamically off
# today's date every run -- not hardcoded -- so this stays correct as time passes.
# Requires ~5 years of cached history; confirmed 2026-08-23 that 11/12 real
# capital-at-stake tickers have massive_hourly_derived data back to 2021-08-23 (~5.0y).
# ETHU is the one exception (data only since 2024-06-04, ~2.2y) -- excluded from the
# default ticker list below, can still be forced via TICKERS= but Window 1-4 will fail
# (no data that far back).
#
# Version labeling: run_ground_truth_phase1.py auto-derives `version` from
# data_source + --start/--end via window_version_suffix() -- each window's rows land
# under a distinct version string (e.g. v6-massive-w2021-08-23_2025-08-23) with zero
# extra flags needed here, so all 5 windows x however many strategy/fixed_sl combos
# stay cleanly separable in backtest_cache afterward.
#
# Loop order: TICKER -> STRATEGY -> WINDOW -> FIXED_SL. One ticker+strategy runs to
# completion across all 5 windows before moving to the next strategy, then the next
# ticker -- matches this project's existing "finish one combo before starting the
# next" convention (run_ground_truth_sweep_queue_permutation.sh).
#
# Usage (ad hoc ticker list):
#   TICKERS="SOXL AGQ" STRATEGIES="TrailingBothZScoreBreakout" FIXED_SLS="2" \
#     ./scripts/run_ground_truth_windowed_sweep.sh
#
# Usage (single ticker, default strategies/SLs):
#   ./scripts/run_ground_truth_windowed_sweep.sh SOXL
#
# Usage (preconfigured tranche, from scripts/gt_tranches.txt -- same file the
# permutation queue script uses, so tranche membership stays in one place):
#   ./scripts/run_ground_truth_windowed_sweep.sh --tranches
#   TRANCHE=3 ./scripts/run_ground_truth_windowed_sweep.sh --tranches
#   ./scripts/run_ground_truth_windowed_sweep.sh --tranches --status
#   ./scripts/run_ground_truth_windowed_sweep.sh --tranches --reset
#
# Overridable env vars: TICKERS, STRATEGIES (default both), FIXED_SLS (default
# "1 2 3", matching run_ground_truth_sweep_queue_permutation.sh's own default),
# WORKERS, MAX_PHASE (default 2.5), DATA_SOURCE (default massive -- needed for the
# ~2021-08-23 floor these windows require; yahoo only goes back to ~2023-07-24).

set -eo pipefail
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
STRATEGIES="${STRATEGIES:-TrailingBothZScoreBreakout TrailingExitZScoreBreakout}"
FIXED_SLS="${FIXED_SLS:-1 2 3}"
WORKERS="${WORKERS:-10}"
MAX_PHASE="${MAX_PHASE:-2.5}"
DATA_SOURCE="${DATA_SOURCE:-massive}"

STATE_DIR="logs/.gt_windowed_tranche_state"
TRANCHES_FILE="scripts/gt_tranches.txt"

# --- Compute the 5 rolling windows off today's date -------------------------------
WINDOW_STARTS=()
WINDOW_ENDS=()
base_start=$(date -d "-5 years" +%Y-%m-%d)
for i in 0 1 2 3 4; do
  w_start=$(date -d "$base_start + $((i * 90)) days" +%Y-%m-%d)
  w_end=$(date -d "$w_start + 4 years" +%Y-%m-%d)
  WINDOW_STARTS+=("$w_start")
  WINDOW_ENDS+=("$w_end")
done

# --- Arg parsing: --tranches mode vs. positional/TICKERS ad hoc mode --------------
tranche_mode=0
status_flag=0
reset_flag=0
skip_refresh_flag=""
positional_tickers=""
for arg in "$@"; do
  case "$arg" in
    --tranches) tranche_mode=1 ;;
    --status) status_flag=1 ;;
    --reset) reset_flag=1 ;;
    --skip-cache-refresh) skip_refresh_flag="--skip-cache-refresh" ;;
    -*) echo "Unknown flag: $arg" >&2; exit 1 ;;
    *) positional_tickers="$positional_tickers $arg" ;;
  esac
done
positional_tickers="${positional_tickers# }"

if [ "$status_flag" = "1" ] || [ "$reset_flag" = "1" ]; then
  tranche_mode=1
fi

TICKERS="${TICKERS:-$positional_tickers}"
if [ -z "$TICKERS" ] && [ "$tranche_mode" = "0" ]; then
  TICKERS="AGQ DFEN DPST GDXU HIBL JNUG KORU LABU NUGT SOXL SOXS UGL WEBL"
fi

if [ "$tranche_mode" = "1" ]; then
  declare -A TRANCHE_TICKERS
  ALL_TRANCHES=""
  while IFS= read -r line; do
    [[ -z "$line" || "$line" =~ ^# ]] && continue
    if [[ "$line" =~ ^([0-9]+)\ (.+)$ ]]; then
      TRANCHE_TICKERS[${BASH_REMATCH[1]}]="${BASH_REMATCH[2]}"
      ALL_TRANCHES="$ALL_TRANCHES ${BASH_REMATCH[1]}"
    fi
  done < "$TRANCHES_FILE"
  ALL_TRANCHES="${ALL_TRANCHES# }"

  mkdir -p logs "$STATE_DIR"

  if [ "$reset_flag" = "1" ]; then
    rm -f "$STATE_DIR"/tranche_*.done
    echo "Cleared windowed-GT tranche markers -- next run starts from tranche 1."
    exit 0
  fi

  if [ "$status_flag" = "1" ]; then
    for n in $ALL_TRANCHES; do
      if [ -f "$STATE_DIR/tranche_${n}.done" ]; then
        echo "tranche $n: done ($(cat "$STATE_DIR/tranche_${n}.done"))"
      else
        echo "tranche $n: pending"
      fi
    done
    exit 0
  fi

  if [ -n "$TRANCHE" ]; then
    RUN_TRANCHES="$TRANCHE"
  else
    RUN_TRANCHES="$ALL_TRANCHES"
  fi
fi

mkdir -p logs
LOG="logs/gt_windowed_sweep_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to $LOG (console + file via tee)"

# Runs TICKER -> STRATEGY -> WINDOW -> FIXED_SL for a given ticker list. Sets
# COMBO_FAILURES on return. One bad combo logs and continues (never aborts the batch),
# same isolation convention as run_ground_truth_sweep_queue_permutation.sh.
run_combo_set() {
  local tickers="$1"
  COMBO_FAILURES=""
  for ticker in $tickers; do
    for strategy in $STRATEGIES; do
      for w in 0 1 2 3 4; do
        w_start="${WINDOW_STARTS[$w]}"
        w_end="${WINDOW_ENDS[$w]}"
        for sl in $FIXED_SLS; do
          echo ""
          echo "=== $ticker | $strategy | window $((w+1))/5 ($w_start..$w_end) | fixed_sl=$sl -- $(date) ==="
          if ! $PYTHON scripts/run_ground_truth_phase1.py --ticker "$ticker" \
              --strategy "$strategy" --fixed-sl "$sl" --max-phase "$MAX_PHASE" \
              --data-source "$DATA_SOURCE" --start "$w_start" --end "$w_end" \
              --workers "$WORKERS" --skip-cache-refresh; then
            echo "!!! $ticker | $strategy | window $((w+1))/5 | fixed_sl=$sl FAILED -- continuing !!!"
            COMBO_FAILURES="$COMBO_FAILURES ${ticker}/${strategy}/w$((w+1))/${sl}"
          fi
        done
      done
    done
  done
}

{
  echo "======================================================"
  echo " GT windowed (5x rolling-start) sweep start -- $(date)"
  echo " Strategies: $STRATEGIES"
  echo " Fixed SLs: $FIXED_SLS"
  echo " Windows:"
  for w in 0 1 2 3 4; do
    echo "   $((w+1)): ${WINDOW_STARTS[$w]}..${WINDOW_ENDS[$w]}"
  done
  echo " data_source=$DATA_SOURCE workers=$WORKERS max_phase=$MAX_PHASE"
  echo "======================================================"

  failed_combos=""

  if [ "$tranche_mode" = "1" ]; then
    echo " Tranches to run: $RUN_TRANCHES"
    for n in $RUN_TRANCHES; do
      marker="$STATE_DIR/tranche_${n}.done"
      tranche_tickers="${TRANCHE_TICKERS[$n]}"

      if [ -z "$tranche_tickers" ]; then
        echo ""
        echo "!!! Tranche $n has no tickers in $TRANCHES_FILE -- skipping, NOT marking done. !!!"
        failed_combos="$failed_combos tranche_${n}/unknown-tranche"
        continue
      fi

      if [ -f "$marker" ]; then
        echo ""
        echo "--- Tranche $n already done ($(cat "$marker")) -- skipping. ---"
        continue
      fi

      echo ""
      echo "------------------------------------------------------"
      echo " Tranche $n start -- $(date)"
      echo " Tickers: $tranche_tickers"
      echo "------------------------------------------------------"

      run_combo_set "$tranche_tickers"

      if [ -z "$COMBO_FAILURES" ]; then
        date > "$marker"
        echo "--- Tranche $n complete -- $(date). ---"
      else
        echo "--- Tranche $n had failures, NOT marking done:$COMBO_FAILURES ---"
        failed_combos="$failed_combos $COMBO_FAILURES"
      fi
    done
  else
    echo " Tickers: $TICKERS"
    run_combo_set "$TICKERS"
    failed_combos="$COMBO_FAILURES"
  fi

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
  [ -z "$failed_combos" ]
} 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
