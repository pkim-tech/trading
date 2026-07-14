"""Coverage for compute_buy_signal edge cases, the indicator cache, the
pending_buys (trailing-buy lifecycle) DB layer, and _trailing_buy_status.

notify_buy_signal/_build_buy_blocks are deliberately not covered here: they
hit yfinance, the research DB (avg_vol lookups), and can write to watch_list
via node['id'] -- too many live side effects for a cheap unit test. That flow
is exercised manually via scripts/live_sim.py instead (see CLAUDE.md)."""
import sys
import tempfile
import os
import pytest
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

import active_signals as A
import signals_config
from tests.conftest import make_synthetic_csv, cleanup_csv, fake_node

TICKER = 'TEST_SIG'


# ---------------------------------------------------------------------------
# compute_buy_signal edge cases
# ---------------------------------------------------------------------------

def test_compute_buy_signal_insufficient_history_returns_none():
    make_synthetic_csv(TICKER, last_close=85.0, days=5)  # far fewer than window=20
    try:
        sig = A.compute_buy_signal(fake_node(TICKER, 'ZScoreBreakout', window=20))
        assert sig is None
    finally:
        cleanup_csv(TICKER)


def test_compute_buy_signal_no_cached_data_returns_none():
    cleanup_csv(TICKER)  # ensure no leftover file
    sig = A.compute_buy_signal(fake_node(TICKER, 'ZScoreBreakout'))
    assert sig is None


def test_compute_buy_signal_price_override_skips_yfinance_lookup():
    make_synthetic_csv(TICKER, last_close=100.0)
    try:
        sig = A.compute_buy_signal(fake_node(TICKER, 'ZScoreBreakout'), price_override=77.0)
        assert sig['current_price'] == 77.0
        # override price is well below the ~100 band -> BUY
        assert sig['signal'] == 'BUY'
    finally:
        cleanup_csv(TICKER)


# ---------------------------------------------------------------------------
# Indicator cache: reused when data is unchanged, recomputed when it isn't
# ---------------------------------------------------------------------------

def test_indicator_cache_reused_for_identical_data(monkeypatch):
    make_synthetic_csv(TICKER, last_close=85.0)
    try:
        A._indicator_cache.clear()
        calls = []
        real_gen = A.strategies.ZScoreBreakout.generate_daily_indicators

        def spy(self, df):
            calls.append(1)
            return real_gen(self, df)

        monkeypatch.setattr(A.strategies.ZScoreBreakout, 'generate_daily_indicators', spy)

        node = fake_node(TICKER, 'ZScoreBreakout')
        A.compute_buy_signal(node, price_override=85.0)
        A.compute_buy_signal(node, price_override=85.0)
        assert len(calls) == 1
    finally:
        cleanup_csv(TICKER)
        A._indicator_cache.clear()


def test_indicator_cache_invalidated_when_data_changes(monkeypatch):
    A._indicator_cache.clear()
    calls = []
    real_gen = A.strategies.ZScoreBreakout.generate_daily_indicators

    def spy(self, df):
        calls.append(1)
        return real_gen(self, df)

    monkeypatch.setattr(A.strategies.ZScoreBreakout, 'generate_daily_indicators', spy)

    node = fake_node(TICKER, 'ZScoreBreakout')
    try:
        make_synthetic_csv(TICKER, last_close=85.0, days=90)
        A.compute_buy_signal(node, price_override=85.0)
        make_synthetic_csv(TICKER, last_close=85.0, days=91)  # extra day -> new cache_key
        A.compute_buy_signal(node, price_override=85.0)
        assert len(calls) == 2
    finally:
        cleanup_csv(TICKER)
        A._indicator_cache.clear()


# ---------------------------------------------------------------------------
# pending_buys DB lifecycle: add -> get -> mark placed -> clear
# ---------------------------------------------------------------------------

@pytest.fixture
def db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    A.ensure_tables()
    yield A
    os.unlink(tmp_db.name)


