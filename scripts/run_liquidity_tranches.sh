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

# Campaign params + tranche membership live in scripts/liquidity_tranches.txt
# -- the single source of truth, also read directly by
# scripts/liquidity_tranche_progress.py. Don't hardcode any of this a
# second time in either script; see that file's header comment for the
# reorg rationale (2026-08-11).
TRANCHES_FILE="scripts/liquidity_tranches.txt"
declare -A TRANCHE_TICKERS
ALL_TRANCHES=""
while IFS= read -r line; do
  [[ -z "$line" || "$line" =~ ^# ]] && continue
  if [[ "$line" =~ ^([0-9]+)\ (.+)$ ]]; then
    TRANCHE_TICKERS[${BASH_REMATCH[1]}]="${BASH_REMATCH[2]}"
    ALL_TRANCHES="$ALL_TRANCHES ${BASH_REMATCH[1]}"
  elif [[ "$line" =~ ^([A-Z_]+)=(.+)$ ]]; then
    declare "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}"
  fi
done < "$TRANCHES_FILE"
ALL_TRANCHES="${ALL_TRANCHES# }"
: "${VERSION:?VERSION not found in $TRANCHES_FILE}"
: "${FIXED_SLS:?FIXED_SLS not found in $TRANCHES_FILE}"
: "${STRATEGIES:?STRATEGIES not found in $TRANCHES_FILE}"
: "${ENTRY_TIMING:?ENTRY_TIMING not found in $TRANCHES_FILE}"

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
  echo "${TRANCHE_TICKERS[$1]}"
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

  # Bad-tick data scan (2026-08-11) -- catches the DFEN shape (a single bad
  # print fabricating a fake headline return) BEFORE burning compute
  # sweeping garbage data, not after chasing an outlier number back to its
  # cause. Advisory only: logs to bad_tick_scan_log and prints a warning,
  # does not block the sweep -- findings can be addressed (patch the CSV,
  # like DFEN) or deliberately deferred, user's call either way.
  echo ""
  echo "--- Tranche $n pre-sweep data scan ---"
  set +e
  $PYTHON scripts/scan_bad_ticks.py --tickers $tickers
  scan_rc=$?
  set -e
  if [ "$scan_rc" -eq 2 ]; then
    echo ""
    echo "########################################################"
    echo "# WARNING: bad-tick data found above for tranche $n tickers."
    echo "# NOT auto-fixed, NOT blocking the sweep -- run"
    echo "#   .venv/bin/python scripts/scan_bad_ticks.py --fix --tickers $tickers"
    echo "# to patch it, then resweep the affected ticker(s)."
    echo "########################################################"
    echo ""
  elif [ "$scan_rc" -ne 0 ]; then
    echo "(scan_bad_ticks.py failed with exit $scan_rc -- not blocking the sweep, but the data wasn't checked this run)"
  fi

  # v5.1 (2026-08-11): version bump after the under-caching data-gap fix (trading_incidents
  # id=5) -- dispatch_parallel_grid's cache-hit check has no data-freshness awareness, so
  # resweeping under the old 'v5' tag would silently skip every already-covered grid
  # coordinate rather than recompute against the now-longer real history. The 23 tranche
  # tickers whose raw data didn't actually change tonight already have their v5 rows copied
  # forward to v5.1 (tagged copied_from_version='v5' in backtest_cache) -- this sweep will
  # correctly skip those (already cached under v5.1) and only really compute the tickers
  # that need it. See docs/backlog_cache.md's 2026-08-11 v5.1 entry.
  # VERSION/FIXED_SLS/STRATEGIES come from scripts/liquidity_tranches.txt, not hardcoded here.
  VERSION="$VERSION" TICKERS="$tickers" FIXED_SLS="$FIXED_SLS" \
    STRATEGIES="$STRATEGIES" \
    ./scripts/run_sweep_queue.sh --skip-cache-refresh

  echo ""
  echo "--- Tranche $n sweep done — $(date). Running full extract+validate (all tickers/groups, not just this tranche's). ---"
  # Was: separate --dry-run/--build/--swap plus a locate_best_node.py pre/post
  # diff scoped to just this tranche's tickers. Replaced 2026-08-07 after that
  # flow silently no-op'd on tranche 2 -- cmd_swap() now requires a validation
  # sentinel (added same day to stop an unvalidated build from being swapped
  # in by hand), but this script still called --swap directly without ever
  # running the validator that writes it. cmd_swap()'s refusal prints and
  # returns (exit 0), which `set -e` can't catch, so the script silently
  # continued, compared PRE against POST (trivially equal since nothing had
  # actually changed), and marked the tranche done -- with the live DB never
  # actually shrinking. scripts/full_db_prune_validate.py both replaces the
  # weaker tranche-scoped pre/post check (it validates every real
  # (ticker,strategy,version,window,z,entry_timing) group, not just one row
  # per this tranche's tickers) AND writes the sentinel --swap now requires,
  # so this single call does both jobs.
  $PYTHON scripts/full_db_prune_validate.py
  $PYTHON scripts/prune_backtest_cache.py --swap
  echo "Extract validation OK -- swapped in."

  echo ""
  echo "--- Running overlay shim for tranche $n. ---"
  $PYTHON scripts/run_overlay_shim.py $tickers --version "$VERSION" --out "$STATE_DIR/tranche_${n}_overlay.csv" || \
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
