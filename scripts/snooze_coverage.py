"""Silence a known coverage scenario for a bounded time -- e.g. UDOW's
deliberately-seeded stale test position keeps firing reconciliation_mismatch
every poll, which is noise once the condition is already understood and
accepted, not a new finding each time. Unlike explaining a coverage_deviations
row (permanent, only ends when a human clears it), a snooze auto-expires and
resumes alerting -- see signals_db.snooze_coverage/is_snoozed.

Usage:
  .venv/bin/python scripts/snooze_coverage.py SCENARIO_KEY DAYS "reason text" \
      [--ticker TICKER] [--account ACCOUNT] [--node-id ID] [--kind KIND]
  .venv/bin/python scripts/snooze_coverage.py --list [SCENARIO_KEY]

Example -- narrow to one mismatch kind, not the whole ticker (a bare --ticker
snooze also silences missing_sl/missing_trailing_sell, i.e. "this position
may be unprotected at the broker", which is a materially different and more
severe alert than the one actually being acknowledged):
  .venv/bin/python scripts/snooze_coverage.py reconciliation_mismatch 30 \
      "known accepted UDOW test position, see signals_invariants.py" \
      --ticker UDOW --kind shares
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('scenario_key', nargs='?')
    ap.add_argument('days', nargs='?', type=float)
    ap.add_argument('reason', nargs='?')
    ap.add_argument('--ticker', default=None)
    ap.add_argument('--account', default=None)
    ap.add_argument('--node-id', type=int, default=None)
    ap.add_argument('--kind', default=None,
                     help="scope to one mismatch kind (e.g. 'shares') -- omit to snooze every "
                          "kind for this scenario/ticker, including missing_sl/missing_trailing_sell")
    ap.add_argument('--list', action='store_true')
    args = ap.parse_args()

    db.ensure_tables()

    if args.list:
        for s in db.get_active_snoozes(args.scenario_key):
            print(f"[{s['id']}] {s['scenario_key']} ticker={s['ticker']} account={s['account']} "
                  f"node_id={s['node_id']} kind={s['kind']} until={s['snoozed_until']} "
                  f"by={s['snoozed_by']}: {s['reason']}")
        return

    if not (args.scenario_key and args.days and args.reason):
        ap.error("scenario_key, days, and reason are required unless --list is passed")

    until = (datetime.now() + timedelta(days=args.days)).strftime('%Y-%m-%d %H:%M:%S')
    db.snooze_coverage(args.scenario_key, until, args.reason, ticker=args.ticker,
                        account=args.account, node_id=args.node_id, kind=args.kind)
    print(f"Snoozed {args.scenario_key} (ticker={args.ticker} account={args.account} "
          f"node_id={args.node_id} kind={args.kind}) until {until}")


if __name__ == '__main__':
    main()
