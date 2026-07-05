"""Recommend the next trail_pct backfill versions to run for TrailingBothZScoreBreakout.

The sparse extension (docs/backlog.md, 2026-07-05) tests trail_pct at 1-7% (v3.21-27,
already run) plus a sparse set at 9/12/15/18/21/24/27/30% (v3.29/32/35/38/41/44/47/50).
Every single-percent value 8-30% has a version slot wired in run_v3_backfill_sweep.sh
(version = trail_pct% + 20), but only the sparse ones are run by default.

This script looks at whatever sparse data already exists in backtest_cache, finds each
ticker's best trail_pct value so far, and prints the run_v3_backfill_sweep.sh commands
to backfill its immediate ±1 neighbors (already-wired versions, no script edit needed)
so the true local optimum can be narrowed in — mirrors the sweep engine's own
coarse-then-island refinement, just applied to the trail_pct axis instead of tp/sl.

Prints commands only — does not execute anything.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "cache" / "trading_universe.db"
STRATEGY = "TrailingBothZScoreBreakout"
SPARSE_VALUES = [1, 2, 3, 4, 5, 6, 7, 9, 12, 15, 18, 21, 24, 27, 30]


def version_for(pct):
    return f"v3.{pct + 20}"


def main():
    conn = sqlite3.connect(DB_PATH)
    tickers = [r[0] for r in conn.execute(
        "SELECT DISTINCT ticker FROM backtest_cache WHERE strategy=? AND version LIKE 'v3.2%'",
        (STRATEGY,)
    ).fetchall()]

    recommendations = []
    for ticker in sorted(tickers):
        best_pct, best_alpha = None, float("-inf")
        present = set()
        for pct in SPARSE_VALUES:
            ver = version_for(pct)
            row = conn.execute(
                "SELECT MAX(alpha_vs_spy) FROM backtest_cache "
                "WHERE ticker=? AND version=? AND strategy=? AND trades > 0",
                (ticker, ver, STRATEGY)
            ).fetchone()
            if row[0] is None:
                continue
            present.add(pct)
            if row[0] > best_alpha:
                best_alpha, best_pct = row[0], pct

        if best_pct is None:
            print(f"{ticker}: no sparse data yet — run the sparse extension first")
            continue

        neighbors = sorted({best_pct - 1, best_pct + 1} & set(range(1, 31)) - present)
        print(f"{ticker}: best sparse trail_pct={best_pct}% (alpha={best_alpha:.1f}%)"
              + (f" — neighbors {neighbors} already covered" if not neighbors else ""))
        for pct in neighbors:
            recommendations.append((ticker, pct))

    if not recommendations:
        print("\nNothing to fill — every best-value neighbor is already covered.")
        return

    print("\nRecommended gap-fill runs:")
    by_version = {}
    for ticker, pct in recommendations:
        by_version.setdefault(version_for(pct), []).append(ticker)
    for ver, ts in sorted(by_version.items()):
        print(f"  ./scripts/run_v3_backfill_sweep.sh {ver} {' '.join(sorted(set(ts)))}")


if __name__ == "__main__":
    main()
