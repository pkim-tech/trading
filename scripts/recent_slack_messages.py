"""Prints real recent Slack messages from signals_db.slack_message_log,
optionally filtered to messages mentioning given tickers -- lets a burst of
real alerts be reviewed directly instead of scrolling Slack by hand.

Usage:
  .venv/bin/python scripts/recent_slack_messages.py --limit 50
  .venv/bin/python scripts/recent_slack_messages.py --tickers IVV QQQ IWM DIA VOO XLF --limit 50
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", nargs="+", help="only show messages mentioning any of these tickers")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--mode", help="filter to live/dry_run/paper/sim")
    ap.add_argument("--blocks", action="store_true",
                    help="also print the raw Block Kit payload (blocks_json) of each message "
                         "(only captured for messages posted from 2026-08-14 onward -- older "
                         "rows logged text only, so this prints nothing for them)")
    args = ap.parse_args()

    rows = db.get_slack_messages(mode=args.mode, limit=args.limit)
    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
        # Searches blocks_json too, not just text: for a Block Kit message the
        # per-ticker content lives entirely in the blocks (the real 2026-08-14
        # case -- a multi-ticker signal-window digest whose `text` is just the
        # "Signal window -- 10:25 ET" header), so a text-only match missed it.
        rows = [r for r in rows
                if any(t in (r["text"] + (r.get("blocks_json") or "")).upper() for t in tickers)]

    for r in rows:
        err = f"  [ERROR: {r['error']}]" if r.get("error") else ""
        print(f"{r['ts']}  [{r['mode']}]{err}\n  {r['text']}\n")
        if args.blocks and r.get("blocks_json"):
            print(f"  blocks: {r['blocks_json']}\n")

    print(f"{len(rows)} message(s) shown.")


if __name__ == "__main__":
    main()
