#!/bin/bash
# v3.x — backtest_cache reparameterization (2026-07-05): stop_loss now always means
# real SL; trail_buy_pct/trail_pct get real named columns instead of overloading
# stop_loss. trail_pct has a real named column, but per-value versions are kept
# separate here (like v2.13-17) rather than swept as a single combined 4th-axis run.
# v1.x/v2.x data is left completely untouched — see docs/backlog.md and
# /home/pkim/.claude/plans/ancient-giggling-kettle.md for the full design.
#
# Version <-> strategy mapping (same convention as run_v2_backfill_sweep.sh, resorted):
#   v3.4  SKIPPED — TrendFilteredZScore (50sma filter, not carried into v3.x)
#   v3.5  ZScoreBreakout
#   v3.6  ZScoreBreakout
#   v3.7  SKIPPED — LimitOrderZScoreBreakout (limit-order family)
#   v3.8  SKIPPED — TrailingExitZScoreBreakout coarse grid, redundant with v3.18's
#         combined grid (superset)
#   v3.9  TrailingBuyZScoreBreakout
#   v3.10 SKIPPED — was going to be TrailingBothZScoreBreakout with all of trail_pct
#         1-7 combined into one run; dropped in favor of the v3.21-27 per-value split
#         below (mirrors the old v2.13-17 pattern, just extended to 7 values)
#   v3.11 SKIPPED — LimitOrderTrailingExit (limit-order family)
#   v3.12 SKIPPED — LimitExitZScoreBreakout (limit-order family)
#   v3.13-17 unused (gap, kept clean/reserved)
#   v3.18 TrailingExitZScoreBreakout (was v2.18, resorted here)
#   v3.19-20 unused (gap, kept clean/reserved)
#   v3.21 TrailingBothZScoreBreakout, trail_pct=1%
#   v3.22 TrailingBothZScoreBreakout, trail_pct=2%
#   v3.23 TrailingBothZScoreBreakout, trail_pct=3%
#   v3.24 TrailingBothZScoreBreakout, trail_pct=4%
#   v3.25 TrailingBothZScoreBreakout, trail_pct=5%
#   v3.26 TrailingBothZScoreBreakout, trail_pct=6%
#   v3.27 TrailingBothZScoreBreakout, trail_pct=7%
#   v3.28+ reserved for future trailing-stop strategy variants
#
# Every version uses the same combined tp/sl grid (COMBINED below, coarse 3-30 plus
# 1,2,4,5 low-end points) — the coarse-only grid was dropped for simplicity after
# confirming several current watchlist winners sit at those low-end points (see
# docs/design.md "Version Changelog").
#
# Scope: Sweep 3's 11 tickers only (AGQ/DPST/EDC/GDXU/HIBL/KORU/UVIX/YANG/NUGT/SOXL/TQQQ),
# not the full 53-ticker universe.
#
# Usage:
#   ./scripts/run_v3_backfill_sweep.sh                    # run all included versions in sequence
#   ./scripts/run_v3_backfill_sweep.sh v3.21              # single version
#   ./scripts/run_v3_backfill_sweep.sh v3.21 --validate   # small-subset validation run
#   ./scripts/run_v3_backfill_sweep.sh v3.21 TICKER ...   # explicit ticker override
#   Add --skip-cache-refresh anywhere to skip both the sweep's internal cache
#   refresh and this script's post-run dropdown/pivot/cliff cache refresh.

set -e
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
TICKERS="AGQ DPST EDC GDXU HIBL KORU UVIX YANG NUGT SOXL TQQQ"
VALIDATE_TICKERS="AGQ EDC HIBL SOXL"
COMBINED="[1,2,3,4,5,6,9,12,15,18,21,24,27,30]"

cp config.json config.json.bak
trap 'cp config.json.bak config.json; echo "config restored"' EXIT

