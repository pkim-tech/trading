#!/bin/bash
# Runs the liquidity-screen candidate pool (docs/backlog_cache.md's 2026-08-07
# "18-ticker watchlist is NOT liquidity-limited" finding --
# scripts/liquidity_screen_candidates.py's ranked output, broad-index/sector
# leveraged ETFs only, single-stock names excluded; EDC re-added per its own
# 2026-07-19 removal being unrelated to liquidity/quality; crypto-linked
# tickers BTCZ/BITX/ETHU/BITU last, user's call -- crypto doesn't reliably
# mean-revert/bounce back the way this strategy needs; ETHT dropped as a
# same-direction duplicate of ETHU) in tranches.
#
# Tranche 1 is deliberately just SQQQ alone: since we're not 100% confident
# prune_backtest_cache.py's island selection is fully correct (a real gap
# was found 2026-08-1x -- it doesn't group by entry_timing, confirmed
# live-triggerable on AGQ/GDXD's historical data), the first tranche is a
# cheap, fast validation round before committing to full-size batches.
# Every tranche runs the same validation regardless: locate each ticker's
# best (max robust-alpha) backtest_cache row BEFORE pruning, prune, then
# locate again AFTER -- if the winning row changed, that's real evidence of
# a pruning bug, and the script stops immediately (no marker written, so
# rerunning resumes at the same tranche once the bug's fixed) instead of
# silently continuing on data that might be wrong.
#
# Once a tranche's prune is validated, scripts/run_overlay_shim.py runs the
# real drought-overlay backtest (reusing drought_overlay_test.py's own
# tested functions) against each ticker's winning node -- a read-only
# smoke-test of overlay viability, not a promotion/live step.
#
# Idempotent / safe to cancel and rerun: each tranche writes a marker file
# under logs/.liquidity_tranche_state/ only after sweep + validated prune +
# overlay shim all finish cleanly. Re-running this same command skips any
# tranche whose marker already exists.
#
# Usage:
#   ./scripts/run_liquidity_tranches.sh          # runs all tranches not yet marked done
#   ./scripts/run_liquidity_tranches.sh --reset  # clears markers, starts over from tranche 1
#   ./scripts/run_liquidity_tranches.sh --status # prints which tranches are done, does nothing else

set -e
set -o pipefail
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
STATE_DIR="logs/.liquidity_tranche_state"

TRANCHE_1="SQQQ"
TRANCHE_2="TZA SPXL TNA SPXU SPXS"
TRANCHE_3="SCO QID SDS DRIP TECL"
TRANCHE_4="UVXY FNGU UGL TMF SDOW"
TRANCHE_5="URTY GLL FAS SRTY BOIL"
TRANCHE_6="TECS LABD KOLD JNUG YINN"
TRANCHE_7="BULZ ERY SPCL FNGD ERX"
TRANCHE_8="GUSH DXD JDST UWM TMV"
TRANCHE_9="FAZ TBT DFEN MQQQ TWM"
TRANCHE_10="SSG DRN DDM ROM SOLT"
TRANCHE_11="CWEB WEBL QPUX CURE OILU"
TRANCHE_12="EDC"
# Crypto-linked, run last -- lowest-priority to validate, not excluded outright.
TRANCHE_13="BTCZ BITX ETHU BITU"
ALL_TRANCHES="1 2 3 4 5 6 7 8 9 10 11 12 13"

mkdir -p logs "$STATE_DIR"

if [ "$1" = "--reset" ]; then
  rm -f "$STATE_DIR"/tranche_*.done
  echo "Cleared tranche markers -- next run starts from tranche 1."
  exit 0
fi

if [ "$1" = "--status" ]; then
  for n in $ALL_TRANCHES; do
    if [ -f "$STATE_DIR/tranche_${n}.done" ]; then
      echo "tranche $n: done ($(cat "$STATE_DIR/tranche_${n}.done"))"
    else
      echo "tranche $n: pending"
    fi
  done
  exit 0
