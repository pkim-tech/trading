#!/bin/bash
# Sweep all 26 qualified 3x index tickers across v1.4–v1.10.
# Cache dedup skips already-computed nodes automatically.
# Usage: ./scripts/run_new_tickers_sweep.sh [v1.4|v1.5|v1.6|v1.7|v1.8|v1.9|v1.10]
#   No arg = run all versions in sequence.

set -e
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
TICKERS="SOXL TQQQ KORU SPXL TNA TECL LABU GDXU FNGU TMF UDOW NAIL FAS YINN DPST BULZ DFEN DRN EDC HIBL WEBL CURE OILU RETL SHNY UTSL"
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
    echo ""
    echo "======================================================"
    echo " $version — $(date)"
    echo "======================================================"
    case "$version" in
        v1.4)  patch_config v1.4  TrendFilteredZScore          7 ;;
        v1.5)  patch_config v1.5  ZScoreBreakout               7 ;;
        v1.6)  patch_config v1.6  ZScoreBreakout               7 ;;
        v1.7)  patch_config v1.7  LimitOrderZScoreBreakout     7 ;;
        v1.8)  patch_config v1.8  TrailingExitZScoreBreakout   7 ;;
        v1.9)  patch_config v1.9  TrailingBuyZScoreBreakout    7 ;;
        v1.10) patch_config v1.10 TrailingBothZScoreBreakout   7 ;;
        *) echo "Unknown version: $version"; exit 1 ;;
    esac
    $PYTHON run_optimization_sweep.py --version "$version" --tickers $TICKERS
}

if [ -z "$1" ]; then
    # v1.9 preview for live watchlist tickers first
    echo ""
    echo "======================================================"
    echo " v1.9 preview (EDC FAS HIBL SOXL) — $(date)"
    echo "======================================================"
    patch_config v1.9 TrailingBuyZScoreBreakout 7
    $PYTHON run_optimization_sweep.py --version v1.9 --tickers EDC FAS HIBL SOXL

    for v in v1.10 v1.4 v1.5 v1.6 v1.7 v1.8 v1.9; do
        run_version "$v"
    done
else
    run_version "$1"
fi

echo ""
echo "All done — $(date)"
