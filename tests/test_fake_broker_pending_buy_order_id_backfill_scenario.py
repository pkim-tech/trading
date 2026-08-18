"""Fake-broker coverage for the 2026-08-18 pending_buys.order_id backfill fix
(docs/backlog_cache.md KEY item, raised 2026-08-17 evening after the
SOXS/ira/wl_id=206 outage). The manual 'Trailing Buy Order Placed' Slack flow
(signals_handlers.handle_trail_buy_order_placed) used to set order_placed=1
without ever capturing the real broker order_id, permanently blocking
check_buy_reminders' real-fill re-verification for every manually-placed
trailing buy. Both new call sites of signals_notify._backfill_pending_buy_
order_id are exercised here: press-time (the handler itself) and the
periodic reminder-loop backstop (check_buy_reminders), plus the non-happy-
path outcomes (not_found, ambiguous -> incident+alert).

2026-08-18 paired-review rework: added fingerprint-check coverage (a single
resting order is no longer trusted purely on "instruction == BUY" -- see
_order_id_backfill_fingerprint_ok), row-scoped write coverage (a core and a
drought_overlay pending_buys row sharing one wl_id must never cross-
contaminate each other's order_id), and ambiguous-alert throttling coverage.

Every node here uses starting_notional=5100 with signal_price=50.0 and the
default trail_buy_pct=1.0/pad_pct=1.0 so buy_order_sizing's worst-case
formula (target_notional // (price * (1 + (trail_buy_pct + pad_pct)/100)))
comes out to an exact, easy round-trip: 5100 // (50 * 1.02) == 100 shares --
matching every seed_resting_order(quantity=100) call below."""
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

TICKER = 'TEST_ORDER_ID_BACKFILL'
STARTING_NOTIONAL = 5100  # -> 100 expected shares at price=50.0, trail_buy_pct=1.0, pad_pct=1.0


@pytest.fixture
def env(monkeypatch, tmp_path):
    if not hasattr(signals_handlers, 'handle_trail_buy_order_placed'):
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
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 8, 18, 10, 30))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    # Ambiguous-alert throttle is a module-level dict, keyed by pending_id --
    # pending_buys.id restarts from 1 in every fresh temp DB, so a leftover
    # cooldown from an earlier test in the same pytest process could silently
    # suppress a real alert this test expects (same pattern as
    # signals_notify._ENTRY_ABANDON_ALERTED.clear() in test_entry_abandon.py).
    signals_notify._ORDER_ID_BACKFILL_AMBIGUOUS_ALERTED.clear()

    signals_db.ensure_tables()
    schwab_safety.reload_accounts()
    yield

    os.unlink(tmp_db.name)


class _FakeClient:
    def __init__(self):
        self.updates = []

    def chat_update(self, **kw):
        self.updates.append(kw)


def _ack():
    return None


def _add_node(version='test', account='ira', starting_notional=STARTING_NOTIONAL):
    signals_db.add_node(
        ticker=TICKER, strategy='TrailingBothZScoreBreakout', version=version,
        window=20, take_profit=10, stop_loss=5, max_hold_hours=56,
        state='live', account=account, trail_buy_pct=1.0, trail_pct=1.0,
        starting_notional=starting_notional,
    )
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER and n['version'] == version][0]


def _pending(node, price=50.0, channel='C123', ts='111.222', position_source='core'):
    sig = {'current_price': price, 'last_bar': datetime(2026, 8, 18, 10, 30)}
    signals_db.add_pending_buy(node, sig, channel=channel, ts=ts, position_source=position_source)


def _click_body(node, signal_price=50.0, channel='C123', ts='111.222'):
    data = {'node': node, 'signal_price': signal_price, 'signal_time': '2026-08-18 10:30:00'}
    return {
        'actions': [{'value': json.dumps(data)}],
        'channel': {'id': channel},
        'message': {'ts': ts},
    }


def test_press_time_backfill_finds_the_one_resting_order(env, fake_broker):
    """The common/expected case: user places the order at the broker, THEN
    taps 'Trailing Buy Order Placed' -- the order is already resting by
    press time, so the handler should find, fingerprint-confirm, and store
    its real order_id."""
    node = _add_node()
    _pending(node)
    real_order_id = fake_broker.seed_resting_order(
        node['account'], TICKER, order_type='TRAILING_STOP', side='BUY', quantity=100, trail_offset=1.0)

    body = _click_body(node)
    signals_handlers.handle_trail_buy_order_placed(_ack, body, _FakeClient())

    pending = signals_db.get_pending_buy_by_wl_id(node['id'])
    assert pending['order_placed'] == 1
    assert pending['order_id'] == real_order_id

    events = signals_db.get_coverage_events(scenario_key="pending_buy_order_id_backfill")
    assert len(events) == 1
    assert events[0]['result'] == "backfilled"
    assert events[0]['node_id'] == node['id']
    assert signals_db.get_incidents(open_only=True) == []


