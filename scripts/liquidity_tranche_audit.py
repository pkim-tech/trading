"""Full history of the liquidity-screen sweep_tranches table, including
soft-removed (disqualified) tickers and their reasons -- the query this
whole DB migration exists to make possible (scripts/liquidity_tranches.txt
never carried more than a comment explaining a removal by hand).

Usage: .venv/bin/python scripts/liquidity_tranche_audit.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db_cache

CAMPAIGN = 'liquidity_screen'


def main():
    rows = db_cache.get_tranche_audit(CAMPAIGN)
    active = [r for r in rows if r['active']]
    removed = [r for r in rows if not r['active']]

    print(f"{len(active)} active, {len(removed)} removed, {len(rows)} total\n")
    print("Active:")
    for r in active:
        print(f"  [{r['tranche_num']:2d}] {r['ticker']:6s} added {r['added_at']}"
              + (f"  ({r['reason']})" if r['reason'] else ""))

    if removed:
        print("\nRemoved:")
        for r in removed:
            print(f"  [{r['tranche_num']:2d}] {r['ticker']:6s} removed {r['removed_at']} -- {r['reason']}")


if __name__ == '__main__':
    main()
