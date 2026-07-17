"""Tests for the schwab_client/schwab_safety wiring into the live BUY/SELL flow
(signals_notify.py) -- automated order placement and the opt-in auto-fill-
detection toggle. Mirrors tests/test_schwab_safety.py's isolated-DB style: no
real Schwab API calls (dry_run stays True) and no real Slack posts."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import signals_notify
import schwab_safety
import schwab_client

TICKER = 'TEST_AUTOMATION'

_IN_WINDOW_TIME = datetime(2026, 7, 15, 10, 30)


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    monkeypatch.setattr(schwab_safety, 'STATE_PATH', tmp_path / "schwab_order_counts.json")
    monkeypatch.setattr(schwab_safety, 'KILL_SWITCH_PATH', tmp_path / "schwab_kill_switch.json")
    monkeypatch.setattr(schwab_safety, 'TICKER_AUTOMATION_PATH', tmp_path / "schwab_ticker_automation.json")
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: _IN_WINDOW_TIME)
    monkeypatch.setattr(schwab_client, '_post_message', lambda *a, **kw: (None, None))
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                         stop_loss=5, max_hold_hours=7, mode='live',
                         trail_buy_pct=1.0, trail_pct=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account = 'ira' WHERE ticker = ?", (TICKER,))
        c.commit()

    yield

    tmp_db_path = Path(tmp_db.name)
    if tmp_db_path.exists():
        tmp_db_path.unlink()


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _sig(price=50.0):
    return {
        'ticker': TICKER, 'current_price': price, 'z_score': -1.4,
        'last_bar': _IN_WINDOW_TIME, 'lower_band': price - 1.0,
        'sma': price + 2.0, 'std': 1.0, 'hurst': None, 'adf_p': None, 'window': 20,
    }


def _pending():
    return [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER][0]


# ---------------------------------------------------------------------------
# Automated placement
# ---------------------------------------------------------------------------

def test_automated_buy_placed_in_window(env):
    signals_notify.notify_buy_signal(_node(), _sig())
    pending = _pending()
    assert pending['order_placed'] == 1


def test_automated_buy_falls_back_outside_scope(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', set())
    signals_notify.notify_buy_signal(_node(), _sig())
    pending = _pending()
    assert pending['order_placed'] == 0


def test_automated_buy_falls_back_outside_signal_window(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 7, 15, 12, 0))
    signals_notify.notify_buy_signal(_node(), _sig())
    pending = _pending()
    assert pending['order_placed'] == 0


def test_automated_buy_falls_back_when_ticker_paused(env):
    schwab_safety.pause_ticker_automation(TICKER, reason="test pause")
    signals_notify.notify_buy_signal(_node(), _sig())
    pending = _pending()
    assert pending['order_placed'] == 0


def test_automated_sell_placed_and_marks_order_placed(env):
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    pos = signals_db.get_open_position(TICKER)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira' WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    signals_notify.notify_trailing_activated(pos, current_price=52.0)
    updated = [p for p in signals_db.get_open_positions() if p['ticker'] == TICKER][0]
    assert updated['trail_state'].get('order_placed') is True


def test_automated_sell_falls_back_outside_scope(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', set())
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    pos = signals_db.get_open_position(TICKER)
    signals_notify.notify_trailing_activated(pos, current_price=52.0)
    updated = [p for p in signals_db.get_open_positions() if p['ticker'] == TICKER][0]
    assert not updated['trail_state'].get('order_placed')


# ---------------------------------------------------------------------------
# Auto-fill-detection toggle (default off)
# ---------------------------------------------------------------------------

def test_auto_fill_detection_defaults_off(env):
    assert schwab_safety.auto_fill_detection_enabled(TICKER) is False


def test_check_auto_fills_noop_when_toggle_off(env, monkeypatch):
    signals_notify.notify_buy_signal(_node(), _sig())
    assert _pending()['order_placed'] == 1

    monkeypatch.setattr(schwab_client, 'get_filled_order',
                         lambda account, ticker, side: {'price': 51.0, 'quantity': 100})
    signals_notify.check_auto_fills(signals_db.get_open_positions())

    # still pending -- toggle is off, so no auto-detected fill should have landed
    assert _pending()['order_placed'] == 1
    assert signals_db.get_open_position(TICKER) is None


def test_check_auto_fills_records_buy_fill_when_enabled(env, monkeypatch):
    signals_notify.notify_buy_signal(_node(), _sig())
    assert _pending()['order_placed'] == 1
    schwab_safety.enable_auto_fill_detection(TICKER)

    monkeypatch.setattr(schwab_client, 'get_filled_order',
                         lambda account, ticker, side: {'price': 51.0, 'quantity': 100})
    signals_notify.check_auto_fills(signals_db.get_open_positions())

    pos = signals_db.get_open_position(TICKER)
    assert pos is not None
    assert pos['entry_price'] == 51.0
    assert pos['shares'] == 100
    assert [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER] == []


def test_check_auto_fills_records_sell_fill_when_enabled(env, monkeypatch):
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    pos = signals_db.get_open_position(TICKER)
    state = {
        'trailing': True, 'order_placed': True,
        'exit_pending': {'reason': 'TRAIL', 'current_price': 54.0, 'target_price': 54.0,
                          'reminder_channel': None, 'reminder_ts': None, 'reminder_count': 0,
                          'last_reminder_at': now.strftime('%Y-%m-%d %H:%M:%S')},
    }
    signals_db.update_position_trail_state(pos['id'], state)
    schwab_safety.enable_auto_fill_detection(TICKER)

    monkeypatch.setattr(schwab_client, 'get_filled_order',
                         lambda account, ticker, side: {'price': 53.5, 'quantity': 100})
    signals_notify.check_auto_fills(signals_db.get_open_positions())

    assert signals_db.get_open_positions() == []
