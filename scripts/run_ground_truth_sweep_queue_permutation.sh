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
# Usage (flat mode -- unchanged from before, still the default):
#   TICKERS="AGQ SOXL" STRATEGIES="TrailingBothZScoreBreakout" FIXED_SLS="1 2" \
#     START=2021-08-23 END=2026-08-21 DATA_SOURCE=massive WORKERS=10 MAX_PHASE=1 \
#     ./scripts/run_ground_truth_sweep_queue_permutation.sh [--skip-cache-refresh]
#
# Usage (tranche mode, added 2026-08-23 -- mirrors run_liquidity_tranches.sh's
# resumability mechanics exactly, see scripts/gt_tranches.txt for membership):
#   ./scripts/run_ground_truth_sweep_queue_permutation.sh --tranches
#       # runs all tranches from scripts/gt_tranches.txt not yet marked done, in order
#   ./scripts/run_ground_truth_sweep_queue_permutation.sh --tranches --status
#       # prints done/pending per tranche, does nothing else
#   ./scripts/run_ground_truth_sweep_queue_permutation.sh --tranches --reset
#       # clears all tranche markers, next --tranches run starts over from tranche 1
#   TRANCHE=5 ./scripts/run_ground_truth_sweep_queue_permutation.sh --tranches
#       # scopes to a single tranche instead of the "all not-done" default
#
# Design notes on gt_tranches.txt vs. the legacy liquidity_tranches.txt format:
# the legacy file also carries VERSION=/FIXED_SLS=/STRATEGIES=/ENTRY_TIMING= metadata
# lines, hard-required by run_liquidity_tranches.sh. gt_tranches.txt deliberately does
# NOT carry those: run_ground_truth_phase1.py auto-derives `version` from
# data_source+window (window_version_suffix) rather than accepting it as an input, and
# ENTRY_TIMING is a hardcoded constant ("open_check") inside that script, not a CLI
# flag -- neither is a real overridable knob for the GT engine, so embedding them in
# the tranche file would just be dead metadata. STRATEGIES/FIXED_SLS ARE real knobs,
# but they already have sensible script-level env-var defaults (see below) matching
# this script's own pre-existing convention -- tranche mode reuses those same env
# vars unchanged rather than duplicating them per-tranche-file-line. gt_tranches.txt's
# job is purely tranche membership/ordering.
#
# Tranche mode reuses the exact same per-combo failure-isolation loop as flat mode
# (one bad combo logs and continues, never aborts the batch) -- a tranche's .done
# marker is only written if every combo in that tranche's ticker list succeeded,
# matching run_liquidity_tranches.sh's "marker only after everything finishes
# cleanly" contract. A tranche with any failed combo is left pending (marker not
# written) so a rerun retries it, and the script moves on to the next tranche rather
# than aborting the whole multi-hour run (same isolation philosophy as combo-level
# failures already had).

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
# Corrected 2026-08-23: default is full Phase1->2->2.5 per combo (finish one
# ticker/strategy/fixed_sl combination all the way before moving to the next),
# not a phase1-only breadth-first pass across every combo. Override to 1 (or 2)
# explicitly for a deliberate cheap coarse-only first cut.
MAX_PHASE="${MAX_PHASE:-2.5}"

STATE_DIR="logs/.gt_tranche_state"
TRANCHES_FILE="scripts/gt_tranches.txt"

tranche_mode=0
status_flag=0
reset_flag=0
skip_refresh_flag=""
for arg in "$@"; do
  case "$arg" in
    --tranches) tranche_mode=1 ;;
    --status) status_flag=1 ;;
    --reset) reset_flag=1 ;;
    --skip-cache-refresh) skip_refresh_flag="--skip-cache-refresh" ;;
  esac
done

# --status/--reset only mean anything in tranche mode -- imply tranche_mode
# rather than silently falling through to a full (expensive) flat-mode sweep
# if --tranches was forgotten (real bug caught in paired review, 2026-08-23).
if [ "$status_flag" = "1" ] || [ "$reset_flag" = "1" ]; then
  tranche_mode=1
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
    echo "Cleared GT tranche markers -- next run starts from tranche 1."
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

  # TRANCHE=<n> scopes to a single tranche; default (unset) runs all not-yet-done
  # tranches, matching run_liquidity_tranches.sh's default convention.
  if [ -n "$TRANCHE" ]; then
    RUN_TRANCHES="$TRANCHE"
  else
    RUN_TRANCHES="$ALL_TRANCHES"
  fi
fi

mkdir -p logs
LOG="logs/gt_sweep_queue_permutation_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to $LOG (console + file via tee)"

# Runs the strategy x ticker x fixed_sl loop against a given ticker list; sets
# COMBO_FAILURES (space-separated ticker/strategy/sl triples) on return. Shared by
# both flat mode (called once with $TICKERS) and tranche mode (called once per
# tranche with that tranche's ticker list).
run_combo_set() {
  local tickers="$1"
  COMBO_FAILURES=""
  for strategy in $STRATEGIES; do
    for ticker in $tickers; do
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
          COMBO_FAILURES="$COMBO_FAILURES ${ticker}/${strategy}/${sl}"
        fi
      done
    done
  done
}

{
  failed_combos=""

  if [ "$tranche_mode" = "1" ]; then
    echo "======================================================"
    echo " GT permutation TRANCHE sweep start -- $(date)"
    echo " Tranches to run: $RUN_TRANCHES"
    echo " Strategies: $STRATEGIES"
    echo " Fixed SLs: $FIXED_SLS"
    echo " Window: $START..$END  data_source=$DATA_SOURCE  workers=$WORKERS"
    echo "======================================================"

    for n in $RUN_TRANCHES; do
      marker="$STATE_DIR/tranche_${n}.done"
      tranche_tickers="${TRANCHE_TICKERS[$n]}"

      # Guard against a typo'd/stale TRANCHE=<n> (or a bad tranche file) --
      # without this, an empty ticker list runs zero combos, COMBO_FAILURES
      # stays empty, and the tranche gets falsely marked done (real bug
      # caught in paired review, 2026-08-23).
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
    echo "======================================================"
    echo " GT permutation sweep queue start -- $(date)"
    echo " Tickers: $TICKERS"
    echo " Strategies: $STRATEGIES"
    echo " Fixed SLs: $FIXED_SLS"
    echo " Window: $START..$END  data_source=$DATA_SOURCE  workers=$WORKERS"
    echo "======================================================"

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
  # Last command in the group -- its exit status is what ${PIPESTATUS[0]} reports below.
  [ -z "$failed_combos" ]
} 2>&1 | tee "$LOG"
# With pipefail, ${PIPESTATUS[0]} is the brace-group's own exit code (not tee's) --
# a real per-combo failure or an unhandled error inside the group now makes the whole
# script exit non-zero instead of always reporting success (same fix as
# run_ground_truth_sweep_queue.sh -- tee alone always exits 0 regardless of the piped
# command's status).
exit "${PIPESTATUS[0]}"
