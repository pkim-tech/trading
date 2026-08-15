"""Enables auto-fill detection (the daemon auto-detecting and reconciling a real
fill, instead of a human tapping "Filled" in Slack) across real live watch_list
nodes in one deliberate step, via schwab_safety.bulk_enable_auto_fill_detection.

The real gate is an AND of two persisted flags -- a per-ticker one
(cache/live/schwab_auto_fill_detection.json) and a per-node one
(cache/live/schwab_node_auto_fill_detection.json) -- and this sets both, for
every node that is state='live' AND whose account is trading_enabled. The
target set is resolved FRESH from watch_list on every run, never a hardcoded
id list. --node-ids/--tickers are FILTERS ONLY: they narrow that set and can
never add a node outside it (a paper node, or one in a dry-run account, cannot
be granted auto-fill trust by naming it here).

DEFAULTS TO A DRY PREVIEW. Without --apply nothing is written anywhere; the
run just prints what it would do. That staging gate is the point -- the
project direction is "no manual anything" and the end state is every real live
node enabled, but flipping it is a deliberate human step taken once the
safety-net tranches have landed, not something that happens as a side effect.

A node a human explicitly Disabled (via the Slack per-row "🤖 Disable" button,
which is now the emergency override since the "Enable" button was removed from
the report) is SKIPPED and listed separately -- silently undoing an emergency
override in a bulk sweep would defeat it. Use --force to re-enable those too.

Recommended first real run: start with --min-notional 10000 so the small
soxl_ira staged-test nodes (some of which are deliberately-detuned live-test
vehicles with an in-flight proof running) aren't switched off manual
confirmation as a side effect. --min-notional respects
starting_notional_override, which is what actually sizes real orders.

Usage:
  .venv/bin/python scripts/bulk_enable_auto_fill_detection.py                       # preview all
  .venv/bin/python scripts/bulk_enable_auto_fill_detection.py --min-notional 10000  # preview real-money tier
  .venv/bin/python scripts/bulk_enable_auto_fill_detection.py --min-notional 10000 --apply
  .venv/bin/python scripts/bulk_enable_auto_fill_detection.py --tickers NUGT SOXL --apply
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import schwab_safety


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--apply', action='store_true',
                    help="actually write the flags (default: preview only, writes nothing)")
    ap.add_argument('--force', action='store_true',
                    help="also re-enable nodes a human explicitly Disabled")
    ap.add_argument('--min-notional', type=float, default=None,
                    help="only nodes sized at or above this (honors starting_notional_override)")
    ap.add_argument('--node-ids', type=int, nargs='*', default=None,
                    help="filter to these watch_list ids (intersects the real target set)")
    ap.add_argument('--tickers', nargs='*', default=None,
                    help="filter to these tickers (intersects the real target set)")
    args = ap.parse_args()

    result = schwab_safety.bulk_enable_auto_fill_detection(
        min_notional=args.min_notional, node_ids=args.node_ids,
        tickers=args.tickers, apply=args.apply, force=args.force)
    print(schwab_safety.format_bulk_enable_auto_fill_detection(result))

    if not args.apply and result['changed']:
        print("\nNothing was written. Re-run with --apply to commit the above.")


if __name__ == '__main__':
    main()
