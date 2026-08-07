"""Relabels a node's watch_list.version to 'canary' -- for a node whose real
role turned out to be proof-of-life/coverage testing (like the original 6
canary nodes) rather than a real backtest-derived config version (e.g. 'v4'),
so the label reflects its actual purpose instead of a stale/misleading one.
Does not touch mode/account/any trading config -- version is a label only.

Usage:
  .venv/bin/python scripts/mark_nodes_as_canary.py --tickers FAZ SPXU TWM QID SDOW JNUG JDST
  .venv/bin/python scripts/mark_nodes_as_canary.py --wl-ids 148 144 146 145 147 150 151
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", nargs="+", help="resolves each ticker's mode='live' node (errors if ambiguous)")
    ap.add_argument("--wl-ids", nargs="+", type=int, help="exact node ids, unambiguous by construction")
    args = ap.parse_args()
    if not args.tickers and not args.wl_ids:
        ap.error("pass --tickers T [T ...] or --wl-ids ID [ID ...]")

    wl_ids = list(args.wl_ids or [])
    if args.tickers:
        for ticker in args.tickers:
            with db._conn() as c:
                rows = [dict(r) for r in c.execute(
                    "SELECT * FROM watch_list WHERE ticker = ? AND watchlist_id = ?",
                    (ticker, db.get_active_watchlist_id())).fetchall()]
            live_rows = [r for r in rows if r.get("state") != "paper"]
            if len(live_rows) != 1:
                print(f"  [skip] {ticker}: {len(live_rows)} non-paper nodes on the active watchlist "
                      f"(need exactly 1) -- resolve manually with --wl-ids")
                continue
            wl_ids.append(live_rows[0]["id"])

    for wl_id in wl_ids:
        node = db.get_watch_list_node_by_id(wl_id)
        if node is None:
            print(f"  [skip] wl_id={wl_id}: no such node")
            continue
        old_version = node["version"]
        with db._conn() as c:
            c.execute("UPDATE watch_list SET version='canary' WHERE id=?", (wl_id,))
            c.commit()
        print(f"  {node['ticker']:6s} wl_id={wl_id}: version {old_version!r} -> 'canary'")


if __name__ == "__main__":
    main()
