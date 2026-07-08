"""Log a manually-executed fill (bought outside the daemon's own signal flow)
into open_positions, using the active watchlist's node for that ticker.

Usage: python scripts/log_manual_position.py TICKER SHARES ENTRY_PRICE [watchlist_id]
Example: python scripts/log_manual_position.py EDC 400 77.79
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import active_signals as a


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    ticker = sys.argv[1].upper()
    shares = int(sys.argv[2])
    entry_price = float(sys.argv[3])
    watchlist_id = int(sys.argv[4]) if len(sys.argv) > 4 else a.get_active_watchlist_id()

    wl = a.get_watchlist(watchlist_id)
    matches = [n for n in wl if n['ticker'] == ticker and n.get('mode') == 'live']
    if not matches:
        print(f"no live node for {ticker} on watchlist {watchlist_id}")
        sys.exit(1)
    if len(matches) > 1:
        print(f"multiple live nodes for {ticker} on watchlist {watchlist_id}, pick one:")
        for n in matches:
            print(f"  id={n['id']}  strategy={n['strategy']}  version={n['version']}")
        sys.exit(1)

    node = matches[0]
    now = datetime.now()
    # No real signal fired (manual entry) -- use the fill itself as both signal and entry.
    a.open_position(node, signal_price=entry_price, signal_time=now,
                     entry_price=entry_price, entry_time=now, shares=shares)
    print(f"Logged {ticker}: {shares} sh @ ${entry_price:.4f}  node id={node['id']} "
          f"({node['strategy']} {node['version']})  account={node.get('account')}")


if __name__ == '__main__':
    main()
