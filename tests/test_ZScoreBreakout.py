#!/usr/bin/env python3
import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

from active_signals import compute_buy_signal, check_sell_condition
from tests.conftest import make_synthetic_csv, cleanup_csv, fake_node, fake_position, run_tests

TICKER   = 'TEST_ZSB'
STRATEGY = 'ZScoreBreakout'

def node(**kw):  return fake_node(TICKER, STRATEGY, **kw)
def pos(**kw):   return fake_position(TICKER, STRATEGY, **kw)

results = []

make_synthetic_csv(TICKER, last_close=85.0)
sig = compute_buy_signal(node())
results += run_tests("BUY signal — price well below lower band", [
    ("returns result",       sig is not None,                     True),
    ("signal == BUY",        sig['signal'] if sig else None,      'BUY'),
    ("current_price = 85.0", round(sig['current_price'], 1) if sig else None, 85.0),
])

make_synthetic_csv(TICKER, last_close=101.0)
sig = compute_buy_signal(node())
results += run_tests("HOLD signal — price above lower band", [
    ("signal == HOLD", sig['signal'] if sig else None, 'HOLD'),
])

results += run_tests("Unknown strategy returns None", [
    ("returns None", compute_buy_signal(fake_node(TICKER, 'NoSuchStrategy')), None),
])

make_synthetic_csv(TICKER, last_close=115.0)
results += run_tests("Sell: TP hit", [
    ("reason == TP",           check_sell_condition(pos(entry_price=100.0, tp=10), 115.0, datetime.now())[0], 'TP'),
    ("target == entry * 1.10", round(check_sell_condition(pos(entry_price=100.0, tp=10), 115.0, datetime.now())[1], 2), 110.0),
])

results += run_tests("Sell: SL hit", [
    ("reason == SL",           check_sell_condition(pos(entry_price=100.0, sl=5), 94.0, datetime.now())[0], 'SL'),
    ("target == entry * 0.95", round(check_sell_condition(pos(entry_price=100.0, sl=5), 94.0, datetime.now())[1], 2), 95.0),
])

results += run_tests("Sell: TIME exit", [
    ("reason == TIME", check_sell_condition(pos(entry_price=100.0, hours_ago=60, hold=56), 101.0, datetime.now())[0], 'TIME'),
])

results += run_tests("No exit — position healthy within hold time", [
    ("reason == None", check_sell_condition(pos(entry_price=100.0, hours_ago=10, tp=10, sl=5, hold=56), 103.0, datetime.now())[0], None),
])

cleanup_csv(TICKER)

passed = sum(results)
total  = len(results)
print(f"\n{'='*40}")
print(f"  ZScoreBreakout: {passed}/{total} passed {'✓' if passed == total else '✗'}")
print(f"{'='*40}\n")
sys.exit(0 if passed == total else 1)
