"""Ticket-model log of real live-trading incidents -- something that actually
fired against real order-placement code and produced a bad or surprising
outcome (not a coverage gap, not a design/backlog note). Rows are never
deleted; resolved_ts NULL means still open. See signals_db.trading_incidents.

Usage:
  .venv/bin/python scripts/trading_incidents.py log "title" "detail text" \
      [--ticker TICKER] [--account ACCOUNT] [--node-id ID] [--real-money]
  .venv/bin/python scripts/trading_incidents.py resolve ID "resolution text"
  .venv/bin/python scripts/trading_incidents.py list [--open]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_log = sub.add_parser('log')
    p_log.add_argument('title')
    p_log.add_argument('detail')
    p_log.add_argument('--ticker', default=None)
    p_log.add_argument('--account', default=None)
    p_log.add_argument('--node-id', type=int, default=None)
    p_log.add_argument('--real-money', action='store_true')

    p_resolve = sub.add_parser('resolve')
    p_resolve.add_argument('incident_id', type=int)
    p_resolve.add_argument('resolution')

    p_list = sub.add_parser('list')
    p_list.add_argument('--open', action='store_true')

    args = ap.parse_args()
    db.ensure_tables()

    if args.cmd == 'log':
        iid = db.log_incident(args.title, args.detail, ticker=args.ticker, account=args.account,
                               node_id=args.node_id, real_money_impact=args.real_money)
        print(f"logged incident id={iid}")
    elif args.cmd == 'resolve':
        db.resolve_incident(args.incident_id, args.resolution)
        print(f"resolved incident id={args.incident_id}")
    elif args.cmd == 'list':
        rows = db.get_incidents(open_only=args.open)
        if not rows:
            print("no incidents" + (" open" if args.open else ""))
            return
        for r in rows:
            status = "OPEN" if r['resolved_ts'] is None else f"resolved {r['resolved_ts']}"
            money = " [REAL MONEY]" if r['real_money_impact'] else ""
            print(f"#{r['id']} [{status}]{money} {r['ts']} — {r['title']}"
                  f" (ticker={r['ticker']}, account={r['account']})")
            print(f"    {r['detail']}")
            if r['resolution']:
                print(f"    resolution: {r['resolution']}")


if __name__ == '__main__':
    main()