patch_config() {
    local version=$1 strategy=$2 hold_min=$3 trail_pcts=$4
    $PYTHON - <<EOF
import json
with open('config.json') as f:
    c = json.load(f)
c['active_strategies'] = ['$strategy']
c['hyperparameters']['take_profits']   = $COMBINED
c['hyperparameters']['stop_losses']    = $COMBINED
c['hyperparameters']['hold_time_caps'] = list(range($hold_min, 147, 7))
c['hyperparameters']['trail_pcts']     = ${trail_pcts:-[3]}
c['execution']['max_generations'] = 3
with open('config.json', 'w') as f:
    json.dump(c, f, indent=4)
print("Patched config for $version ($strategy, hold>=${hold_min}h, trail_pcts=${trail_pcts:-n/a})")
EOF
}

run_version() {
    local version=$1
    shift
    local tickers="${*:-$TICKERS}"
    echo ""
    echo "======================================================"
    echo " $version — $(date)"
    echo "======================================================"
    case "$version" in
        v3.5)  patch_config v3.5  ZScoreBreakout             7 ;;
        v3.6)  patch_config v3.6  ZScoreBreakout             7 ;;
        v3.9)  patch_config v3.9  TrailingBuyZScoreBreakout  7 ;;
        v3.18) patch_config v3.18 TrailingExitZScoreBreakout 7 ;;
        v3.21) patch_config v3.21 TrailingBothZScoreBreakout 7 "[1]" ;;
        v3.22) patch_config v3.22 TrailingBothZScoreBreakout 7 "[2]" ;;
        v3.23) patch_config v3.23 TrailingBothZScoreBreakout 7 "[3]" ;;
        v3.24) patch_config v3.24 TrailingBothZScoreBreakout 7 "[4]" ;;
        v3.25) patch_config v3.25 TrailingBothZScoreBreakout 7 "[5]" ;;
        v3.26) patch_config v3.26 TrailingBothZScoreBreakout 7 "[6]" ;;
        v3.27) patch_config v3.27 TrailingBothZScoreBreakout 7 "[7]" ;;
        *) echo "Unknown or excluded version: $version"; exit 1 ;;
    esac
    local refresh_flag=""
    [ "$DEFER_CACHE_REFRESH" = "1" ] && refresh_flag="--skip-cache-refresh"
    $PYTHON run_optimization_sweep.py --version "$version" --tickers $tickers $refresh_flag
}

version="$1"
shift || true

skip_cache_refresh=false
args=()
for arg in "$@"; do
    if [ "$arg" = "--skip-cache-refresh" ]; then
        skip_cache_refresh=true
    else
        args+=("$arg")
    fi
done
set -- "${args[@]}"

if [ "$1" = "--validate" ]; then
    tickers="$VALIDATE_TICKERS"
    echo "Validation run — small ticker subset: $tickers"
    shift
else
    tickers="$*"
fi

if [ -z "$version" ]; then
    DEFER_CACHE_REFRESH=1
    for v in v3.5 v3.6 v3.9 v3.18 v3.21 v3.22 v3.23 v3.24 v3.25 v3.26 v3.27; do
        run_version "$v" $tickers
    done
    if [ "$skip_cache_refresh" = true ]; then
        echo ""
        echo "Skipping final cache refresh (--skip-cache-refresh)."
    else
        echo ""
        echo "Final cache refresh (all versions)..."
        $PYTHON -c "
from db_cache import refresh_dropdown_cache, refresh_pivot_cache, refresh_cliff_grid_cache
refresh_dropdown_cache()
refresh_pivot_cache(versions=['v3.5','v3.6','v3.9','v3.18','v3.21','v3.22','v3.23','v3.24','v3.25','v3.26','v3.27'])
refresh_cliff_grid_cache()
"
    fi
else
    DEFER_CACHE_REFRESH=1
    run_version "$version" $tickers
    if [ "$skip_cache_refresh" = true ]; then
        echo ""
        echo "Skipping cache refresh (--skip-cache-refresh)."
    else
        echo ""
        echo "Cache refresh..."
        $PYTHON -c "
from db_cache import refresh_dropdown_cache, refresh_pivot_cache, refresh_cliff_grid_cache
refresh_dropdown_cache()
refresh_pivot_cache(versions=['$version'])
refresh_cliff_grid_cache()
"
    fi
fi

echo ""
echo "All done — $(date)"
