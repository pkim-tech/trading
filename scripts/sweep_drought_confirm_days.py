"""Sweeps confirm_days 1-15 per ticker via run_overlay_shim.py, picks each
ticker's best (by compounded drought return, min 5 trades to avoid overfitting
on a thin sample), then re-runs with the winning value so it's the most-recent
row candidate_full_review.py's `ORDER BY run_timestamp DESC` query picks up
for drought_ie_confirm_days.

Full data only (no fit/test half-split, no single-trade-removal stress test) --
this is the "find a candidate" pass, not the full validation in
docs/overlay_parameter_robustness_process.md. Run that process's remaining
steps by hand before trusting a winning confirm_days as genuinely robust.

Usage:
  .venv/bin/python scripts/sweep_drought_confirm_days.py TICKER [TICKER ...]
"""
import argparse
import sqlite3
import subprocess
import sys

DB = "cache/research/trading_universe.db"


def run_shim(ticker, confirm_days):
    """Run run_overlay_shim.py for one (ticker, confirm_days). Returns True on success;
    on failure, prints the real exit code + stderr instead of swallowing it."""
    result = subprocess.run(
        [".venv/bin/python", "scripts/run_overlay_shim.py", ticker, "--confirm-days", str(confirm_days)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(
            f"ERROR: run_overlay_shim.py {ticker} --confirm-days {confirm_days} "
            f"failed (exit {result.returncode})\n"
            f"stdout: {result.stdout.strip()}\nstderr: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--min-trades", type=int, default=5)
    args = ap.parse_args()

    failed_cds = {}  # ticker -> set of confirm_days that failed this run
    for t in args.tickers:
        failed_cds[t] = set()
        for cd in range(1, 16):
            if not run_shim(t, cd):
                failed_cds[t].add(cd)
        n_failed = len(failed_cds[t])
        status = f" ({n_failed} FAILED)" if n_failed else ""
        print(f"{t}: swept confirm_days 1-15{status}", file=sys.stderr)

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    best_by_ticker = {}
    for t in args.tickers:
        c.execute("""
            SELECT confirm_days, ret FROM candidate_overlay_results
            WHERE ticker=? AND mechanism='drought'
            ORDER BY confirm_days
        """, (t,))
        by_cd = {}
        for cd, ret in c.fetchall():
            by_cd.setdefault(cd, []).append(ret)
        best_cd, best_compounded, best_n = None, None, 0
        for cd, rets in by_cd.items():
            if cd in failed_cds[t]:
                # this run's shim invocation failed for this confirm_days -- any DB rows
                # here are stale from a prior run, don't let them win on fresh-looking data
                continue
            if len(rets) < args.min_trades:
                continue
            compounded = 1.0
            for r in rets:
                compounded *= (1 + r)
            compounded -= 1
            if best_compounded is None or compounded > best_compounded:
                best_cd, best_compounded, best_n = cd, compounded, len(rets)
        best_by_ticker[t] = (best_cd, best_compounded, best_n)
        print(f"{t}: best confirm_days={best_cd} compounded={best_compounded} n={best_n}")

    rerun_failed = []
    rerun_skipped = []
    for t, (cd, _, _) in best_by_ticker.items():
        if cd is None:
            continue
        if failed_cds[t]:
            # sweep for this ticker had a genuine subprocess failure -- don't write an
            # authoritative-looking final row (candidate_full_review.py picks the latest
            # run_timestamp) off the back of an incomplete sweep
            rerun_skipped.append(t)
            continue
        if not run_shim(t, cd):
            rerun_failed.append(t)

    any_sweep_failures = any(failed_cds[t] for t in args.tickers)
    if any_sweep_failures or rerun_failed:
        print("Done -- FAILURES occurred, results below are incomplete/unreliable:", file=sys.stderr)
        for t in args.tickers:
            if failed_cds[t]:
                print(f"  {t}: confirm_days {sorted(failed_cds[t])} failed during sweep", file=sys.stderr)
        for t in rerun_failed:
            print(f"  {t}: final winning-confirm_days re-run failed", file=sys.stderr)
        for t in rerun_skipped:
            print(f"  {t}: final re-run skipped (sweep had failures) -- no authoritative row written", file=sys.stderr)
        sys.exit(1)

    print("Done -- winning confirm_days re-run last per ticker.", file=sys.stderr)


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
