"""Tests for the Bolt interactive handlers in signals_handlers.py (BUY
confirmation buttons/modals) -- specifically the coverage-accountability
branches that had zero test coverage as of 2026-07-26 (stale_buy_button_guard,
buy_buttons_resolve_correct_node, manual_buy_confirmation_account). The module
only defines its handler functions when cfg.SOCKET_MODE was True at import
time (real Slack creds in .env); call them directly rather than through Bolt's
dispatch, mirroring how live_sim.py exercises real handler logic without a
live Socket Mode connection. Mirrors tests/test_schwab_safety.py's isolated-DB
style: no real Schwab API calls, no real Slack posts (conftest's autouse
fixture patches signals_handlers._post_message)."""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import signals_handlers
import schwab_safety

TICKER = 'TEST_HANDLERS'


@pytest.fixture
def env(monkeypatch, tmp_path):
    if not hasattr(signals_handlers, 'handle_entry_price'):
        pytest.skip("signals_handlers handlers only defined when cfg.SOCKET_MODE was True at import time")
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', set())
    signals_db.ensure_tables()
    yield
    os.unlink(tmp_db.name)


class _FakeClient:
    def __init__(self):
        self.updates = []

    def chat_update(self, **kw):
        self.updates.append(kw)


def _add_node(version='test', account='ira'):
    signals_db.add_node(TICKER, 'ZScoreBreakout', version, window=20, take_profit=10,
                         stop_loss=5, max_hold_hours=56, mode='live', account=account)
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER and n['version'] == version][0]


def _pending(node, price=50.0):
    sig = {'current_price': price, 'last_bar': datetime(2026, 7, 15, 10, 30)}
    signals_db.add_pending_buy(node, sig, channel='C123', ts='111.222')


def _entry_price_body(node, exec_price, signal_price=50.0):
    data = {'node': node, 'signal_price': signal_price, 'signal_time': '2026-07-15 10:30:00'}
    return {
        'view': {
            'private_metadata': json.dumps({'data': data, 'channel': 'C123', 'ts': '111.222'}),
            'state': {'values': {'price_block': {'price_input': {'value': str(exec_price)}}}},
        }
    }


def _ack():
    return None


def test_stale_buy_button_guard_logs_coverage_event(env):
    node = _add_node()
    # Deliberately no pending_buys row -- simulates a click on an already-
    # resolved (skipped/cleared) or stale duplicate confirmation.
    body = _entry_price_body(node, exec_price=51.0)
    signals_handlers.handle_entry_price(_ack, body, _FakeClient())

    assert signals_db.get_open_position(TICKER) is None
    events = signals_db.get_coverage_events(scenario_key="stale_buy_button_guard")
    assert len(events) == 1
    assert events[0]['result'] == "guard_fired"
    assert events[0]['detail'] == "entry_price"


def test_buy_buttons_resolve_correct_node_logs_coverage_event(env):
    node_a = _add_node(version='test_a')
    node_b = _add_node(version='test_b')
    _pending(node_a)
    _pending(node_b)

    body = _entry_price_body(node_a, exec_price=51.0)
    signals_handlers.handle_entry_price(_ack, body, _FakeClient())

    events = signals_db.get_coverage_events(scenario_key="buy_buttons_resolve_correct_node")
    assert len(events) == 1
    assert events[0]['result'] == "resolved"
    assert events[0]['node_id'] == node_a['id']

    # The confirmation must have opened a position for node_a specifically,
    # not node_b -- the real thing this scenario is meant to prove.
    remaining_pending = [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER]
    assert len(remaining_pending) == 1
    assert remaining_pending[0]['node']['id'] == node_b['id']


def test_manual_buy_confirmation_account_logs_coverage_event(env):
    node = _add_node(account='ira')
    _pending(node)

    body = _entry_price_body(node, exec_price=51.0)
    signals_handlers.handle_entry_price(_ack, body, _FakeClient())

    assert signals_db.get_open_position(TICKER) is not None
    events = signals_db.get_coverage_events(scenario_key="manual_buy_confirmation_account")
    assert len(events) == 1
    assert events[0]['result'] == "opened"
    assert events[0]['detail'] == "account='ira'"


def test_manual_buy_confirmation_account_no_account_logs_unattributed(env):
    node = _add_node(account=None)
    _pending(node)

    body = _entry_price_body(node, exec_price=51.0)
    signals_handlers.handle_entry_price(_ack, body, _FakeClient())

    events = signals_db.get_coverage_events(scenario_key="manual_buy_confirmation_account")
    assert len(events) == 1
    assert events[0]['result'] == "no_account"
    assert events[0]['mode'] == "unattributed"