def test_press_time_not_found_leaves_order_id_null_no_incident(env, fake_broker):
    """No resting order visible yet at press time (e.g. a brief broker-side
    lag) -- benign, no incident/alert, order_id stays NULL for the periodic
    backstop to retry later."""
    node = _add_node()
    _pending(node)
    # Deliberately no seed_resting_order call.

    body = _click_body(node)
    signals_handlers.handle_trail_buy_order_placed(_ack, body, _FakeClient())

    pending = signals_db.get_pending_buy_by_wl_id(node['id'])
    assert pending['order_placed'] == 1
    assert pending['order_id'] is None

    events = signals_db.get_coverage_events(scenario_key="pending_buy_order_id_backfill")
    assert len(events) == 1
    assert events[0]['result'] == "not_found"
    assert signals_db.get_incidents(open_only=True) == []


def test_press_time_fingerprint_rejects_unrelated_resting_order_as_not_found(env, fake_broker):
    """A single resting BUY order exists, but its quantity is nowhere near
    what buy_order_sizing would have produced for this node/signal -- e.g. a
    human's own long-standing discretionary order in the same ticker/account.
    resolve_resting_buy_orders' 'at most one resting BUY' guarantee only
    covers DAEMON-placed orders, not this. Must be treated as not_found, not
    silently accepted as ground truth (2026-08-18 paired review finding)."""
    node = _add_node()
    _pending(node)
    # Expected ~100 shares (see STARTING_NOTIONAL docstring) -- 10 shares is
    # miles outside DUPLICATE_ORDER_QUANTITY_TOLERANCE_PCT (5%).
    fake_broker.seed_resting_order(
        node['account'], TICKER, order_type='TRAILING_STOP', side='BUY', quantity=10, trail_offset=1.0)

    body = _click_body(node)
    signals_handlers.handle_trail_buy_order_placed(_ack, body, _FakeClient())

    pending = signals_db.get_pending_buy_by_wl_id(node['id'])
    assert pending['order_id'] is None, "a fingerprint-mismatched single order must not be trusted"

    events = signals_db.get_coverage_events(scenario_key="pending_buy_order_id_backfill")
    assert len(events) == 1
    assert events[0]['result'] == "not_found"
    assert signals_db.get_incidents(open_only=True) == []


def test_press_time_ambiguous_multiple_resting_orders_raises_incident_and_alert(env, fake_broker, monkeypatch):
    """>1 fingerprint-confirmed resting BUY order for the same ticker/account
    is genuinely anomalous -- must not silently pick one; must raise a
    trading_incidents row (and a Slack alert)."""
    node = _add_node()
    _pending(node)
    id_a = fake_broker.seed_resting_order(
        node['account'], TICKER, order_type='TRAILING_STOP', side='BUY', quantity=100, trail_offset=1.0)
    id_b = fake_broker.seed_resting_order(
        node['account'], TICKER, order_type='TRAILING_STOP', side='BUY', quantity=100, trail_offset=1.0)

    posted = []
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda *a, **kw: posted.append((a, kw)) or (None, None))

    body = _click_body(node)
    signals_handlers.handle_trail_buy_order_placed(_ack, body, _FakeClient())

    pending = signals_db.get_pending_buy_by_wl_id(node['id'])
    assert pending['order_id'] is None, "must not guess when the match is ambiguous"

    events = signals_db.get_coverage_events(scenario_key="pending_buy_order_id_backfill")
    assert len(events) == 1
    assert events[0]['result'] == "ambiguous"

    incidents = signals_db.get_incidents(open_only=True)
    assert len(incidents) == 1
    assert incidents[0]['ticker'] == TICKER
    assert incidents[0]['node_id'] == node['id']
    assert len(posted) == 1, "an ambiguous match must also raise a real-time Slack alert"


def test_ambiguous_alert_throttled_across_repeated_reminder_cycles(env, fake_broker, monkeypatch):
    """The periodic backstop re-enters check_buy_reminders every cycle -- an
    un-throttled ambiguous branch would raise a fresh trading_incidents row +
    Slack alert every single cycle indefinitely. Confirms a second backfill
    attempt on the same still-ambiguous row within the cooldown window is
    silent (no new incident, no new Slack post), matching
    _throttled_entry_abandon_alert's established pattern."""
    node = _add_node()
    _pending(node)
    fake_broker.seed_resting_order(
        node['account'], TICKER, order_type='TRAILING_STOP', side='BUY', quantity=100, trail_offset=1.0)
    fake_broker.seed_resting_order(
        node['account'], TICKER, order_type='TRAILING_STOP', side='BUY', quantity=100, trail_offset=1.0)

    posted = []
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda *a, **kw: posted.append((a, kw)) or (None, None))

    pending = signals_db.get_pending_buy_by_wl_id(node['id'])
    r1 = signals_notify._backfill_pending_buy_order_id(pending, source='reminder_backstop')
    r2 = signals_notify._backfill_pending_buy_order_id(pending, source='reminder_backstop')

    assert r1 is None and r2 is None
    events = signals_db.get_coverage_events(scenario_key="pending_buy_order_id_backfill")
    assert len(events) == 2, "both attempts still log their own coverage_events row"
    assert all(e['result'] == 'ambiguous' for e in events)

    incidents = signals_db.get_incidents(open_only=True)
    assert len(incidents) == 1, "the second attempt must be throttled, not raise a duplicate incident"
    assert len(posted) == 1, "the second attempt must not re-post to Slack within the cooldown"


