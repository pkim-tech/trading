#!/bin/bash
# v4 — fill-optimism resolution bounds + fixed_sl sweep + Open-check entry timing
# (2026-07-14). See docs/backlog_cache.md fill-optimism item and the full plan at
# /home/pkim/.claude/plans/rustling-bubbling-hennessy.md.
#
# _simulate_trail_both now computes three parallel bounce-fill resolutions per
# node (possible/pessimistic/certain — see CLAUDE.md Key Files or the kernel's own
# docstring), since no OHLC-only method proves the true intrabar path. Island
# search and cliff-safety rank/filter on MIN(possible, pessimistic, certain), not
# 'possible' alone.
#
# Unlike v3.x (one version string per campaign), every v4 campaign writes to the
# SAME version 'v4' — the real backtest_cache columns (stop_loss=fixed_sl for
# TrailingBothZScoreBreakout, entry_timing) disambiguate campaigns instead of the
# version string. trail_sell_pct is swept normally *within* each campaign (already
# a real per-run axis since the 2026-07-05 v3.x reparameterization — not
# campaign-split like stop_loss/entry_timing have to be, see docs/design.md:93-96).
#
# 10 stop_loss values (config.hyperparameters.stop_losses, reused) x 2
# entry_timing values (close, open_check) = 20 campaigns, each a clean 3-axis
# (axis_tp, trail_buy_pct, hold_time) island search plus the trail_sell_pct axis
# swept inside it — see docs/design.md's 3-axis island cap for why stop_loss and
# entry_timing can't just be added as real 4th/5th grid axes instead.
#
# Scope: the 11 live-watchlist tickers only (watchlist_id=9), not the full
# 53-ticker universe — TrailingBothZScoreBreakout only.
#
# Usage:
#   ./scripts/run_v4_backfill_sweep.sh                      # all 20 campaigns, live tickers
#   ./scripts/run_v4_backfill_sweep.sh 9 close               # single (stop_loss, entry_timing) combo
#   ./scripts/run_v4_backfill_sweep.sh 9 close --validate     # single combo, SOXL only (sanity check)
#   ./scripts/run_v4_backfill_sweep.sh 9 close SOXL           # single combo, explicit ticker(s)
#   Add --skip-cache-refresh anywhere to skip both the sweep's internal cache
#   refresh and this script's post-run dropdown/pivot/cliff cache refresh.

set -e
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
TICKERS="AGQ DPST EDC GDXU HIBL KORU LABU NUGT SOXL TQQQ YANG"
VALIDATE_TICKERS="SOXL"
STOP_LOSSES="1 2 3 4 5 6 9 12 15 18 21 24 27 30"
ENTRY_TIMINGS="close open_check"
COMBINED="[1,2,3,4,5,6,9,12,15,18,21,24,27,30]"
TRAIL_PCTS="[1,2,3,4,5,6,7]"

cp config.json config.json.bak
trap 'cp config.json.bak config.json; echo "config restored"' EXIT

patch_config() {
    local stop_loss=$1
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
c['execution']['fixed_stop_loss'] = $stop_loss
with open('config.json', 'w') as f:
    json.dump(c, f, indent=4)
print("Patched config for v4 (fixed_sl=${stop_loss}%)")
EOF
}

run_campaign() {
    local stop_loss=$1 entry_timing=$2
    shift 2
    local tickers="${*:-$TICKERS}"
    echo ""
    echo "======================================================"
    echo " v4 — stop_loss=${stop_loss}% entry_timing=${entry_timing} — $(date)"
    echo "======================================================"
    patch_config "$stop_loss"
    local refresh_flag=""
    [ "$DEFER_CACHE_REFRESH" = "1" ] && refresh_flag="--skip-cache-refresh"
    local max_phase_flag=""
    [ -n "$MAX_PHASE" ] && max_phase_flag="--max-phase $MAX_PHASE"
    $PYTHON run_optimization_sweep.py --version v4 --entry-timing "$entry_timing" $max_phase_flag \
        --tickers $tickers $refresh_flag
}

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

stop_loss="$1"
entry_timing="$2"
shift 2 2>/dev/null || true

if [ "$1" = "--validate" ]; then
    tickers="$VALIDATE_TICKERS"
    echo "Validation run — SOXL only: $tickers"
    shift
elif [ "$1" = "ALL53" ]; then
    echo "ALL53 not supported for v4 — live-watchlist scope only." >&2
    exit 1
else
    tickers="$*"
fi

if [ -z "$stop_loss" ]; then
    DEFER_CACHE_REFRESH=1
    for sl in $STOP_LOSSES; do
        for et in $ENTRY_TIMINGS; do
            run_campaign "$sl" "$et" $tickers
        done
    done
    if [ "$skip_cache_refresh" = true ]; then
        echo ""
        echo "Skipping final cache refresh (--skip-cache-refresh)."
    else
        echo ""
        echo "Final cache refresh..."
        $PYTHON -c "
from db_cache import refresh_dropdown_cache, refresh_pivot_cache, refresh_cliff_grid_cache
refresh_dropdown_cache()
refresh_pivot_cache(versions=['v4'])
refresh_cliff_grid_cache()
"
    fi
else
    if [ -z "$entry_timing" ]; then
        echo "Usage: $0 <stop_loss> <close|open_check> [tickers... | --validate]" >&2
        exit 1
    fi
    DEFER_CACHE_REFRESH=1
    run_campaign "$stop_loss" "$entry_timing" $tickers
    if [ "$skip_cache_refresh" = true ]; then
        echo ""
        echo "Skipping cache refresh (--skip-cache-refresh)."
    else
        echo ""
        echo "Cache refresh..."
        $PYTHON -c "
from db_cache import refresh_dropdown_cache, refresh_pivot_cache, refresh_cliff_grid_cache
refresh_dropdown_cache()
refresh_pivot_cache(versions=['v4'])
refresh_cliff_grid_cache()
"
    fi
fi

echo ""
echo "All done — $(date)"
