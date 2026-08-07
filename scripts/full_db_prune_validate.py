"""Full-database version of run_liquidity_tranches.sh's per-tranche prune
validation gate (locate_best_node pre vs. post), but for every ticker in
backtest_cache and with a real retry loop instead of a one-shot hard-stop --
built 2026-08-07 after prune_backtest_cache.py's missing `trades > 0` filter
(same bug class as the take_profit/NULL and entry_timing bugs found earlier
this week) let a trivial zero-trade row outrank a real losing node as a
group's "winner", dropping the real node's island during prune.

Never touches --swap or deletes anything until pre/post match for every
ticker. If they don't match, prints the mismatching tickers and stops --
the caller fixes the underlying bug in prune_backtest_cache.py and reruns
this script (it's idempotent, `--build` always recomputes from the live DB).

Usage:
  .venv/bin/python scripts/full_db_prune_validate.py
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from locate_best_node import best_row, _fmt_row
import prune_backtest_cache as pbc


def snapshot(db_path: str, tickers: list[str]) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    out = {}
    for t in tickers:
        r = best_row(conn, t, "v5")
        out[t] = _fmt_row(r)
    conn.close()
    return out


def main():
    live = str(pbc.DB_PATH)
    conn = sqlite3.connect(live)
    tickers = sorted(r[0] for r in conn.execute("SELECT DISTINCT ticker FROM backtest_cache").fetchall())
    conn.close()
    print(f"Validating prune across {len(tickers)} tickers: {tickers}")

    print("\n--- Snapshotting best node PRE-prune (from live, untouched) ---")
    pre = snapshot(live, tickers)

    print("\n--- Extracting (--build, does not touch live DB) ---")
    pbc.cmd_build()

    print("\n--- Snapshotting best node POST-prune (from the new extracted file, live still untouched) ---")
    post = snapshot(str(pbc.PRUNED_PATH), tickers)

    mismatches = [t for t in tickers if pre[t] != post[t]]
    if mismatches:
        print("\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print(f" MISMATCH for {len(mismatches)} ticker(s): {mismatches}")
        print(" Live DB and archives untouched -- no swap, nothing deleted.")
        print(" Fix the bug in prune_backtest_cache.py, then rerun this script.")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        for t in mismatches:
            print(f"\n{t}")
            print(f"  PRE:  {pre[t]}")
            print(f"  POST: {post[t]}")
        sys.exit(1)

    print(f"\nAll {len(tickers)} tickers match. Safe to --swap.")
    print("This script does NOT swap or delete anything -- run those manually next:")
    print("  .venv/bin/python scripts/prune_backtest_cache.py --swap")


if __name__ == "__main__":
    main()
