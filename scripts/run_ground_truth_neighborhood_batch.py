"""Runs scripts/run_ground_truth_neighborhood.py's check sequentially across all real
Tranche-1 live tickers except SOXL (which already has both this neighborhood check AND
a full Phase1-coarse sweep running separately). 1 worker only (per the explicit 3-way
concurrent worker budget: 8 for SOXL's Phase1-coarse, 1 for Phase2/2.5 dev, 1 for this).

Ticker list: sim_minute_groundtruth_independent.LIVE_NODE_IDS (the 12 real state='live'
nodes this whole plan has used throughout, see the Step 2a parity test) minus SOXL (id=92)
-- DPST, KORU, JNUG, HIBL, LABU, AGQ, GDXU, NUGT, DFEN, UGL, WEBL. Run under explicit user
authorization, 2026-08-22 ("yes, run the neighborhood check on all 11 remaining Tranche-1
tickers... as a background process, 1 worker").

Usage: .venv/bin/python scripts/run_ground_truth_neighborhood_batch.py
Reports per-ticker as each finishes (not batched at the end).
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TICKERS = ["DPST", "KORU", "JNUG", "HIBL", "LABU", "AGQ", "GDXU", "NUGT", "DFEN", "UGL", "WEBL"]


def main():
    for i, ticker in enumerate(TICKERS, 1):
        print(f"\n{'='*70}\n[{i}/{len(TICKERS)}] {ticker}\n{'='*70}", flush=True)
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "run_ground_truth_neighborhood.py"),
             "--ticker", ticker, "--workers", "1"],
            cwd=ROOT,
        )
        if result.returncode != 0:
            print(f"[{ticker}] FAILED (exit {result.returncode}) -- continuing to next ticker", flush=True)

    print(f"\n{'='*70}\nBatch complete: {len(TICKERS)} tickers\n{'='*70}", flush=True)


if __name__ == "__main__":
    main()