def _fake_sig(price=85.0):
    return {'current_price': price, 'last_bar': datetime.now()}


def test_pending_buy_round_trip(db):
    A_ = db
    node = fake_node('PB_TICK', 'TrailingBothZScoreBreakout')
    A_.add_pending_buy(node, _fake_sig(), channel='C1', ts='123.456')

    pending = A_.get_pending_buys()
    assert len(pending) == 1
    assert pending[0]['ticker'] == 'PB_TICK'
    assert pending[0]['order_placed'] == 0
    assert pending[0]['reminder_count'] == 0

    A_.mark_pending_buy_placed('PB_TICK')
    pending = A_.get_pending_buys()
    assert pending[0]['order_placed'] == 1
    assert pending[0]['reminder_count'] == 0  # reset on placement, per mark_pending_buy_placed's docstring

    A_.clear_pending_buy('PB_TICK')
    assert A_.get_pending_buys() == []


def test_update_pending_buy_reminder_bumps_count(db):
    A_ = db
    node = fake_node('PB_TICK2', 'TrailingBothZScoreBreakout')
    A_.add_pending_buy(node, _fake_sig(), channel='C1', ts='123.456')
    pending_id = A_.get_pending_buys()[0]['id']

    A_.update_pending_buy_reminder(pending_id, channel='C2', ts='789', reminder_count=3)
    pending = A_.get_pending_buys()[0]
    assert pending['reminder_channel'] == 'C2'
    assert pending['reminder_ts'] == '789'
    assert pending['reminder_count'] == 3


# ---------------------------------------------------------------------------
# _trailing_buy_status: bounce-trigger detection off Low/High bars
# ---------------------------------------------------------------------------

def _write_low_high_csv(ticker, lows, highs, start='2025-06-01 10:30:00'):
    import pandas as pd
    idx = pd.date_range(start, periods=len(lows), freq='h')
    df = pd.DataFrame({'Close': lows, 'Low': lows, 'High': highs}, index=idx)
    df.index.name = 'Datetime'
    df.to_csv(Path('./cache/research') / f"{ticker}_1h.csv")
    return idx


def test_trailing_buy_status_met_when_bounce_clears_trigger():
    ticker = 'TEST_TBSTATUS_MET'
    lows  = [100.0, 95.0, 95.0]
    # running_low settles at 95 after bar 2; trigger = 95 * 1.05 = 99.75
    highs = [100.0, 95.0, 100.5]  # bar 3 high clears 99.75
    idx = _write_low_high_csv(ticker, lows, highs)
    try:
        pending = {
            'ticker': ticker,
            'signal_time': (idx[0] - timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S'),
            'node': {'trail_buy_pct': 5.0},
        }
        met, trigger = A._trailing_buy_status(pending)
        assert met is True
        assert round(trigger, 2) == round(95.0 * 1.05, 2)
    finally:
        cleanup_csv(ticker)


def test_trailing_buy_status_not_met_when_no_bounce():
    ticker = 'TEST_TBSTATUS_NOTMET'
    lows  = [100.0, 95.0, 94.0]
    highs = [100.0, 95.5, 94.5]  # never bounces back up trail_buy_pct% off the running low
    idx = _write_low_high_csv(ticker, lows, highs)
    try:
        pending = {
            'ticker': ticker,
            'signal_time': (idx[0] - timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S'),
            'node': {'trail_buy_pct': 5.0},
        }
        met, _ = A._trailing_buy_status(pending)
        assert met is False
    finally:
        cleanup_csv(ticker)


def test_trailing_buy_status_no_cache_returns_none():
    cleanup_csv('TEST_TBSTATUS_NOCACHE')
    pending = {
        'ticker': 'TEST_TBSTATUS_NOCACHE',
        'signal_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'node': {'trail_buy_pct': 5.0},
    }
    met, trigger = A._trailing_buy_status(pending)
    assert met is None
    assert trigger is None
