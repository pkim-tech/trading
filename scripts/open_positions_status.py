"""Print current open positions (ticker, strategy, entry, shares, account).

Usage:
  python scripts/open_positions_status.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import active_signals as a


def main():
    positions = a.get_open_positions()
    if not positions:
        print("No open positions.")
        return
    cols = ["ticker", "strategy", "version", "window", "shares", "entry_price", "entry_time", "signal_time"]
    widths = {c: max(len(c), *(len(str(p.get(c, ""))) for p in positions)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for p in positions:
        print("  ".join(str(p.get(c, "")).ljust(widths[c]) for c in cols))


if __name__ == "__main__":
    main()
