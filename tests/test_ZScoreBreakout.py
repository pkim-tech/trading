import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from active_signals import compute_buy_signal, check_sell_condition
from tests.conftest import make_synthetic_csv, cleanup_csv, fake_node, fake_position

TICKER   = 'TEST_ZSB'
STRATEGY = 'ZScoreBreakout'


def node(**kw):  return fake_node(TICKER, STRATEGY, **kw)
def pos(**kw):   return fake_position(TICKER, STRATEGY, **kw)


def test_buy_signal_price_well_below_lower_band():
    make_synthetic_csv(TICKER, last_close=85.0)
    try:
        sig = compute_buy_signal(node())
        assert sig is not None
        assert sig['signal'] == 'BUY'
        assert round(sig['current_price'], 1) == 85.0
    finally:
        cleanup_csv(TICKER)


def test_hold_signal_price_above_lower_band():
    make_synthetic_csv(TICKER, last_close=101.0)
    try:
        sig = compute_buy_signal(node())
        assert sig['signal'] == 'HOLD'
    finally:
        cleanup_csv(TICKER)


def test_unknown_strategy_returns_none():
    assert compute_buy_signal(fake_node(TICKER, 'NoSuchStrategy')) is None


def test_sell_tp_hit():
    make_synthetic_csv(TICKER, last_close=115.0)
    try:
        reason, target, _ = check_sell_condition(pos(entry_price=100.0, tp=10), 115.0, datetime.now())
        assert reason == 'TP'
        # ZScoreBreakout.check_exit is bar-close-only: it reports the
        # triggering current_price, not a computed entry * (1 + tp) level.
        assert round(target, 2) == 115.0
    finally:
        cleanup_csv(TICKER)


def test_sell_sl_hit():
    make_synthetic_csv(TICKER, last_close=115.0)
    try:
        reason, target, _ = check_sell_condition(pos(entry_price=100.0, sl=5), 94.0, datetime.now())
        assert reason == 'SL'
        assert round(target, 2) == 94.0
    finally:
        cleanup_csv(TICKER)


def test_sell_time_exit():
    make_synthetic_csv(TICKER, last_close=101.0)
    try:
        reason, _, _ = check_sell_condition(pos(entry_price=100.0, hours_ago=60, hold=56), 101.0, datetime.now())
        assert reason == 'TIME'
    finally:
        cleanup_csv(TICKER)


def test_no_exit_position_healthy_within_hold_time():
    make_synthetic_csv(TICKER, last_close=103.0)
    try:
        reason, _, _ = check_sell_condition(pos(entry_price=100.0, hours_ago=10, tp=10, sl=5, hold=56), 103.0, datetime.now())
        assert reason is None
    finally:
        cleanup_csv(TICKER)
