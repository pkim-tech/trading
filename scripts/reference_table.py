"""Print the morning reference table: one row per live ticker with hold status,
next trigger/action, proximity, and node params. Same data used in the Slack
morning report; run this standalone for an on-demand check until the Slack
slash command exists (backlogged pending Slack app registration).

Usage:
  python scripts/reference_table.py [watchlist_id]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import active_signals as a


def main():
    watchlist_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    wl = a.get_watchlist(watchlist_id)
    rows = a.build_reference_table(wl)
    print(a.format_reference_table(rows))


if __name__ == "__main__":
    main()
