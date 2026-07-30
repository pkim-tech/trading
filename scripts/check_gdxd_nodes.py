"""One-off check: every real watch_list row for GDXD, to resolve which one
(if any) is the new v4 node added 2026-07-29 alongside the other 7 that got
relabeled canary."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

with db._conn() as c:
    rows = [dict(r) for r in c.execute(
        "SELECT id, ticker, version, account, mode, watchlist_id, added_at FROM watch_list "
        "WHERE ticker='GDXD' ORDER BY id"
    ).fetchall()]

for r in rows:
    print(r)
