#!/bin/bash
# Sweep all 18 active watchlist tickers' TrailingBothZScoreBreakout campaign
# (SL=1%, both entry_timings) with the corrected kernel (2026-07-20 exit-side
# gap-through-trigger fix, on top of the 2026-07-19 entry-side fix).
# Companion to run_v5_full18_test.sh (TrailingExitZScoreBreakout) for a fair,
# same-kernel comparison across the whole watchlist, not just GDXD
# (run_v4_gdxd_resweep_compare.sh). --max-phase 2.5 throughout: Phase 3 held
# the best node in 0/30 tagged campaigns (2026-07-15 finding).
#
# Writes to version='v5', NOT 'v4' (changed 2026-07-20) -- backtest_cache's PK
# already includes `strategy`, so TrailingBothZScoreBreakout and
# TrailingExitZScoreBreakout rows never collide regardless of version label.
# Overwriting v4 in place (the original plan) would have thrown away the
# ability to compare pre-fix vs post-fix numbers -- exactly what "don't
# delete data, versioning exists for coexistence" exists to prevent. v5 now
# means "reswept with today's corrected kernel" for both strategies; v4 stays
# untouched as the historical pre-fix baseline.
#
# Tickers/params pulled from the real live watch_list (watchlist_id=57,
# 2026-07-20) -- all 18 run fixed_sl=1%, all but GDXU run trail_buy_pct=1%
# (GDXU uses 3%), so the full COMBINED grid (not narrowed like the GDXD-only
# test) covers every ticker's real neighborhood.
#
# No stale-cache deletion needed here (unlike the earlier v4-overwrite plan)
# since version='v5' is a fresh PK for these tickers/strategy -- nothing to
# collide with. Chain with run_v5_full18_test.sh via --skip-cache-refresh so
# only the true last invocation in the chain pays for the index rebuild +
# cache refresh:
#   ./scripts/run_v4_full18_resweep.sh --skip-cache-refresh && ./scripts/run_v5_full18_test.sh
#
# Usage: ./scripts/run_v4_full18_resweep.sh [--skip-cache-refresh]

set -e
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
TICKERS="AGQ DPST DUST GDXD GDXU HIBL KORU LABU NAIL NUGT RETL SOXL TQQQ UDOW USD UVIX YANG ZSL"
COMBINED="[1,2,3,4,5,6,9,12,15,18,21,24,27,30]"
TRAIL_PCTS="[1,2,3,4,5,6,7]"

cp config.json config.json.bak
trap 'cp config.json.bak config.json; echo "config restored"' EXIT

patch_config() {
$PYTHON - <<EOF
import json
with open('config.json') as f:
    c = json.load(f)
c['active_strategies'] = ['TrailingBothZScoreBreakout']
c['target_tickers'] = "$TICKERS".split()
c['hyperparameters']['take_profits'] = $COMBINED
c['hyperparameters']['stop_losses']  = $COMBINED
c['hyperparameters']['trail_pcts']   = $TRAIL_PCTS
c['execution']['max_generations'] = 3
c['execution']['fixed_stop_loss'] = 1
with open('config.json', 'w') as f:
    json.dump(c, f, indent=4)
print("Patched config for v5 full-18 resweep, TrailingBoth (fixed_sl=1%)")
EOF
}

for et in open_check close; do
    echo ""
    echo "=== v5 resweep (TrailingBoth) — entry_timing=$et ==="
    patch_config
    $PYTHON run_optimization_sweep.py --version v5 --entry-timing "$et" --max-phase 2.5 \
        --tickers $TICKERS --skip-cache-refresh
done

if [ "$1" != "--skip-cache-refresh" ]; then
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
