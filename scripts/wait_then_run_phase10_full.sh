#!/bin/bash
# Waits for the currently-running SOXL bench_phase1_phase2_inmemory.py sweep to finish,
# then launches the FULL Phase10 candidate report (--full-review-population core_safe,
# all 14 v6.5.1 tickers) -- scoped to the entire core_safe population per ticker, not just
# the curated top-N, so we can see "everything that beats current live," not a fixed
# shortlist. Estimated a few hours (~10,279 core_safe candidates across 14 tickers at
# ~11-13s/candidate, workers=budget). Detached from the launching shell/session via
# nohup+disown so it survives session end -- this is a genuine background OS process,
# not a Claude Code backgrounded tool call.
#
# Usage: nohup ./scripts/wait_then_run_phase10_full.sh > logs/phase10_full_wait.log 2>&1 &
#        disown

set -uo pipefail
cd "$(dirname "$0")/.."

VERSION="v6.5.1-bench-inmemory-v6-massive-w2021-08-23_2026-08-21-z0.5-1.0-1.5-2.0-isl3-pv4"
OUT_XLSX="candidate_full_review_v6.5.1_full_core_safe.xlsx"
LOG="logs/phase10_full_$(date +%Y%m%d_%H%M%S).log"
WORKERS=11  # campaign_registry.get_workers_budget for this version, confirmed 2026-09-04

echo "[$(date)] Waiting for SOXL sweep (bench_phase1_phase2_inmemory.py --ticker SOXL) to finish..."
while pgrep -f "bench_phase1_phase2_inmemory.py.*--ticker SOXL" > /dev/null; do
    sleep 60
done
echo "[$(date)] SOXL sweep finished. Launching full Phase10 core_safe population report."

.venv/bin/python scripts/candidate_full_review_two_tab.py \
    --version "$VERSION" \
    --top-n 8 --workers "$WORKERS" \
    --full-review-population core_safe \
    --xlsx "$OUT_XLSX" \
    > "$LOG" 2>&1

echo "[$(date)] Full Phase10 report done. See output/$OUT_XLSX and $LOG"
