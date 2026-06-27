#!/usr/bin/env python3
"""
Live test driver for active_signals.py Socket Mode flow.

Uses a synthetic TEST ticker with ZScoreBreakout (window=5, TP=5, SL=5, hold=24h).

Commands:
    python scripts/live_test.py setup    # write TEST CSV with BUY-triggering price, add to watch list
    python scripts/live_test.py sell     # pump TEST price above TP target
    python scripts/live_test.py status   # show watch list entry + open positions for TEST
    python scripts/live_test.py cleanup  # remove TEST from watch list, positions, and CSV

Flow:
    1. python scripts/live_test.py setup
    2. SIGNAL_POLL_SECS=10 python active_signals.py   (separate terminal)
    3. Click "Executed" in Slack, enter price 95.00
    4. python scripts/live_test.py sell
    5. Wait for next poll — click "Exited" in Slack
    6. python scripts/live_test.py cleanup
"""

import sys
import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))
from active_signals import add_node, remove_node, get_watchlist, get_open_positions, close_position, DB_PATH

CACHE_DIR  = Path("./cache")
TEST_CSV   = CACHE_DIR / "TEST_1h.csv"

# Node params
TICKER     = "TEST"
STRATEGY   = "ZScoreBreakout"
VERSION    = "test"
WINDOW     = 5
TP         = 5
SL         = 5
HOLD       = 24

# Synthetic price series: 6 days of daily variation, last hourly bar is the signal price
# Day closes: [103, 97, 102, 98, 101, ???]
# Window=5 → SMA of last 5 = mean([97,102,98,101,X]), Std ≈ 2.5
# Lower band ≈ 99.6 - 2*2.2 = ~95.2  →  BUY price = 94.0 (clearly below)
_DAY_CLOSES = [103.0, 97.0, 102.0, 98.0, 101.0]
BUY_PRICE   = 94.0   # < lower band (~95.2)
SELL_PRICE  = 100.0  # > BUY_PRICE * 1.05 = 98.7  →  triggers TP


def _write_csv(final_price: float):
    CACHE_DIR.mkdir(exist_ok=True)
    now   = datetime.now().replace(minute=0, second=0, microsecond=0)
    rows  = []

    # One bar per day for the warm-up period
    for i, close in enumerate(_DAY_CLOSES):
        dt = now - timedelta(days=(len(_DAY_CLOSES) - i))
        rows.append({"Datetime": dt, "Close": close,
                     "High": close + 1, "Low": close - 1,
                     "Open": close, "Volume": 100000})

    # Final hourly bar — this is what current_price reads
    rows.append({"Datetime": now, "Close": final_price,
                 "High": final_price + 0.5, "Low": final_price - 0.5,
                 "Open": final_price, "Volume": 100000})

    df = pd.DataFrame(rows).set_index("Datetime")
    df.to_csv(TEST_CSV)
    print(f"  Wrote {TEST_CSV}  (last close = {final_price})")


def _test_watchlist_entry():
    return next((n for n in get_watchlist()
                 if n['ticker'] == TICKER and n['version'] == VERSION), None)


def _test_positions():
    return [p for p in get_open_positions() if p['ticker'] == TICKER]


def cmd_setup():
    _write_csv(BUY_PRICE)
    add_node(TICKER, STRATEGY, VERSION, WINDOW, TP, SL, HOLD, label="live-test")
    entry = _test_watchlist_entry()
    if entry:
        print(f"  Watch list: ID={entry['id']}  {TICKER} w={WINDOW} TP={TP} SL={SL} hold={HOLD}h")
    else:
        print("  [warn] node already existed — run cleanup first if you want a fresh test")
    print()
    print("Next steps:")
    print(f"  1. SIGNAL_POLL_SECS=10 python active_signals.py")
    print(f"  2. BUY signal fires immediately — click Executed in Slack, enter {BUY_PRICE}")
    print(f"  3. python scripts/live_test.py sell")


def cmd_sell():
    positions = _test_positions()
    if not positions:
        print("  No open TEST position found. Did you click Executed in Slack?")
        return
    pos        = positions[0]
    entry      = pos['entry_price']
    tp_target  = entry * (1 + TP / 100)
    _write_csv(SELL_PRICE)
    print(f"  Entry was ${entry:.2f} — TP target ${tp_target:.2f} — sell price ${SELL_PRICE:.2f}")
    print("  Wait for next poll (~10s) — SELL signal will fire.")


def cmd_status():
    entry = _test_watchlist_entry()
    print(f"Watch list: {entry if entry else 'not found'}")
    positions = _test_positions()
    print(f"Positions:  {positions if positions else 'none'}")
    if TEST_CSV.exists():
        df  = pd.read_csv(TEST_CSV, index_col=0, parse_dates=True)
        print(f"CSV:        {len(df)} rows, last close = {df['Close'].iloc[-1]}")
    else:
        print("CSV:        not found")


def cmd_cleanup():
    entry = _test_watchlist_entry()
    if entry:
        remove_node(entry['id'])
        print(f"  Removed watch list ID {entry['id']}")
    else:
        print("  Watch list entry not found")

    with sqlite3.connect(DB_PATH) as c:
        deleted = c.execute("DELETE FROM open_positions WHERE ticker=?", (TICKER,)).rowcount
        c.commit()
    print(f"  Deleted {deleted} open position(s)")

    if TEST_CSV.exists():
        TEST_CSV.unlink()
        print(f"  Deleted {TEST_CSV}")
    else:
        print(f"  {TEST_CSV} not found")


_CMDS = {
    'setup':   cmd_setup,
    'sell':    cmd_sell,
    'status':  cmd_status,
    'cleanup': cmd_cleanup,
}

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    fn  = _CMDS.get(cmd)
    if fn is None:
        print(f"Unknown command: {cmd}")
        print(f"Usage: python scripts/live_test.py [{' | '.join(_CMDS)}]")
        sys.exit(1)
    fn()
