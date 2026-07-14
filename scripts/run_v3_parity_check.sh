#!/bin/bash
# v3.x parity check (2026-07-05): AGQ-only regression comparing the reparameterized
# schema/dispatch code (docs/design.md "v3.x reparameterization", plan at
# /home/pkim/.claude/plans/ancient-giggling-kettle.md) against known-good v2.x cached
# results. Each version below is an EXACT copy of its v2.x counterpart's config
# (same strategy, same grid, same trail_pct) — only the version label and ticker
# list change. If the migration/dispatch refactor is correct, v3.x alpha/trades/
# win_rate should match v2.x's cached numbers for AGQ exactly.
#
#   v2.5  -> v3.5  : ZScoreBreakout (not overloaded at all — sanity check the
#                    migration didn't touch non-trailing strategies)
#   v2.10 -> v3.10 : TrailingBothZScoreBreakout, trail_pct=3% (fixed), coarse sl-grid
#                    — tests that the schema fix alone (real columns) doesn't change
#                    results vs. the old overloaded-stop_loss storage
#   v2.17 -> v3.17 : TrailingBothZScoreBreakout, trail_pct=5% (fixed), COMBINED sl-grid
#                    — same idea, at the other end of the v2.13-17 trail_pct range
#
# Usage: ./scripts/run_v3_parity_check.sh

set -e
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
TICKER="AGQ"
COARSE="[3,6,9,12,15,18,21,24,27,30]"
COMBINED="[1,2,3,4,5,6,9,12,15,18,21,24,27,30]"

cp config.json config.json.bak
trap 'cp config.json.bak config.json; echo "config restored"' EXIT

patch_config() {
    local version=$1 strategy=$2 trail_pct=${3:-3} sl_grid=${4:-$COARSE}
    $PYTHON - <<EOF
import json
with open('config.json') as f:
    c = json.load(f)
c['active_strategies'] = ['$strategy']
c['hyperparameters']['take_profits']   = $COARSE
c['hyperparameters']['stop_losses']    = $sl_grid
c['hyperparameters']['hold_time_caps'] = list(range(7, 147, 7))
c['hyperparameters'].pop('trail_pcts', None)
c['execution']['trail_pct'] = $trail_pct
c['execution']['max_generations'] = 3
with open('config.json', 'w') as f:
    json.dump(c, f, indent=4)
print("Patched config for $version ($strategy, trail_pct=${trail_pct}%, sl_grid=$sl_grid)")
EOF
}

run_one() {
    local version=$1 strategy=$2 trail_pct=$3 sl_grid=$4
    echo ""
    echo "======================================================"
    echo " $version — $(date)"
    echo "======================================================"
    patch_config "$version" "$strategy" "$trail_pct" "$sl_grid"
    $PYTHON run_optimization_sweep.py --version "$version" --tickers $TICKER --skip-cache-refresh
}

run_one v3.5  ZScoreBreakout             3 "$COARSE"
run_one v3.10 TrailingBothZScoreBreakout 3 "$COARSE"
run_one v3.17 TrailingBothZScoreBreakout 5 "$COMBINED"

echo ""
echo "======================================================"
echo " Parity check — v2.x vs v3.x best node for $TICKER"
echo "======================================================"
$PYTHON -c "
import sqlite3
c = sqlite3.connect('cache/research/trading_universe.db')
pairs = [('v2.5','v3.5','ZScoreBreakout'), ('v2.10','v3.10','TrailingBothZScoreBreakout'),
         ('v2.17','v3.17','TrailingBothZScoreBreakout')]
for old, new, strat in pairs:
    for v in (old, new):
        row = c.execute('''SELECT take_profit, stop_loss, fixed_sl, trail_buy_pct, trail_pct,
                                   trades, win_rate, strategy_return, alpha_vs_spy
                            FROM backtest_cache
                            WHERE version=? AND ticker='$TICKER' AND strategy=? AND trades > 0
                            ORDER BY alpha_vs_spy DESC LIMIT 1''', (v, strat)).fetchone()
        print(f'{v:>6}  tp={row[0]:>3} sl={row[1]:>3} fsl={row[2]:>5} tbp={row[3]:>5} tp%={row[4]:>5}  trades={row[5]:>4} win%={row[6]:.1f}  ret={row[7]:+.1f}%  alpha={row[8]:+.1f}%' if row else f'{v:>6}  NO DATA')
    print()
"

echo ""
echo "All done — $(date). Compare the alpha/trades/win% columns above per pair; they should match exactly."
