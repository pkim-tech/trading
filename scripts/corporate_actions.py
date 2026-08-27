"""
CLI for the manually-populated corporate_actions table (signals_db), built for
docs/backlog_cache.md's 2026-08-19/20 "corporate_actions table + detect_price_
discontinuity fix" item -- closes a confirmed false-positive class where the
price-ratio-only heuristic in signals_helpers.detect_price_discontinuity
mistook an ordinary large move for a split (NUGT 2026-08-14, GDXU 2026-08-19).

Population is manual/periodic (every few days), by design -- see that backlog
entry for why (direxion.com/proshares.com Cloudflare-block direct fetches;
WebSearch + mirror sources -- Yahoo Finance, GlobeNewswire, prnewswire.com,
sponsors' own newsroom pages when they load directly -- is the validated
method). This script is the add/list interface for that manual workflow, not
a scraper.

RATIO convention: reference_price / current_price, matching signals_helpers.
detect_price_discontinuity's own return value -- e.g. a 2-for-1 forward split
(price halves) is `2.0`, a 1-for-10 reverse split (price 10x's) is `0.1`.
Must be > 0 (validated by signals_db.add_corporate_action).

Usage:
  .venv/bin/python scripts/corporate_actions.py add TICKER RATIO EFFECTIVE_DATE \
      [--sponsor S] [--announced YYYY-MM-DD] [--source URL]
  .venv/bin/python scripts/corporate_actions.py list [TICKER]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_db as db


def cmd_add(args):
    db.ensure_tables()
    db.add_corporate_action(
        args.ticker.upper(), args.ratio, args.effective_date,
        sponsor=args.sponsor, announced_date=args.announced, source_url=args.source,
    )
    print(f"Recorded: {args.ticker.upper()} ratio={args.ratio} effective={args.effective_date}")


def cmd_list(args):
    db.ensure_tables()
    rows = db.get_corporate_actions(args.ticker.upper() if args.ticker else None)
    if not rows:
        print("No corporate actions on file.")
        return
    for r in rows:
        print(f"{r['ticker']:<8} ratio={r['split_ratio']:<8} effective={r['effective_date']} "
              f"announced={r['announced_date'] or '-'} sponsor={r['sponsor'] or '-'} "
              f"source={r['source_url'] or '-'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_add = sub.add_parser('add', help='Record a confirmed real corporate action')
    p_add.add_argument('ticker')
    p_add.add_argument('ratio', type=float)
    p_add.add_argument('effective_date')
    p_add.add_argument('--sponsor')
    p_add.add_argument('--announced')
    p_add.add_argument('--source')
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser('list', help='List recorded corporate actions')
    p_list.add_argument('ticker', nargs='?')
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
