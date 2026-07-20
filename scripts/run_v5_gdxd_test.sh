#!/bin/bash
# v5 — GDXD-only test: immediate bar-close entry (drop trail_buy_pct) with the
# existing trailing-stop exit kept as-is (TrailingExitZScoreBreakout, v1.8
# kernel). Compares against GDXD's live TrailingBothZScoreBreakout (v4) node to
# see whether skipping the trailing-buy bounce wait actually costs edge, raised
# 2026-07-20 while discussing the gap-through-trigger fallout. See
# docs/backlog_cache.md.
#
# TrailingExitZScoreBreakout's sl_axis is trail_pct (the exit trailing-stop %,
# not an entry trail) — the swept 'stop_losses' grid value becomes trail_pct
# here, matching v4's TRAIL_PCTS range (1-7%). fixed_sl=1 matches GDXD's real
# live SL. No open_check support in this kernel (run_backtest_v18 ignores
# entry_timing) -- close only.
#
# Usage: ./scripts/run_v5_gdxd_test.sh [--skip-cache-refresh]

set -e
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"

cp config.json config.json.bak
trap 'cp config.json.bak config.json; echo "config restored"' EXIT

$PYTHON - <<'EOF'
import json
with open('config.json') as f:
    c = json.load(f)
c['active_strategies'] = ['TrailingExitZScoreBreakout']
c['target_tickers'] = ['GDXD']
c['hyperparameters']['stop_losses'] = [1, 2, 3, 4, 5, 6, 7]
c['execution']['max_generations'] = 3
c['execution']['fixed_stop_loss'] = 1
with open('config.json', 'w') as f:
    json.dump(c, f, indent=4)
print("Patched config for v5 GDXD test (fixed_sl=1%, trail_pct grid=1-7%)")
EOF

refresh_flag=""
[ "$1" = "--skip-cache-refresh" ] && refresh_flag="--skip-cache-refresh"

$PYTHON run_optimization_sweep.py --version v5 --entry-timing close --max-phase 2.5 \
    --tickers GDXD $refresh_flag

echo ""
echo "All done — $(date)"
