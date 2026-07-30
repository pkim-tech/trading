"""One-off: full watch_list_audit history (no limit) for a given set of
tickers -- checks for any add_node/remove_node/modify event before today, to
confirm or rule out an earlier delete-then-recreate cycle."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

ap = argparse.ArgumentParser()
ap.add_argument("--tickers", nargs="+", required=True)
args = ap.parse_args()

with db._conn() as c:
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM watch_list_audit WHERE ticker IN ({}) ORDER BY id".format(
            ",".join("?" * len(args.tickers))
        ), args.tickers
    ).fetchall()]

for r in rows:
    print(r)
print(f"\n{len(rows)} audit row(s) total for {args.tickers}.")
