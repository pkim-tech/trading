#!/bin/bash
# Quarterly rolling N-year-window backtest campaign, SOXL only (built for the
# "quarterly resweep cadence" backlog item, 2026-08-11/2026-08-16, generalized
# to --lookback-years 2026-08-16 to compare lookback lengths after the 1-year
# version's real walk-forward chain showed a genuinely out-of-sample -56.2%
# result -- see docs/backlog_cache.md). Answers: how does SOXL's best
# (CAGR-first) config drift across quarterly rebalance decision points,
# historically, and does a longer lookback reduce that drift/overfitting?
#
# Windows: calendar-aligned, quarterly-stepped, each exactly --lookback-years
# long, from SOXL's real cached-data start (2023-07-24) through the most
# recent full quarter. Each window gets a real full Phase1->2->2.5 grid sweep
# (both strategies, matching run_sweep_queue.sh's default), persisted under
# its own version=v5-w{start}_{end} (per the Stage 2 date-windowing build,
# commit 427669a -- no schema/cache-collision risk, every consumer already
# scopes by version alone). After all windows sweep, runs
# candidate_full_review.py per window (CAGR-first convention, confirmed
# 2026-08-10 as the user's real selection practice) and appends one row per
# window to a summary CSV.
#
# Fixed 2026-08-16: previously passed an ALREADY-windowed version string
# (e.g. "v5-w2025-07-01_2026-06-30") as run_optimization_sweep.py's --version
# while ALSO passing --start-date/--end-date -- that script's own Stage 2
# logic appends window_version_suffix() on top of whatever --version it's
# given, so the real stored version ended up double-suffixed
# ("v5-w2025-07-01_2026-06-30-w2025-07-01_2026-06-30"), silently breaking
# every downstream candidate_full_review.py lookup (which used the
# undoubled string) -- all 8 original candidate CSVs came back blank despite
# the sweep itself computing real data. Now passes a bare base version
# ("v5") to run_optimization_sweep.py and computes the real final
# (correctly-suffixed) version separately for candidate_full_review.py/the
# summary CSV, so the two always agree.
#
# Per docs/backtest-change-rollout skill convention: the user runs this
# script themselves, not an agent. Times the whole run (real capacity data
# for future quarterly cadence planning).
#
# Usage: ./scripts/run_quarterly_soxl_sweep.sh [--lookback-years N] [--skip-sweep] [--dry-run]
#   --lookback-years N : each window's length in years (default 1)
#   --skip-sweep : skip the sweep phase, only (re)run candidate selection
#                  against windows already swept (for re-running Part 2 alone)
#   --dry-run    : print the computed windows and exit, no sweep/selection

set -e
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
TICKER="SOXL"
STRATEGIES="TrailingBothZScoreBreakout TrailingExitZScoreBreakout"
FIXED_SL="2"   # SOXL's real live fixed_sl (CLAUDE.md: window=10, fixed_sl=2.0)
DATA_START="2023-07-24"
BASE_VERSION="v5"
LOOKBACK_YEARS="1"

skip_sweep=false
dry_run=false
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-sweep) skip_sweep=true; shift ;;
    --dry-run) dry_run=true; shift ;;
    --lookback-years) LOOKBACK_YEARS="$2"; shift 2 ;;
    *) shift ;;
  esac
done

# --- Compute calendar-aligned quarterly-stepped N-year windows ---
windows=$($PYTHON -c "
import pandas as pd
start = pd.Timestamp('$DATA_START')
# first quarter boundary on/after data start
q_start = pd.Timestamp(start.year, ((start.month - 1) // 3) * 3 + 1, 1)
if q_start < start:
    q_start += pd.DateOffset(months=3)
end_ceiling = pd.Timestamp.today().normalize()
w = q_start
out = []
while w + pd.DateOffset(years=$LOOKBACK_YEARS) <= end_ceiling:
    w_end = w + pd.DateOffset(years=$LOOKBACK_YEARS) - pd.Timedelta(days=1)
    out.append(f'{w.date()},{w_end.date()}')
    w += pd.DateOffset(months=3)
print('\n'.join(out))
")

n_windows=$(echo "$windows" | grep -c .)
echo "SOXL quarterly rolling-window campaign: $n_windows windows of ${LOOKBACK_YEARS}y each, $DATA_START through today"
echo "$windows" | sed 's/^/  /'

if [ "$dry_run" = true ]; then
  exit 0
fi

mkdir -p logs output
LOG="logs/quarterly_soxl_sweep_${LOOKBACK_YEARS}y_$(date +%Y%m%d_%H%M%S).log"
SUMMARY_CSV="output/quarterly_soxl_summary_${LOOKBACK_YEARS}y_$(date +%Y%m%d_%H%M%S).csv"
echo "ticker,window_start,window_end,version,strategy,sweep_seconds,select_seconds" > "$SUMMARY_CSV"
echo "Logging to $LOG, summary to $SUMMARY_CSV"

cp config.json config.json.bak
trap 'cp config.json.bak config.json; echo "config restored"' EXIT

campaign_start=$(date +%s)

{
  echo "======================================================"
  echo " Quarterly SOXL campaign start — $(date)"
  echo " $n_windows windows"
  echo "======================================================"

  while IFS=',' read -r w_start w_end; do
    [ -z "$w_start" ] && continue
    # final_version is what actually lands in backtest_cache -- run_optimization_sweep.py
    # appends window_version_suffix(w_start, w_end) onto BASE_VERSION itself when
    # --start-date/--end-date are given, so BASE_VERSION (not final_version) is what gets
    # passed as --version below. Passing an already-suffixed version here would double it
    # (the 2026-08-16 bug this fix closes).
    final_version="${BASE_VERSION}-w${w_start}_${w_end}"
    echo ""
    echo "=== window [$w_start, $w_end] -> version=$final_version — $(date) ==="

    if [ "$skip_sweep" = false ]; then
      for strategy in $STRATEGIES; do
        entry_timings=$($PYTHON scripts/campaign_config.py entry_timings "$strategy")
        for et in $entry_timings; do
          sweep_t0=$(date +%s)
          $PYTHON scripts/campaign_config.py patch "$strategy" "$FIXED_SL"
          $PYTHON run_optimization_sweep.py --version "$BASE_VERSION" --entry-timing "$et" --max-phase 2.5 \
              --tickers "$TICKER" --start-date "$w_start" --end-date "$w_end" --skip-cache-refresh
          sweep_t1=$(date +%s)
          echo "$TICKER,$w_start,$w_end,$final_version,$strategy,$((sweep_t1 - sweep_t0))," >> "$SUMMARY_CSV"
        done
      done
    fi

    select_t0=$(date +%s)
    $PYTHON scripts/candidate_full_review.py "$TICKER" --version "$final_version" \
        --skip-5min --skip-overlay --skip-walkforward --skip-trend --skip-splits --skip-bear-market \
        --csv "output/quarterly_soxl_${LOOKBACK_YEARS}y_${w_start}_${w_end}_candidates.csv" || \
      echo "  (no candidates found/review failed for $final_version -- continuing)"
    select_t1=$(date +%s)
    echo "  candidate selection: $((select_t1 - select_t0))s"

  done <<< "$windows"

  echo ""
  echo "Rebuilding indexes + final cache refresh..."
  $PYTHON -c "
from run_optimization_sweep import rebuild_indexes
rebuild_indexes()
"

  campaign_end=$(date +%s)
  echo ""
  echo "======================================================"
  echo " Campaign done — $(date). Total: $((campaign_end - campaign_start))s"
  echo " Summary: $SUMMARY_CSV"
  echo "======================================================"

} 2>&1 | tee "$LOG"
