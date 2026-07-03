#!/bin/bash
# Bias-corrected reindex of the full strategy sweep, one major version up from
# v1.x (see docs/backlog.md "Look-ahead bias..." — backtester.py:21 prep_inputs
# fix). Version stays tied to strategy, same mapping as v1.x, just v2.x.
# Scope: liquid (>= $50k max notional at 1% of 10d avg $ volume), non-crypto,
# index-underlier-only (excludes single-stock leveraged ETPs), non-dupe
# (excludes dupe_direxion copycats) — 53 tickers as of 2026-07-03.
# v1.x data is left untouched for before/after comparison.
# Usage: ./scripts/run_v2_backfill_sweep.sh [v2.4|v2.5|v2.6|v2.7|v2.8|v2.9|v2.10] [TICKER ...]
#   No version arg = run all versions in sequence (full ticker list).
#   Extra args after version = ticker override (e.g. a single-ticker sanity check),
#   still goes through the version->strategy patch_config guard below.

set -e
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
TICKERS="SOXL TQQQ SQQQ SOXS TZA KORU SPXL TNA QLD SPXS SCO QID TECL AGQ UVXY LABU GDXU FNGU TMF UVIX UCO UDOW SDOW USD NAIL NUGT ZSL SVXY FAS TECS LABD DUST GDXD YINN DPST BULZ YANG FNGD DGP TMV FAZ TBT DFEN DRN ROM EDC HIBL WEBL CURE OILU RETL SHNY UTSL"
COARSE="[3,6,9,12,15,18,21,24,27,30]"

cp config.json config.json.bak
trap 'cp config.json.bak config.json; echo "config restored"' EXIT

patch_config() {
    local version=$1 strategy=$2 hold_min=$3
    $PYTHON - <<EOF
import json
with open('config.json') as f:
    c = json.load(f)
c['active_strategies'] = ['$strategy']
c['hyperparameters']['take_profits']   = $COARSE
c['hyperparameters']['stop_losses']    = $COARSE
c['hyperparameters']['hold_time_caps'] = list(range($hold_min, 147, 7))
c['execution']['max_generations'] = 3
with open('config.json', 'w') as f:
    json.dump(c, f, indent=4)
print("Patched config for $version ($strategy, hold>=${hold_min}h)")
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
        *) echo "Unknown version: $version"; exit 1 ;;
    esac
    $PYTHON run_optimization_sweep.py --version "$version" --tickers $tickers
}

if [ -z "$1" ]; then
    for v in v2.4 v2.5 v2.6 v2.7 v2.8 v2.9 v2.10; do
        run_version "$v"
    done
else
    run_version "$@"
fi

echo ""
echo "All done — $(date)"
