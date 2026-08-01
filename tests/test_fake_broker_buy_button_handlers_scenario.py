"""Buy button handler scenarios with fake_broker coverage -- extending
tests/test_signals_handlers.py to prove real stop-loss placement and automated
fill reconciliation work end-to-end when the ticker is in AUTOMATION_ENABLED_TICKERS.

The old test file set AUTOMATION_ENABLED_TICKERS=set() to avoid the SL-placement
side effect; this file does the opposite (include the ticker) so _place_stop_loss_for_position
actually runs against fake_broker state, proving the resting STOP order really reaches
the broker. Also adds coverage for buy_fill_reconciles_correct_node (_reconcile_buy_fill path)
with multiple pending buys, testing node disambiguation via wl_id."""
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
from schwab_safety import AccountLimits
import signals_notify
import schwab_safety
import strategies

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_BUY_HANDLERS'


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Set up isolated DB and enable AUTOMATION for the test ticker so SL
    placement actually runs (unlike test_signals_handlers.py which set it empty)."""
    if not hasattr(signals_handlers, 'handle_entry_price'):
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

    # KEY DIFFERENCE FROM OLD TESTS: include the ticker in AUTOMATION scope so
    # _place_stop_loss_for_position actually runs and places a real STOP order
    # against fake_broker state, proving the integration works.
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})

    # 10:30 ET -- inside the real (10:25-10:40) signal window for consistency
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 7, 30, 10, 30))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])

    signals_db.ensure_tables()
    yield

    os.unlink(tmp_db.name)


class _FakeClient:
    """Mock Slack client (conftest patches real _post_message, so this is
    only used to track Slack update calls via .chat_update)."""
    def __init__(self):
        self.updates = []

    def chat_update(self, **kw):
        self.updates.append(kw)


def _ack():
    return None


def _add_node(version='test', account='ira', strategy='ZScoreBreakout',
              trail_buy_pct=None, trail_pct=None, fixed_sl_override=None):
    """Add a watch_list node with optional trailing/SL params."""
    kwargs = {
        'ticker': TICKER,
        'strategy': strategy,
        'version': version,
        'window': 20,
        'take_profit': 10,
        'stop_loss': 5,
        'max_hold_hours': 56,
        'mode': 'live',
        'account': account,
    }
    if trail_buy_pct is not None:
        kwargs['trail_buy_pct'] = trail_buy_pct
    if trail_pct is not None:
        kwargs['trail_pct'] = trail_pct
    if fixed_sl_override is not None:
        kwargs['fixed_sl_override'] = fixed_sl_override

    signals_db.add_node(**kwargs)
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER and n['version'] == version][0]


def _pending(node, price=50.0):
    """Create a pending buy for a node."""
    sig = {'current_price': price, 'last_bar': datetime(2026, 7, 30, 10, 30)}
    signals_db.add_pending_buy(node, sig, channel='C123', ts='111.222')


def _entry_price_body(node, exec_price, signal_price=50.0):
    """Build a Slack modal submission body for handle_entry_price."""
    data = {'node': node, 'signal_price': signal_price, 'signal_time': '2026-07-30 10:30:00'}
    return {
        'view': {
            'private_metadata': json.dumps({'data': data, 'channel': 'C123', 'ts': '111.222'}),
            'state': {'values': {'price_block': {'price_input': {'value': str(exec_price)}}}},
        }
    }


def test_stale_buy_button_guard_logs_coverage_and_places_no_stop(env, fake_broker, monkeypatch):
    """Test that a stale click (no matching pending row) is guarded and no
    position/stop is created. The key difference vs test_signals_handlers.py:
    we verify no STOP order is placed (even though AUTOMATION_ENABLED_TICKERS
    includes the ticker)."""
    node = _add_node()
    # Deliberately no pending_buys row -- simulates a click on an already-
    # resolved (skipped/cleared) or stale duplicate confirmation.
    fake_broker.set_quote(TICKER, last=50.0, bid=49.9, ask=50.1)
    fake_broker.set_cash_balance(node.get('account') or 'ira', 50000.0)

    body = _entry_price_body(node, exec_price=51.0)
    signals_handlers.handle_entry_price(_ack, body, _FakeClient())

    # Guard should have fired
    assert signals_db.get_open_position(TICKER) is None
    events = signals_db.get_coverage_events(scenario_key="stale_buy_button_guard")
    assert len(events) == 1
    assert events[0]['result'] == "guard_fired"
    assert events[0]['detail'] == "entry_price"

def test_buy_buttons_resolve_correct_node_logs_coverage_and_opens_position(env, fake_broker, monkeypatch):
    """Test that when 2+ nodes have pending buys for the same ticker, the
    button confirmation opens the position for the RIGHT node (not the other one).
    Also verifies the SL order is placed for the chosen node via fake_broker."""
    node_a = _add_node(version='test_a')
    node_b = _add_node(version='test_b')
    _pending(node_a)
    _pending(node_b)

    fake_broker.set_quote(TICKER, last=51.0, bid=50.9, ask=51.1)
    fake_broker.set_cash_balance(node_a.get('account') or 'ira', 50000.0)

    body = _entry_price_body(node_a, exec_price=51.0)
    signals_handlers.handle_entry_price(_ack, body, _FakeClient())

    # Coverage event should fire for the correct-node disambiguation
    events = signals_db.get_coverage_events(scenario_key="buy_buttons_resolve_correct_node")
    assert len(events) == 1
    assert events[0]['result'] == "resolved"
    assert events[0]['node_id'] == node_a['id']

    # Position should be opened for node_a only
    pos = signals_db.get_open_position(TICKER)
    assert pos is not None
    assert pos['wl_id'] == node_a['id'], "Position should be linked to node_a"

    # Remaining pending buy should still be for node_b
    remaining_pending = [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER]
    assert len(remaining_pending) == 1
    assert remaining_pending[0]['node']['id'] == node_b['id']


def test_manual_buy_confirmation_account_logs_coverage_and_places_stop(env, fake_broker, monkeypatch):
    """Test that handle_entry_price logs account attribution and places a STOP
    order when the ticker is in AUTOMATION_ENABLED_TICKERS."""
    node = _add_node(account='ira')
    _pending(node)

    fake_broker.set_quote(TICKER, last=51.0, bid=50.9, ask=51.1)
    fake_broker.set_cash_balance('ira', 50000.0)

    body = _entry_price_body(node, exec_price=51.0)
    signals_handlers.handle_entry_price(_ack, body, _FakeClient())

    # Position should be opened
    pos = signals_db.get_open_position(TICKER)
    assert pos is not None
    assert pos['shares'] > 0

    # Coverage event should fire with account attribution
    events = signals_db.get_coverage_events(scenario_key="manual_buy_confirmation_account")
    assert len(events) == 1
    assert events[0]['result'] == "opened"
    assert events[0]['detail'] == "account='ira'"



def test_buy_fill_reconciles_correct_node_with_multiple_pending(env, fake_broker, monkeypatch):
    """Test that _reconcile_buy_fill correctly disambiguates between multiple
    pending buys for the same ticker using the wl_id hint, opening a position
    for the correct node and placing its STOP order. This is the 4th scenario_key."""
    node_a = _add_node(version='test_a', strategy='TrailingBothZScoreBreakout',
                       trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0)
    node_b = _add_node(version='test_b', strategy='TrailingBothZScoreBreakout',
                       trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0)

    # Create pending buys for both nodes
    sig = {'current_price': 50.0, 'last_bar': datetime(2026, 7, 30, 10, 30)}
    signals_db.add_pending_buy(node_a, sig, channel='C123', ts='111.222', order_id=8888888888)
    signals_db.add_pending_buy(node_b, sig, channel='C124', ts='111.223', order_id=8888888889)
    signals_db.mark_pending_buy_placed_by_wl_id(node_a['id'])
    signals_db.mark_pending_buy_placed_by_wl_id(node_b['id'])

    # Set up fake broker quotes and cash
    fill_price = 49.5
    fake_broker.set_quote(TICKER, last=fill_price, bid=fill_price, ask=fill_price + 0.01)
    fake_broker.set_cash_balance(node_a.get('account') or 'ira', 50000.0)

    # Reconcile a fill against node_a specifically (via wl_id)
    filled_shares = 50.0
    signals_notify._reconcile_buy_fill(TICKER, fill_price=fill_price, filled_shares=filled_shares,
                                        wl_id=node_a['id'])

    # Coverage event should show correct node was resolved
    events = signals_db.get_coverage_events(scenario_key="buy_fill_reconciles_correct_node")
    assert len(events) == 1, f"expected one event, got {len(events)}: {events}"
    assert events[0]['result'] == "resolved", f"expected result='resolved', got {events[0]}"
    assert events[0]['node_id'] == node_a['id'], (
        f"event should resolve to node_a ({node_a['id']}), got {events[0]['node_id']}"
    )

    # Position should be opened for node_a, NOT node_b
    pos_a = signals_db.get_open_position_by_wl_id(node_a['id'])
    pos_b = signals_db.get_open_position_by_wl_id(node_b['id'])
    assert pos_a is not None, "node_a should have an open position"
    assert pos_b is None, "node_b should NOT have an open position"
    assert pos_a['shares'] >= filled_shares

    # node_a's pending row should be cleared; node_b's should still exist
    all_pending = signals_db.get_pending_buys()
    pending_a = [p for p in all_pending if p['node']['id'] == node_a['id']]
    pending_b = [p for p in all_pending if p['node']['id'] == node_b['id']]
    assert len(pending_a) == 0, "node_a's pending row should be cleared after reconcile"
    assert len(pending_b) == 1, "node_b's pending row should still exist"


