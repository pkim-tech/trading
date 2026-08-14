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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--min-trades", type=int, default=5)
    args = ap.parse_args()

    for t in args.tickers:
        for cd in range(1, 16):
            subprocess.run(
                [".venv/bin/python", "scripts/run_overlay_shim.py", t, "--confirm-days", str(cd)],
                capture_output=True, text=True,
            )
        print(f"{t}: swept confirm_days 1-15", file=sys.stderr)

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

    for t, (cd, _, _) in best_by_ticker.items():
        if cd is not None:
            subprocess.run(
                [".venv/bin/python", "scripts/run_overlay_shim.py", t, "--confirm-days", str(cd)],
                capture_output=True, text=True,
            )
    print("Done -- winning confirm_days re-run last per ticker.", file=sys.stderr)


if __name__ == "__main__":
    main()