def test_core_and_drought_rows_backfill_independently_no_cross_contamination(env, fake_broker):
    """A node can hold two simultaneous pending_buys rows sharing one wl_id
    (core + position_source='drought_overlay', see add_pending_buy).
    Backfilling the core row's order_id must never also stamp it onto the
    drought row (or vice versa) -- the pre-fix wl_id-keyed UPDATE did exactly
    that, and check_drought_handoff's Case A could then cancel_order using
    the wrong id, cancelling a real resting core BUY."""
    node = _add_node()
    _pending(node, channel='C_CORE', ts='1.000', position_source='core')
    _pending(node, channel='C_DROUGHT', ts='2.000', position_source='drought_overlay')

    core_order_id = fake_broker.seed_resting_order(
        node['account'], TICKER, order_type='TRAILING_STOP', side='BUY', quantity=100, trail_offset=1.0)

    core_pending = signals_db.get_pending_buy_by_channel_ts('C_CORE', '1.000')
    drought_pending = signals_db.get_pending_buy_by_channel_ts('C_DROUGHT', '2.000')
    assert core_pending['wl_id'] == drought_pending['wl_id'] == node['id']
    assert core_pending['position_source'] == 'core'
    assert drought_pending['position_source'] == 'drought_overlay'
    assert core_pending['id'] != drought_pending['id']

    result = signals_notify._backfill_pending_buy_order_id(core_pending, source='press_time')
    assert result == core_order_id

    core_after = signals_db.get_pending_buy_by_channel_ts('C_CORE', '1.000')
    drought_after = signals_db.get_pending_buy_by_channel_ts('C_DROUGHT', '2.000')
    assert core_after['order_id'] == core_order_id
    assert drought_after['order_id'] is None, "backfilling the core row must not stamp the drought row too"


def test_periodic_backstop_backfills_when_press_time_missed(env, fake_broker, monkeypatch):
    """Simulates a press-time miss (order_placed=1, order_id still NULL) and
    confirms check_buy_reminders' periodic backstop finds and backfills the
    order once it becomes visible, instead of leaving the row permanently
    unreconciled the way the pre-fix code did."""
    node = _add_node()
    _pending(node)
    signals_db.mark_pending_buy_placed_by_wl_id(node['id'])  # order_placed=1, order_id still NULL

    real_order_id = fake_broker.seed_resting_order(
        node['account'], TICKER, order_type='TRAILING_STOP', side='BUY', quantity=100, trail_offset=1.0)
    fake_broker.set_quote(TICKER, last=50.0, bid=49.9, ask=50.1)

    monkeypatch.setattr(signals_notify, 'BUY_REMINDER_MINUTES', 0)
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))

    signals_notify.check_buy_reminders()

    pending = signals_db.get_pending_buy_by_wl_id(node['id'])
    assert pending['order_id'] == real_order_id

    events = signals_db.get_coverage_events(scenario_key="pending_buy_order_id_backfill")
    assert len(events) == 1
    assert events[0]['result'] == "backfilled"
    assert events[0]['detail'] == f"source=reminder_backstop order_id={real_order_id}"


def test_periodic_backstop_skips_row_that_already_has_order_id(env, fake_broker, monkeypatch):
    """check_buy_reminders only calls the backfill for a row with order_placed=1
    and order_id still NULL -- a row that already carries a real order_id
    (from an earlier successful press-time or backstop backfill) must not
    trigger a second, wasted broker lookup on every subsequent reminder
    cycle."""
    node = _add_node()
    _pending(node)
    signals_db.mark_pending_buy_placed_by_wl_id(node['id'])
    real_order_id = fake_broker.seed_resting_order(
        node['account'], TICKER, order_type='TRAILING_STOP', side='BUY', quantity=100, trail_offset=1.0)
    signals_db.set_pending_buy_order_id_by_wl_id(node['id'], real_order_id)
    fake_broker.set_quote(TICKER, last=50.0, bid=49.9, ask=50.1)

    calls = []
    orig = signals_notify.schwab_client.resolve_resting_buy_orders

    def _tracking_resolve(*a, **kw):
        calls.append((a, kw))
        return orig(*a, **kw)

    monkeypatch.setattr(signals_notify.schwab_client, 'resolve_resting_buy_orders', _tracking_resolve)
    monkeypatch.setattr(signals_notify, 'BUY_REMINDER_MINUTES', 0)
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))
    # get_filled_order isn't seeded to return a fill here (order still
    # WORKING) -- fine, this test only cares whether the backfill lookup
    # itself was skipped, not the rest of the reminder flow.
    monkeypatch.setattr(signals_notify.schwab_client, 'get_filled_order', lambda *a, **kw: None)

    signals_notify.check_buy_reminders()

    assert calls == [], "a row that already has order_id must not trigger a fresh resolve_resting_buy_orders call"

    events = signals_db.get_coverage_events(scenario_key="pending_buy_order_id_backfill")
    assert events == [], "no backfill attempt should have been logged at all"
