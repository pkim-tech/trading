"""Tests for signals_notify.check_live_state_reconciliation -- the detection-
only live-state reconciliation check (backlog 2026-07-21, automation_
principles.md #1/#5). Never places an order; only verifies the right Slack
alert text fires (or doesn't) for each mismatch shape."""
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

TICKER = 'TEST_RECONCILE'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    posted = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: posted.append(a[0] if a else kw.get('text')))
    signals_notify._RECONCILE_ALERTED.clear()

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                         stop_loss=5, max_hold_hours=7, mode='live',
                         trail_buy_pct=1.0, trail_pct=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account = 'ira' WHERE ticker = ?", (TICKER,))
        c.commit()

    yield posted

    tmp_db_path = Path(tmp_db.name)
    if tmp_db_path.exists():
        tmp_db_path.unlink()


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _open_pos(shares=100):
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=shares)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira' WHERE ticker=?", (TICKER,))
        c.commit()
    return signals_db.get_open_position(TICKER)


def test_no_alert_when_state_matches(env, monkeypatch):
    pos = _open_pos(shares=100)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    signals_notify.check_live_state_reconciliation([pos])
    assert env == []


def test_alerts_on_share_count_mismatch(env, monkeypatch):
    pos = _open_pos(shares=100)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 80.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    signals_notify.check_live_state_reconciliation([pos])
    assert len(env) == 1
    assert 'share-count' in env[0] or 'mismatch' in env[0]
    assert '80' in env[0] and '100' in env[0]


def test_share_count_mismatch_alert_rate_limited(env, monkeypatch):
    pos = _open_pos(shares=100)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 80.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    signals_notify.check_live_state_reconciliation([pos])
    signals_notify.check_live_state_reconciliation([pos])
    assert len(env) == 1  # second call suppressed by the 15-min cooldown


def test_alerts_on_missing_sl_order(env, monkeypatch):
    pos = _open_pos(shares=100)
    signals_db.set_sl_order_id(TICKER, 12345)
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])  # no resting SELL order
    signals_notify.check_live_state_reconciliation([pos])
    assert len(env) == 1
    assert 'SL' in env[0] or 'stop' in env[0].lower()


def test_no_alert_when_sl_order_actually_resting(env, monkeypatch):
    pos = _open_pos(shares=100)
    signals_db.set_sl_order_id(TICKER, 12345)
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    resting_sell_order = [{'orderLegCollection': [
        {'instruction': 'SELL', 'instrument': {'symbol': TICKER}}
    ]}]
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: resting_sell_order)
    signals_notify.check_live_state_reconciliation([pos])
    assert env == []


def test_alerts_on_missing_trailing_sell_order(env, monkeypatch):
    pos = _open_pos(shares=100)
    signals_db.update_position_trail_state(pos['id'], {'trailing': True, 'order_placed': True})
    # trail_state is JSON-parsed by get_open_positions() (the list form run_loop actually
    # passes in), unlike get_open_position()'s raw string -- match the real call site.
    pos = [p for p in signals_db.get_open_positions() if p['ticker'] == TICKER][0]
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    signals_notify.check_live_state_reconciliation([pos])
    assert len(env) == 1
    assert 'trailing-sell' in env[0]


def test_no_alert_outside_automation_scope(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', set())
    pos = _open_pos(shares=100)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 80.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    signals_notify.check_live_state_reconciliation([pos])
    assert env == []


def test_fetch_failure_skips_position_without_raising(env, monkeypatch):
    pos = _open_pos(shares=100)

    def _raise(account, ticker):
        raise RuntimeError("network error")
    monkeypatch.setattr(schwab_client, 'get_real_position', _raise)
    signals_notify.check_live_state_reconciliation([pos])  # must not raise
    assert env == []
