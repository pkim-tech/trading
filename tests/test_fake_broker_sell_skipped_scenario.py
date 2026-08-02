"""fake_broker scenarios for the SELL-signal 'Skipped' button
(signals_handlers.handle_sell_skipped) -- built 2026-08-02 after a paired
independent+contextual Opus review of the handler's first version (which
called schwab_client.cancel_order unconditionally whenever exit_pending had
an order_id) found it could cancel a genuinely-armed position's ONLY real
protection: for a genuine TRAIL breach (not hold-time-forced),
_attempt_automated_exit_sell reuses the SAME order placed at the earlier arm
event (the position's standing trailing-sell, not a fresh order placed in
response to this specific alert) -- so cancelling it on Skip would leave the
position with zero broker protection while the alert claims 'no action
needed'. Zero fake_broker (or any other) coverage existed for this handler
before this file."""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import signals_config
import signals_db
import signals_handlers
import signals_notify
import schwab_safety

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_SELL_SKIPPED'


@pytest.fixture
def env(monkeypatch, tmp_path):
    if not hasattr(signals_handlers, 'handle_sell_skipped'):
        pytest.skip("signals_handlers handlers only defined when cfg.SOCKET_MODE was True at import time")

    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    monkeypatch.setattr(schwab_safety, 'STATE_PATH', tmp_path / "schwab_order_counts.json")
    monkeypatch.setattr(schwab_safety, 'KILL_SWITCH_PATH', tmp_path / "schwab_kill_switch.json")
    monkeypatch.setattr(schwab_safety, 'TICKER_AUTOMATION_PATH', tmp_path / "schwab_ticker_automation.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTOMATION_PATH', tmp_path / "schwab_node_automation.json")
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_node_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 7, 30, 10, 30))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, mode='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='soxl_ira' WHERE ticker=?", (TICKER,))
        c.commit()

    yield

    os.unlink(tmp_db.name)


class _FakeClient:
    def __init__(self):
        self.updates = []

    def chat_update(self, **kw):
        self.updates.append(kw)


def _ack():
    return None


def _skip_body(pos, reason, order_id):
    """Mirrors the real Skip button's value (signals_blocks.py:340-346),
    plus seeds exit_pending directly (as notify_sell_signal does) rather
    than going through the full alert-firing path."""
    state = dict(pos.get('trail_state') or {})
    state['exit_pending'] = {
        'reason': reason, 'current_price': 51.0, 'target_price': 50.0,
        'reminder_channel': 'C1', 'reminder_ts': '1.1', 'reminder_count': 0,
        'last_reminder_at': '2026-07-30 10:30:00', 'order_id': order_id,
    }
    if reason != 'TRAIL':
        state['exit_forced_by_hold_time'] = False
    signals_db.update_position_trail_state(pos['id'], state)
    value = json.dumps({
        "type": "sell", "position_id": pos['id'], "ticker": TICKER,
        "current_price": 51.0, "entry_price": 50.0, "reason": reason,
    })
    return {
        "actions": [{"value": value}],
        "channel": {"id": "C1"},
        "message": {"ts": "1.1"},
    }


def _open_position(fake_broker):
    now = datetime.now()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    node = [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='soxl_ira' WHERE ticker=?", (TICKER,))
        c.commit()
    return signals_db.get_open_position(TICKER)


def test_skip_on_genuine_trail_breach_does_not_cancel_the_standing_protection(env, fake_broker):
    """The most severe finding from the 2026-08-02 paired review: a genuine
    TRAIL breach's exit_pending.order_id IS the arm-time trailing-sell, the
    position's only real protection. Skip must leave it resting."""
    pos = _open_position(fake_broker)
    trailing_sell_id = fake_broker.seed_resting_order(
        'soxl_ira', TICKER, 'TRAILING_STOP', 'SELL', 100, stop_price=51.0)
    state = dict(pos.get('trail_state') or {})
    state['trailing'] = True
    state['exit_order_id'] = trailing_sell_id
    signals_db.update_position_trail_state(pos['id'], state)
    pos = signals_db.get_open_position(TICKER)

    body = _skip_body(pos, reason='TRAIL', order_id=trailing_sell_id)
    signals_handlers.handle_sell_skipped(_ack, body, _FakeClient())

    assert fake_broker.orders[trailing_sell_id]['status'] == 'WORKING'
    updated = signals_db.get_open_position(TICKER)
    assert updated['trail_state'].get('exit_pending') is None
    assert updated['trail_state'].get('exit_order_id') == trailing_sell_id


def test_skip_on_fresh_time_exit_cancels_the_real_order(env, fake_broker):
    """TIME/SL/TP exits place a fresh market-sell in direct response to the
    signal -- Skip meaning 'keep the position open' should cancel it, unlike
    the standing-protection TRAIL case above."""
    pos = _open_position(fake_broker)
    exit_order_id = fake_broker.seed_resting_order(
        'soxl_ira', TICKER, 'MARKET', 'SELL', 100)

    body = _skip_body(pos, reason='TIME', order_id=exit_order_id)
    signals_handlers.handle_sell_skipped(_ack, body, _FakeClient())

    assert fake_broker.orders[exit_order_id]['status'] == 'CANCELED'
    updated = signals_db.get_open_position(TICKER)
    assert updated['trail_state'].get('exit_pending') is None


def test_skip_leaves_exit_pending_when_order_already_filled(env, fake_broker):
    """If the exit order already filled (a real race, or it filled before
    Skip was tapped), exit_pending must stay so check_own_sell_fills'
    existing polling reconciles the real close -- not this handler guessing
    'position kept open' while the shares are actually gone."""
    pos = _open_position(fake_broker)
    exit_order_id = fake_broker.seed_resting_order(
        'soxl_ira', TICKER, 'MARKET', 'SELL', 100)
    fake_broker.orders[exit_order_id]['status'] = 'FILLED'

    body = _skip_body(pos, reason='TIME', order_id=exit_order_id)
    signals_handlers.handle_sell_skipped(_ack, body, _FakeClient())

    updated = signals_db.get_open_position(TICKER)
    assert updated['trail_state'].get('exit_pending') is not None
    assert updated['trail_state']['exit_pending']['order_id'] == exit_order_id


def test_skip_on_hold_time_forced_trail_cancels_and_clears_exit_order_id(env, fake_broker):
    """A hold-time-forced TRAIL exit's resting order IS a fresh replace
    (force-market-sold once max_hold_hours expired while armed) -- Skip
    should cancel it like any other fresh exit, and must clear
    exit_order_id/hold_time_replaced so the next forced-exit attempt doesn't
    try to replace an order that's already cancelled."""
    pos = _open_position(fake_broker)
    exit_order_id = fake_broker.seed_resting_order(
        'soxl_ira', TICKER, 'MARKET', 'SELL', 100)
    state = dict(pos.get('trail_state') or {})
    state['exit_forced_by_hold_time'] = True
    state['hold_time_replaced'] = True
    state['exit_order_id'] = exit_order_id
    signals_db.update_position_trail_state(pos['id'], state)
    pos = signals_db.get_open_position(TICKER)

    body = _skip_body(pos, reason='TRAIL', order_id=exit_order_id)
    signals_handlers.handle_sell_skipped(_ack, body, _FakeClient())

    assert fake_broker.orders[exit_order_id]['status'] == 'CANCELED'
    updated = signals_db.get_open_position(TICKER)
    assert updated['trail_state'].get('exit_order_id') is None
    assert updated['trail_state'].get('hold_time_replaced') is False