fi

LOG="logs/liquidity_tranches_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to $LOG (console + file via tee)"

tickers_for() {
  case "$1" in
    1) echo "$TRANCHE_1" ;;
    2) echo "$TRANCHE_2" ;;
    3) echo "$TRANCHE_3" ;;
    4) echo "$TRANCHE_4" ;;
    5) echo "$TRANCHE_5" ;;
    6) echo "$TRANCHE_6" ;;
    7) echo "$TRANCHE_7" ;;
    8) echo "$TRANCHE_8" ;;
    9) echo "$TRANCHE_9" ;;
    10) echo "$TRANCHE_10" ;;
    11) echo "$TRANCHE_11" ;;
    12) echo "$TRANCHE_12" ;;
    13) echo "$TRANCHE_13" ;;
  esac
}

run_tranche() {
  local n="$1"
  local marker="$STATE_DIR/tranche_${n}.done"
  local tickers
  tickers="$(tickers_for "$n")"

  if [ -f "$marker" ]; then
    echo ""
    echo "--- Tranche $n already done ($(cat "$marker")) -- skipping. ---"
    return
  fi

  echo ""
  echo "======================================================"
  echo " Tranche $n sweep start — $(date)"
  echo " Tickers: $tickers"
  echo "======================================================"

  VERSION=v5 TICKERS="$tickers" FIXED_SLS="1 2 3" \
    STRATEGIES="TrailingBothZScoreBreakout TrailingExitZScoreBreakout" \
    ./scripts/run_sweep_queue.sh --skip-cache-refresh

  echo ""
  echo "--- Tranche $n sweep done — $(date). Locating best node PRE-prune. ---"
  local pre="$STATE_DIR/tranche_${n}_pre.txt"
  local post="$STATE_DIR/tranche_${n}_post.txt"
  $PYTHON scripts/locate_best_node.py $tickers --out "$pre"

  echo ""
  echo "--- Pruning backtest_cache to island-only. ---"
  $PYTHON scripts/prune_backtest_cache.py --dry-run
  $PYTHON scripts/prune_backtest_cache.py --build
  $PYTHON scripts/prune_backtest_cache.py --swap

  echo ""
  echo "--- Locating best node POST-prune, comparing. ---"
  $PYTHON scripts/locate_best_node.py $tickers --out "$post"

  if ! diff -q "$pre" "$post" > /dev/null; then
    echo ""
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    echo " PRUNE VALIDATION FAILED for tranche $n -- best node"
    echo " changed after pruning. This is a real pruning bug,"
    echo " not a warning to ignore. Stopping (no marker written)."
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    echo "--- PRE ---"; cat "$pre"
    echo "--- POST ---"; cat "$post"
    exit 1
  fi
  echo "Prune validation OK -- best node unchanged for all of: $tickers"

  echo ""
  echo "--- Running overlay shim for tranche $n. ---"
  $PYTHON scripts/run_overlay_shim.py $tickers --out "$STATE_DIR/tranche_${n}_overlay.csv" || \
    echo "(overlay shim non-fatal failure/no data -- see output above, tranche still marked done)"

  date > "$marker"
  echo "--- Tranche $n complete — $(date). ---"
}

{
  for n in $ALL_TRANCHES; do
    run_tranche "$n"
  done

  echo ""
  echo "Rebuilding indexes + refreshing dropdown/pivot/cliff caches..."
  $PYTHON -c "
from run_optimization_sweep import rebuild_indexes
from db_cache import refresh_dropdown_cache, refresh_pivot_cache, refresh_cliff_grid_cache
rebuild_indexes()
refresh_dropdown_cache()
refresh_pivot_cache(versions=['v5'])
refresh_cliff_grid_cache()
"

  echo ""
  echo "All tranches complete — $(date)"
} 2>&1 | tee "$LOG"
