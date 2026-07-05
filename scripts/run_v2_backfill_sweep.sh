#!/bin/bash
# Bias-corrected reindex of the full strategy sweep, one major version up from
# v1.x (see docs/backlog.md "Look-ahead bias..." — backtester.py:21 prep_inputs
# fix). Version stays tied to strategy, same mapping as v1.x, just v2.x.
# Scope: liquid (>= $50k max notional at 1% of 10d avg $ volume), non-crypto,
# index-underlier-only (excludes single-stock leveraged ETPs), non-dupe
# (excludes dupe_direxion copycats) — 53 tickers as of 2026-07-03.
# v1.x data is left untouched for before/after comparison.
# Usage: ./scripts/run_v2_backfill_sweep.sh [v2.4|...|v2.12|v2.13|v2.14|v2.15|v2.16|v2.17|v2.18] [TICKER ...]
#   v2.10 = TrailingBothZScoreBreakout, original run, untouched (trail_pct=3%, plain
#   coarse 3-30% sl-grid — trail_buy_pct never tested below 3% except where island/
#   full-mesh refinement happened to reach there).
#   v2.13/v2.14/v2.15/v2.16/v2.17 = TrailingBothZScoreBreakout, one full backfill per
#   trail_pct value (trailing-exit %), ascending: v2.13=1%, v2.14=2%, v2.15=3%,
#   v2.16=4%, v2.17=5% — trail_pct has no free grid axis on this strategy (sl axis =
#   trail_buy_pct), so testing each value means a full separate backfill. All 5 use the
#   COMBINED sl-grid (coarse 3-30% plus 1,2,4,5 filled in), so trail_buy_pct also gets
#   guaranteed low-end coverage on every ticker, not just the ones whose coarse=3% point
#   happened to earn island/full-mesh refinement in v2.10.
#   v2.18 = TrailingExitZScoreBreakout (same as v2.8), same COMBINED sl-grid (=trail_pct
#   for this strategy) instead of v2.8's plain coarse grid — same rationale, different
#   strategy family.
#   No version arg = run all versions in sequence (full ticker list).
#   Extra args after version = ticker override (e.g. a single-ticker sanity check),
#   still goes through the version->strategy patch_config guard below.

set -e
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
TICKERS="SOXL TQQQ SQQQ SOXS TZA KORU SPXL TNA QLD SPXS SCO QID TECL AGQ UVXY LABU GDXU FNGU TMF UVIX UCO UDOW SDOW USD NAIL NUGT ZSL SVXY FAS TECS LABD DUST GDXD YINN DPST BULZ YANG FNGD DGP TMV FAZ TBT DFEN DRN ROM EDC HIBL WEBL CURE OILU RETL SHNY UTSL"
COARSE="[3,6,9,12,15,18,21,24,27,30]"
# COARSE plus 1,2,4,5 filled in — keeps the full 3-30% range while guaranteeing every
# ticker also gets a coarse-level (Phase 1, ungated) test of the tight low end, instead
# of relying on Phase 2/3 refinement (which only reaches tickers that already passed
# Checkpoint 2) to have stumbled into it.
COMBINED="[1,2,3,4,5,6,9,12,15,18,21,24,27,30]"

cp config.json config.json.bak
trap 'cp config.json.bak config.json; echo "config restored"' EXIT

patch_config() {
    local version=$1 strategy=$2 hold_min=$3 trail_pct=${4:-3} sl_grid=${5:-$COARSE}
    $PYTHON - <<EOF
import json
with open('config.json') as f:
    c = json.load(f)
c['active_strategies'] = ['$strategy']
c['hyperparameters']['take_profits']   = $COARSE
c['hyperparameters']['stop_losses']    = $sl_grid
c['hyperparameters']['hold_time_caps'] = list(range($hold_min, 147, 7))
c['execution']['max_generations'] = 3
c['execution']['trail_pct'] = $trail_pct
with open('config.json', 'w') as f:
    json.dump(c, f, indent=4)
print("Patched config for $version ($strategy, hold>=${hold_min}h, trail_pct=${trail_pct}%, sl_grid=$sl_grid)")
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
        v2.4)  patch_config v2.4  TrendFilteredZScore          7 ;;
        v2.5)  patch_config v2.5  ZScoreBreakout               7 ;;
        v2.6)  patch_config v2.6  ZScoreBreakout               7 ;;
        v2.7)  patch_config v2.7  LimitOrderZScoreBreakout     7 ;;
        v2.8)  patch_config v2.8  TrailingExitZScoreBreakout   7 ;;
        v2.9)  patch_config v2.9  TrailingBuyZScoreBreakout    7 ;;
        v2.10) patch_config v2.10 TrailingBothZScoreBreakout   7 ;;
        v2.11) patch_config v2.11 LimitOrderTrailingExit       7 ;;
        v2.12) patch_config v2.12 LimitExitZScoreBreakout      7 ;;
        v2.13) patch_config v2.13 TrailingBothZScoreBreakout   7 1 "$COMBINED" ;;
        v2.14) patch_config v2.14 TrailingBothZScoreBreakout   7 2 "$COMBINED" ;;
        v2.15) patch_config v2.15 TrailingBothZScoreBreakout   7 3 "$COMBINED" ;;
        v2.16) patch_config v2.16 TrailingBothZScoreBreakout   7 4 "$COMBINED" ;;
        v2.17) patch_config v2.17 TrailingBothZScoreBreakout   7 5 "$COMBINED" ;;
        v2.18) patch_config v2.18 TrailingExitZScoreBreakout   7 3 "$COMBINED" ;;
        *) echo "Unknown version: $version"; exit 1 ;;
    esac
    local refresh_flag=""
    [ "$DEFER_CACHE_REFRESH" = "1" ] && refresh_flag="--skip-cache-refresh"
    $PYTHON run_optimization_sweep.py --version "$version" --tickers $tickers $refresh_flag
}

if [ -z "$1" ]; then
    DEFER_CACHE_REFRESH=1
    for v in v2.4 v2.5 v2.6 v2.7 v2.8 v2.9 v2.10 v2.11; do
        run_version "$v"
    done
    echo ""
    echo "Final cache refresh (all versions)..."
    $PYTHON -c "
from db_cache import refresh_dropdown_cache, refresh_pivot_cache, refresh_cliff_grid_cache
refresh_dropdown_cache()
refresh_pivot_cache(versions=['v2.4','v2.5','v2.6','v2.7','v2.8','v2.9','v2.10','v2.11'])
refresh_cliff_grid_cache()
"
else
    run_version "$@"
fi

echo ""
echo "All done — $(date)"
