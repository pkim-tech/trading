#!/bin/bash
# v3.x — backtest_cache reparameterization (2026-07-05): stop_loss now always means
# real SL; trail_buy_pct/trail_pct get real named columns instead of overloading
# stop_loss. trail_pct is now a genuine 4th swept grid axis for
# TrailingBothZScoreBreakout (hyperparameters.trail_pcts), replacing the old
# v2.13-v2.17 one-full-backfill-per-trail_pct-value pattern with a single run.
# v1.x/v2.x data is left completely untouched — see docs/backlog.md and
# /home/pkim/.claude/plans/ancient-giggling-kettle.md for the full design.
#
# Usage:
#   ./scripts/run_v3_backfill_sweep.sh v3.0              # full 53-ticker backfill
#   ./scripts/run_v3_backfill_sweep.sh v3.0 --validate    # small-subset validation run
#   ./scripts/run_v3_backfill_sweep.sh v3.0 TICKER ...    # explicit ticker override

set -e
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
TICKERS="SOXL TQQQ SQQQ SOXS TZA KORU SPXL TNA QLD SPXS SCO QID TECL AGQ UVXY LABU GDXU FNGU TMF UVIX UCO UDOW SDOW USD NAIL NUGT ZSL SVXY FAS TECS LABD DUST GDXD YINN DPST BULZ YANG FNGD DGP TMV FAZ TBT DFEN DRN ROM EDC HIBL WEBL CURE OILU RETL SHNY UTSL"
VALIDATE_TICKERS="AGQ EDC FAS HIBL SOXL"
COMBINED="[1,2,3,4,5,6,9,12,15,18,21,24,27,30]"
TRAIL_PCTS="[1,2,3,4,5]"

cp config.json config.json.bak
trap 'cp config.json.bak config.json; echo "config restored"' EXIT

patch_config() {
    local version=$1
    $PYTHON - <<EOF
import json
with open('config.json') as f:
    c = json.load(f)
c['active_strategies'] = ['TrailingBothZScoreBreakout']
c['hyperparameters']['take_profits']   = $COMBINED
c['hyperparameters']['stop_losses']    = $COMBINED
c['hyperparameters']['hold_time_caps'] = list(range(7, 147, 7))
c['hyperparameters']['trail_pcts']     = $TRAIL_PCTS
c['execution']['max_generations'] = 3
with open('config.json', 'w') as f:
    json.dump(c, f, indent=4)
print("Patched config for $version (TrailingBothZScoreBreakout, trail_pcts=$TRAIL_PCTS, sl_grid=$COMBINED)")
EOF
}

version="${1:-v3.0}"
shift || true

if [ "$1" = "--validate" ]; then
    tickers="$VALIDATE_TICKERS"
    echo "Validation run — small ticker subset: $tickers"
else
    tickers="${*:-$TICKERS}"
fi

echo ""
echo "======================================================"
echo " $version — $(date)"
echo "======================================================"
patch_config "$version"
$PYTHON run_optimization_sweep.py --version "$version" --tickers $tickers

echo ""
echo "Cache refresh..."
$PYTHON -c "
from db_cache import refresh_dropdown_cache, refresh_pivot_cache, refresh_cliff_grid_cache
refresh_dropdown_cache()
refresh_pivot_cache(versions=['$version'])
refresh_cliff_grid_cache()
"

echo ""
echo "All done — $(date)"
