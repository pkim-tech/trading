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
#
# Sparse-then-fill extension (2026-07-05, evening): v3.18/NUGT/SOXL/TQQQ showed
# TrailingExitZScoreBreakout doing much better at wide trail_pct (9-24%) than
# TrailingBoth's tested 1-7% range — never checked whether TrailingBoth's
# bounce-entry tickers also improve past 7%. All single-percent versions 8-30%
# are wired below (version = trail_pct% + 20, so nothing ever needs renumbering).
# Priority tonight: run the sparse set first (9,12,15,18,21,24,27,30 — v3.29,
# v3.32, v3.35, v3.38, v3.41, v3.44, v3.47, v3.50), then gap-fill the immediate
# neighbors of whichever value comes out best per ticker (e.g. if 9% wins, run
# v3.28/v3.30/v3.31 next to refine toward the true local optimum) —
# see scripts/fill_trail_pct_gaps.py once the sparse run has data to work with.
#   v3.21-27: trail_pct=1-7% (already run)
#   v3.28-50: trail_pct=8-30%, one version per percent (v3.2X = X% for X<=27,
#             v3.2X where X=trail_pct+20 otherwise — see case statement below)
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
#   ./scripts/run_v3_backfill_sweep.sh v3.29              # single version
#   ./scripts/run_v3_backfill_sweep.sh v3.29 --validate   # small-subset validation run
#   ./scripts/run_v3_backfill_sweep.sh v3.29 TICKER ...   # explicit ticker override
#   ./scripts/run_v3_backfill_sweep.sh v3.29 ALL53        # full 53-ticker universe
#   Add --skip-cache-refresh anywhere to skip both the sweep's internal cache
#   refresh and this script's post-run dropdown/pivot/cliff cache refresh.
#   To run just the sparse trail_pct extension tonight (skipping the already-done
#   1-7% versions): for v in v3.29 v3.32 v3.35 v3.38 v3.41 v3.44 v3.47 v3.50; do
#   ./scripts/run_v3_backfill_sweep.sh $v; done

set -e
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
TICKERS="AGQ DPST EDC GDXU HIBL KORU UVIX YANG NUGT SOXL TQQQ"
VALIDATE_TICKERS="AGQ EDC HIBL SOXL"
# Full 53-ticker universe (same list as run_v2_backfill_sweep.sh's TICKERS) — pass
# the literal token ALL53 as the ticker arg to expand to this instead of typing it out.
ALL53_TICKERS="SOXL TQQQ SQQQ SOXS TZA KORU SPXL TNA QLD SPXS SCO QID TECL AGQ UVXY LABU GDXU FNGU TMF UVIX UCO UDOW SDOW USD NAIL NUGT ZSL SVXY FAS TECS LABD DUST GDXD YINN DPST BULZ YANG FNGD DGP TMV FAZ TBT DFEN DRN ROM EDC HIBL WEBL CURE OILU RETL SHNY UTSL"
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
        v3.28) patch_config v3.28 TrailingBothZScoreBreakout 7 "[8]"  ;;
        v3.29) patch_config v3.29 TrailingBothZScoreBreakout 7 "[9]"  ;;
        v3.30) patch_config v3.30 TrailingBothZScoreBreakout 7 "[10]" ;;
        v3.31) patch_config v3.31 TrailingBothZScoreBreakout 7 "[11]" ;;
        v3.32) patch_config v3.32 TrailingBothZScoreBreakout 7 "[12]" ;;
        v3.33) patch_config v3.33 TrailingBothZScoreBreakout 7 "[13]" ;;
        v3.34) patch_config v3.34 TrailingBothZScoreBreakout 7 "[14]" ;;
        v3.35) patch_config v3.35 TrailingBothZScoreBreakout 7 "[15]" ;;
        v3.36) patch_config v3.36 TrailingBothZScoreBreakout 7 "[16]" ;;
        v3.37) patch_config v3.37 TrailingBothZScoreBreakout 7 "[17]" ;;
        v3.38) patch_config v3.38 TrailingBothZScoreBreakout 7 "[18]" ;;
        v3.39) patch_config v3.39 TrailingBothZScoreBreakout 7 "[19]" ;;
        v3.40) patch_config v3.40 TrailingBothZScoreBreakout 7 "[20]" ;;
        v3.41) patch_config v3.41 TrailingBothZScoreBreakout 7 "[21]" ;;
        v3.42) patch_config v3.42 TrailingBothZScoreBreakout 7 "[22]" ;;
        v3.43) patch_config v3.43 TrailingBothZScoreBreakout 7 "[23]" ;;
        v3.44) patch_config v3.44 TrailingBothZScoreBreakout 7 "[24]" ;;
        v3.45) patch_config v3.45 TrailingBothZScoreBreakout 7 "[25]" ;;
        v3.46) patch_config v3.46 TrailingBothZScoreBreakout 7 "[26]" ;;
        v3.47) patch_config v3.47 TrailingBothZScoreBreakout 7 "[27]" ;;
        v3.48) patch_config v3.48 TrailingBothZScoreBreakout 7 "[28]" ;;
        v3.49) patch_config v3.49 TrailingBothZScoreBreakout 7 "[29]" ;;
        v3.50) patch_config v3.50 TrailingBothZScoreBreakout 7 "[30]" ;;
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
elif [ "$1" = "ALL53" ]; then
    tickers="$ALL53_TICKERS"
    echo "Full 53-ticker universe run: $tickers"
else
    tickers="$*"
fi

if [ -z "$version" ]; then
    DEFER_CACHE_REFRESH=1
    for v in v3.5 v3.6 v3.9 v3.18 v3.21 v3.22 v3.23 v3.24 v3.25 v3.26 v3.27 \
             v3.29 v3.32 v3.35 v3.38 v3.41 v3.44 v3.47 v3.50; do
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
refresh_pivot_cache(versions=['v3.5','v3.6','v3.9','v3.18','v3.21','v3.22','v3.23','v3.24','v3.25','v3.26','v3.27',
                              'v3.29','v3.32','v3.35','v3.38','v3.41','v3.44','v3.47','v3.50'])
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
