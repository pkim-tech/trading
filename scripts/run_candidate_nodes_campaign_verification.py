"""Orchestration: Phase3 + Phase4 + Phase5 for one candidate_nodes-sourced campaign
(the in-memory sweep pipeline's own output), scoped to exactly (ticker, version, window)
-- new, 2026-08-29 (Task #3 follow-up, planner dispatch).

Real gap this closes: `scripts/candidate_summary_report.py --kernel gt` has no per-
version scoping for GT mode (a pre-existing limitation, not touched here) -- running it
naively against a ticker would ALSO re-run Phase4 for every already-covered backtest_
cache scope (e.g. Campaign A), not just the candidate_nodes campaign being verified.
This script instead calls `gt_rows_for_scope` directly per real candidate_nodes scope
for the given (ticker, version, window), so Phase4 only runs against the campaign
actually being checked. Phase3/Phase5 already have their own `--window`/`--window`
CLI flags (added same task) and are invoked as real subprocesses (their own scripts,
unmodified) rather than reimplemented here.

Usage:
  .venv/bin/python scripts/run_candidate_nodes_campaign_verification.py \\
      --ticker SOXL --version bench-inmemory-v6-massive-w2021-08-23_2026-08-21 --window 15
"""
import argparse
import os
import sqlite3
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))


def run_phase3(ticker, version, window, data_source):
    print(f"\n{'='*100}\nPHASE 3 -- {ticker} / {version} / window={window}\n{'='*100}", flush=True)
    t0 = time.monotonic()
    subprocess.run([
        sys.executable, os.path.join(ROOT, "scripts", "phase3_second_level_check.py"),
        "--ticker", ticker, "--version", version, "--window", str(window),
        "--data-source", data_source,
    ], check=True)
    print(f"[Phase3 done in {time.monotonic()-t0:.1f}s]", flush=True)


def run_phase4(ticker, version, window, data_source):
    print(f"\n{'='*100}\nPHASE 4 -- {ticker} / {version} / window={window}\n{'='*100}", flush=True)
    t0 = time.monotonic()
    from phase4_candidate_nodes_resolver import discover_candidate_nodes_scopes
    from candidate_summary_report import gt_rows_for_scope, _write_csv, _write_xlsx, GT_COLUMN_DEFS
    from run_optimization_sweep import DB_PATH

    with sqlite3.connect(DB_PATH) as conn:
        cn_scopes = discover_candidate_nodes_scopes(ticker, version)
    scopes = [(strategy, entry_timing, fixed_sl) for strategy, entry_timing, fixed_sl, w in cn_scopes
              if w == window]
    print(f"Found {len(scopes)} real candidate_nodes scope(s) at window={window}.")

    all_rows = []
    for strategy, entry_timing, fixed_sl in scopes:
        print(f"\n{'#'*80}\n{ticker} / {strategy} / {version} / entry_timing={entry_timing} "
              f"/ fixed_sl={fixed_sl} / window={window}\n{'#'*80}", flush=True)
        try:
            all_rows.extend(gt_rows_for_scope(ticker, strategy, version, entry_timing, fixed_sl,
                                               grid_window=window))
        except Exception as e:
            print(f"  UNEXPECTED error on this scope, skipping: {e}")

    out_name = f"phase4_{ticker.lower()}_{version}_w{window}"
    _write_csv(out_name, all_rows, col_defs=GT_COLUMN_DEFS, to_record=lambda r: r)
    _write_xlsx(out_name, all_rows, col_defs=GT_COLUMN_DEFS, to_record=lambda r: r)
    print(f"[Phase4 done in {time.monotonic()-t0:.1f}s -- {len(all_rows)} rows -- "
          f"output/{out_name}.csv / .xlsx]", flush=True)


def run_phase5(ticker, version, window, data_source, workers):
    print(f"\n{'='*100}\nPHASE 5 -- {ticker} / {version} / window={window}\n{'='*100}", flush=True)
    t0 = time.monotonic()
    subprocess.run([
        sys.executable, os.path.join(ROOT, "scripts", "phase5_second_level_overlay_check.py"),
        "--ticker", ticker, "--version", version, "--window", str(window),
        "--data-source", data_source, "--workers", str(workers),
    ], check=True)
    print(f"[Phase5 done in {time.monotonic()-t0:.1f}s]", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--window", type=int, required=True)
    ap.add_argument("--data-source", choices=["yahoo", "massive"], default="massive")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--skip", choices=["phase3", "phase4", "phase5"], action="append", default=[],
                     help="skip a phase (repeatable)")
    args = ap.parse_args()

    t_all = time.monotonic()
    if "phase3" not in args.skip:
        run_phase3(args.ticker, args.version, args.window, args.data_source)
    if "phase4" not in args.skip:
        run_phase4(args.ticker, args.version, args.window, args.data_source)
    if "phase5" not in args.skip:
        run_phase5(args.ticker, args.version, args.window, args.data_source, args.workers)
    print(f"\nAll phases done in {time.monotonic()-t_all:.1f}s total.")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
