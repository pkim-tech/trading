"""Prints the current real capital-at-stake tickers (space-separated, sorted), for use
in shell contexts that need to scope work to real money without hardcoding a ticker list
that goes stale -- node-to-account/ticker assignment shifts often (CLAUDE.md's Live
Trading section). Same query/filter as scripts/evening_status.py's real_capital_nodes()
(state='live' AND archived_at IS NULL, filtered by signals_helpers.has_capital_at_stake),
kept independent rather than importing evening_status.py to avoid pulling in that
module's much heavier import chain for a one-line answer.

Usage:
  .venv/bin/python scripts/real_capital_tickers.py
  scripts/fetch_massive_minute_data.py --tickers $(.venv/bin/python scripts/real_capital_tickers.py)
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_helpers as helpers

LIVE_DB = Path(__file__).resolve().parent.parent / "cache" / "live" / "trading_live.db"


def real_capital_tickers():
    con = sqlite3.connect(LIVE_DB)
    con.row_factory = sqlite3.Row
    nodes = [dict(r) for r in con.execute(
        "SELECT * FROM watch_list WHERE state='live' AND archived_at IS NULL")]
    con.close()
    return sorted({n['ticker'] for n in nodes if helpers.has_capital_at_stake(n)})


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    print(" ".join(real_capital_tickers()))
