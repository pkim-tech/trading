#!/usr/bin/env python3
"""Round-trip test for the real DB plumbing: add_node -> open_position ->
check_sell_condition -> close_position, against an isolated sqlite file (never
trading_live.db). Exercises actual DB reads/writes, unlike the per-strategy
signal tests which fabricate node/position dicts directly."""
import os
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

_tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_tmp_db.close()
os.environ['TRADING_DB_PATH'] = _tmp_db.name

import active_signals as A  # noqa: E402  (must import after TRADING_DB_PATH is set)
from tests.conftest import make_synthetic_csv, cleanup_csv, run_tests  # noqa: E402

TICKER = 'TEST_ROUNDTRIP'

A.ensure_tables()
make_synthetic_csv(TICKER, last_close=100.0)

results = []

# add_node -> watch_list row round-trips with the values passed in
A.add_node(TICKER, 'ZScoreBreakout', 'test', window=20, take_profit=10, stop_loss=5,
           max_hold_hours=56)
watchlist = [n for n in A.get_watchlist() if n['ticker'] == TICKER]
results += run_tests("add_node writes a watch_list row", [
    ("one row",           len(watchlist),        1),
    ("strategy stored",   watchlist[0]['strategy'] if watchlist else None, 'ZScoreBreakout'),
    ("take_profit stored", watchlist[0]['take_profit'] if watchlist else None, 10),
])
node = watchlist[0]

# open_position -> open_positions + trade_log rows, keyed off the real watch_list node
signal_time = datetime.now() - timedelta(hours=10)
entry_time  = signal_time
A.open_position(node, signal_price=100.0, signal_time=signal_time,
                 entry_price=101.0, entry_time=entry_time, shares=50)
open_positions = [p for p in A.get_open_positions() if p['ticker'] == TICKER]
results += run_tests("open_position writes open_positions + trade_log", [
    ("one open position",   len(open_positions), 1),
    ("entry_price stored",  open_positions[0]['entry_price'] if open_positions else None, 101.0),
    ("trade_log_id set",    bool(open_positions[0]['trade_log_id']) if open_positions else False, True),
])
pos = open_positions[0]

# Duplicate open_position for the same ticker/window is a no-op, not a second row
A.open_position(node, signal_price=100.0, signal_time=signal_time,
                 entry_price=102.0, entry_time=entry_time, shares=50)
results += run_tests("open_position skips duplicate ticker/window", [
    ("still one open position", len(
        [p for p in A.get_open_positions() if p['ticker'] == TICKER]), 1),
])

# check_sell_condition against the real DB-backed position row -> SL hit
reason, price, _ = A.check_sell_condition(pos, current_price=95.0, now=datetime.now())
results += run_tests("check_sell_condition: SL hit on a DB-backed position", [
    ("reason == SL",     reason, 'SL'),
    # ZScoreBreakout.check_exit is bar-close-only and returns current_price on
    # an SL hit, not a computed stop-price level (unlike the trailing strategies).
    ("exit at current_price", price, 95.0),
])

# check_sell_condition: healthy position, no exit
reason, price, _ = A.check_sell_condition(pos, current_price=103.0, now=datetime.now())
results += run_tests("check_sell_condition: no exit when healthy", [
    ("reason is None", reason, None),
])

# close_position -> open_positions row removed, trade_log exit fields populated
A.close_position(pos['id'], exit_signal_price=95.0, exit_price=95.0,
                  exit_time=datetime.now(), exit_reason='SL')
results += run_tests("close_position removes open_positions row and logs exit", [
    ("no open positions left",
        len([p for p in A.get_open_positions() if p['ticker'] == TICKER]), 0),
])
with A._conn() as c:
    trade_row = c.execute(
        "SELECT exit_price, exit_reason, pnl_pct FROM trade_log WHERE id = ?",
        (pos['trade_log_id'],)
    ).fetchone()
results += run_tests("trade_log exit fields written", [
    ("exit_price stored",  trade_row['exit_price'] if trade_row else None, 95.0),
    ("exit_reason stored", trade_row['exit_reason'] if trade_row else None, 'SL'),
    ("pnl_pct is negative", (trade_row['pnl_pct'] < 0) if trade_row else False, True),
])

cleanup_csv(TICKER)
os.unlink(_tmp_db.name)

passed = sum(results)
total = len(results)
print(f"\n{'='*40}")
print(f"  DB round-trip: {passed}/{total} passed {'✓' if passed == total else '✗'}")
print(f"{'='*40}\n")
sys.exit(0 if passed == total else 1)
