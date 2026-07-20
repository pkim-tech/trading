#!/bin/bash
# v5 across the full 18-ticker watchlist: immediate bar-close entry (drop
# trail_buy_pct), existing trailing-stop exit kept as-is
# (TrailingExitZScoreBreakout, v1.8 kernel). Companion to
# run_v4_full18_resweep.sh (TrailingBothZScoreBreakout) for a fair,
# same-kernel comparison of whether the trailing-buy bounce wait is worth its
# cost, across the whole watchlist rather than just GDXD
# (run_v5_gdxd_test.sh). --max-phase 2.5: Phase 3 held the best node in 0/30
# tagged campaigns (2026-07-15 finding).
#
# fixed_sl=1% matches every ticker's real live SL. No open_check support in
# this kernel (run_backtest_v18 ignores entry_timing) -- close only, so this
# is NOT apples-to-apples with v4's open_check winner; use alongside
# run_v4_full18_resweep.sh's close-entry_timing campaign for the fair half of
# the comparison.
#
# No delete: dispatch_parallel_grid's own per-node cache lookup (keyed on the
# full param tuple) already skips any node that's genuinely been computed with
# the current (corrected) kernel and picks up wherever a prior run left off --
# same reasoning as run_v4_full18_resweep.sh. A blanket DELETE here would
# throw away real, already-correct v5 rows for no reason (don't delete data;
# versioning/per-node dedup exists so this isn't needed).
#
# Usage: ./scripts/run_v5_full18_test.sh [--skip-cache-refresh]

set -e
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
TICKERS="AGQ DPST DUST GDXD GDXU HIBL KORU LABU NAIL NUGT RETL SOXL TQQQ UDOW USD UVIX YANG ZSL"
COMBINED="[1,2,3,4,5,6,9,12,15,18,21,24,27,30]"
TRAIL_PCTS_GRID="[1,2,3,4,5,6,7]"

cp config.json config.json.bak
trap 'cp config.json.bak config.json; echo "config restored"' EXIT

$PYTHON - <<EOF
import json
with open('config.json') as f:
    c = json.load(f)
c['active_strategies'] = ['TrailingExitZScoreBreakout']
c['target_tickers'] = "$TICKERS".split()
c['hyperparameters']['take_profits'] = $COMBINED
c['hyperparameters']['stop_losses']  = $TRAIL_PCTS_GRID
c['execution']['max_generations'] = 3
c['execution']['fixed_stop_loss'] = 1
with open('config.json', 'w') as f:
    json.dump(c, f, indent=4)
print("Patched config for v5 full-18 test (fixed_sl=1%, trail_pct grid=1-7%)")
EOF

refresh_flag=""
[ "$1" = "--skip-cache-refresh" ] && refresh_flag="--skip-cache-refresh"

$PYTHON run_optimization_sweep.py --version v5 --entry-timing close --max-phase 2.5 \
    --tickers $TICKERS $refresh_flag

echo ""
echo "All done — $(date)"
