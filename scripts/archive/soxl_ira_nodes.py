"""Lists every real watch_list node in the soxl_ira account (the one real,
dry_run=False account) with its strategy and current position/order-relevant
config -- for cross-checking against which accountability-grid rows actually
need a real (non-dry_run) order.

Usage: .venv/bin/python scripts/soxl_ira_nodes.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

with db._conn() as c:
    rows = [dict(r) for r in c.execute(
        "SELECT id, ticker, version, state, strategy, entry_timing, trail_buy_pct, "
        "fixed_sl, arm_sell_pct, trail_sell_pct, starting_notional, watchlist_id, added_at "
        "FROM watch_list WHERE account='soxl_ira' ORDER BY ticker"
    ).fetchall()]

for r in rows:
    print(f"  {r['ticker']:6s} wl_id={r['id']:4d}  version={r['version']:10s} state={r['state']:8s} "
          f"strategy={r['strategy']:28s} entry_timing={r['entry_timing']:12s} "
          f"watchlist_id={r['watchlist_id']}  added={r['added_at']}")
print(f"\n{len(rows)} soxl_ira node(s) total.")
