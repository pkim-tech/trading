#!/bin/bash
# One-off: resweep GDXD's TrailingBothZScoreBreakout campaign (SL=1%, both
# entry_timings) with the corrected kernel (2026-07-19 entry-side +
# 2026-07-20 exit-side gap-through-trigger fixes), so the v5 GDXD test
# (TrailingExitZScoreBreakout, run_v5_gdxd_test.sh) has a fair, same-kernel
# number to compare against.
#
# Writes to version='v5', NOT 'v4' (changed 2026-07-20) -- backtest_cache's PK
# already includes `strategy`, so TrailingBothZScoreBreakout and
# TrailingExitZScoreBreakout rows never collide regardless of version label;
# there was no need to split them across v4/v5 in the first place. Overwriting
# v4 in place (the original plan) would have thrown away the ability to
# compare pre-fix vs post-fix numbers -- exactly what "don't delete data,
# versioning exists for coexistence" exists to prevent. v5 now means "reswept
# with today's corrected kernel" for both strategies; v4 stays untouched as
# the historical pre-fix baseline for every ticker not yet touched. (GDXD's
# old v4/TrailingBoth rows were already deleted earlier this session before
# this renaming decision -- can't be restored, but the numbers are preserved
# in the session conversation.)
#
# trail_buy_pct grid narrowed to 1-3% (2026-07-20, user call) instead of the
# full run_v4_backfill_sweep.sh COMBINED range — the existing best node on file
# already has trail_buy_pct=1.0, so 1-3% covers its neighborhood without the
# full sweep's cost.
#
# Usage: ./scripts/run_v4_gdxd_resweep_compare.sh [--skip-cache-refresh]

set -e
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
COMBINED="[1,2,3,4,5,6,9,12,15,18,21,24,27,30]"
TB_GRID="[1,2,3]"
TRAIL_PCTS="[1,2,3,4,5,6,7]"

cp config.json config.json.bak
trap 'cp config.json.bak config.json; echo "config restored"' EXIT

patch_config() {
$PYTHON - <<EOF
import json
with open('config.json') as f:
    c = json.load(f)
c['active_strategies'] = ['TrailingBothZScoreBreakout']
c['target_tickers'] = ['GDXD']
c['hyperparameters']['take_profits'] = $COMBINED
c['hyperparameters']['stop_losses']  = $TB_GRID
c['hyperparameters']['trail_pcts']   = $TRAIL_PCTS
c['execution']['max_generations'] = 3
c['execution']['fixed_stop_loss'] = 1
with open('config.json', 'w') as f:
    json.dump(c, f, indent=4)
print("Patched config for v4 GDXD resweep (fixed_sl=1%, trail_buy_pct grid=1-3%)")
EOF
}

refresh_flag=""
[ "$1" = "--skip-cache-refresh" ] && refresh_flag="--skip-cache-refresh"

for et in open_check close; do
    echo ""
    echo "=== v5 resweep (TrailingBoth) — GDXD entry_timing=$et ==="
    patch_config
    $PYTHON run_optimization_sweep.py --version v5 --entry-timing "$et" --max-phase 2.5 \
        --tickers GDXD --skip-cache-refresh
done

if [ "$refresh_flag" != "--skip-cache-refresh" ]; then
    echo ""
    echo "Rebuilding indexes + final cache refresh..."
    $PYTHON -c "
from run_optimization_sweep import rebuild_indexes
from db_cache import refresh_dropdown_cache, refresh_pivot_cache, refresh_cliff_grid_cache
rebuild_indexes()
refresh_dropdown_cache()
refresh_pivot_cache(versions=['v5'])
refresh_cliff_grid_cache()
"
fi

echo ""
echo "All done — $(date)"
