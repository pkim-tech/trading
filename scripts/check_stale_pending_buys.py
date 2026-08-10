"""
Flags any pending_buys row that never resolved (order_placed=False, no
real broker order) and is older than today -- the class of gap the 4:05
EOD Coverage Report doesn't catch, since that report only checks
scenario_expectations rows (the canary A-F/G scenarios + per-node
reconciliation_mismatch), not generic unresolved pending buys on ordinary
live nodes.

Found 2026-08-09/10: RETL's drought-entry pending buy (2026-08-07,
blocked by a resting LABD order in the same account) sat unresolved for
2+ days with nothing flagging it -- the daemon being down that whole
time meant no reminders fired either, and status_check.py only surfaces
it if someone thinks to run it.

Usage:
  .venv/bin/python scripts/check_stale_pending_buys.py
"""
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db


def main():
    today = date.today()
    stale = []
    for pending in db.get_pending_buys():
        if pending['order_placed']:
            continue
        signal_time = datetime.strptime(pending['signal_time'], '%Y-%m-%d %H:%M:%S')
        if signal_time.date() >= today:
            continue
        stale.append(pending)

    if not stale:
        print("No stale unresolved pending buys.")
        return

    for p in stale:
        node = p['node']
        print(f"{p['ticker']}  wl_id={p['wl_id']}  account={node.get('account')}  "
              f"state={node.get('state')}  version={node.get('version')}")
        print(f"  signal_time={p['signal_time']}  order_placed={bool(p['order_placed'])}  "
              f"reminder_count={p['reminder_count']}  last_reminder_at={p['last_reminder_at']}")
        print(f"  position_source={p.get('position_source')}")

    print(f"\n{len(stale)} stale unresolved pending buy(s).")


if __name__ == "__main__":
    main()
