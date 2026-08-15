"""Corrected Stage A: a CONFIRMED real fill that gets dropped by the
auto-fill-detection opt-in gate, while a matching pending_buys row is still
open, must produce a distinct loud alert.

Real incident, SOXS/ira/wl_id=206, 2026-08-14: a 2026-08-12 trailing-buy filled
at 09:30:05 ET and sat unreconciled for hours. Both fill-detection paths
(check_auto_fills' poll and drain_fill_queue's stream fast path) sit behind the
SAME opt-in gate -- schwab_safety.auto_fill_detection_enabled(ticker) AND
node_auto_fill_detection_enabled(wl_id) -- and SOXS was in neither flag file.
So the fill was silently dropped, identically, by both paths, and the only
thing still talking about the order (check_buy_reminders) kept asserting "still
pending" off purely local, purely price-based state.

Note the asymmetry this closes: drain_fill_queue already had a loud
orphaned_fill_detected alert, but only for the case where NO pending row
exists. The more dangerous case -- a real fill for an order we ourselves placed
and are still actively reminding about -- was completely silent.

The fix (signals_notify.check_buy_reminders) re-verifies against the broker
before nagging, and is deliberately NOT gated on has_capital_at_stake: a
confirmed-but-unreconciled real fill is an infrastructure-precondition failure,
not routine per-node noise (same exemption precedent as
check_addon_buying_power_drift). The node in this scenario is $800 notional,
far below CAPITAL_AT_STAKE_THRESHOLD, so these tests only pass if that
exemption really holds.
"""
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import signals_config
import signals_db
import signals_notify
import schwab_safety

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_GATEDROP_SCENARIO'
ORDER_ID = 9911223344


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    monkeypatch.setattr(schwab_safety, 'STATE_PATH', tmp_path / "schwab_order_counts.json")
    monkeypatch.setattr(schwab_safety, 'KILL_SWITCH_PATH', tmp_path / "schwab_kill_switch.json")
    monkeypatch.setattr(schwab_safety, 'TICKER_AUTOMATION_PATH', tmp_path / "schwab_ticker_automation.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTOMATION_PATH', tmp_path / "schwab_node_automation.json")
    # Both flag files point at fresh, EMPTY tmp paths -- i.e. the node is opted
    # OUT of auto-fill detection, exactly like SOXS/206 was on 2026-08-14.
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_node_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 8, 14, 10, 30))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='soxl_ira', starting_notional=800 WHERE ticker=?",
                   (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _seed_pending_with_stale_reminder(node, order_id=ORDER_ID):
    """Seeds a pending_buys row in the order_placed phase with last_reminder_at
    far enough in the past that check_buy_reminders' cadence gate lets it
    through this poll."""
    sig = {'current_price': 10.15, 'last_bar': datetime(2026, 8, 12, 10, 25)}
    signals_db.add_pending_buy(node, sig, channel='C0TEST', ts='1234.5', order_id=order_id)
    signals_db.mark_pending_buy_placed_by_wl_id(node['id'])
    stale = (datetime.now() - timedelta(minutes=signals_notify.BUY_REMINDER_MINUTES + 5)
             ).strftime('%Y-%m-%d %H:%M:%S')
    with signals_db._conn() as c:
        c.execute("UPDATE pending_buys SET last_reminder_at=? WHERE wl_id=?", (stale, node['id']))
        c.commit()


def _capture_posts(monkeypatch):
    posted = []

    def _fake_post(text=None, *a, **kw):
        posted.append(text if text is not None else (a[0] if a else kw.get('text')))
        return ('C0TEST', '9999.1')

    monkeypatch.setattr(signals_notify, '_post_message', _fake_post)
    return posted


def _seed_resting_buy(fake_broker, price, qty):
    """Puts a real resting trailing BUY in the fake broker's order book, via the
    fixture's own API (not a hand-built dict -- that skips fields like `account`
    that get_orders_for_account really filters on). Returns its order_id."""
    fake_broker.set_quote(TICKER, last=price, bid=price, ask=price + 0.01)
    return fake_broker.seed_resting_order('soxl_ira', TICKER, 'TRAILING_STOP', 'BUY', qty)


def test_confirmed_fill_dropped_at_optin_gate_alerts_and_logs(env, fake_broker, monkeypatch):
    node = _node()
    order_id = _seed_resting_buy(fake_broker, price=40.60, qty=19.0)
    _seed_pending_with_stale_reminder(node, order_id=order_id)
    fake_broker.force_fill(order_id, price=40.60)

    # --- step 3/4: the opt-in gate correctly declines to auto-reconcile ---
    posted = _capture_posts(monkeypatch)
    signals_notify.check_auto_fills([])
    assert signals_db.get_open_position(TICKER) is None, (
        "check_auto_fills must still respect the opt-in gate -- this test is about "
        "ALERTING on the drop, not about silently widening auto-reconciliation")
    assert signals_db.get_pending_buy_by_wl_id(node['id']) is not None
    assert posted == [], "the gate itself should stay silent; the alert is the reminder loop's job"

    # --- step 5: the NEW behavior -- the reminder loop re-verifies and shouts ---
    signals_notify.check_buy_reminders()

    assert len(posted) == 1, f"expected exactly one alert, got: {posted}"
    msg = posted[0]
    assert 'CONFIRMED FILLED' in msg, msg
    assert TICKER in msg, msg
    assert '40.6' in msg, msg
    assert str(order_id) in msg, msg
    assert 'still pending' not in msg.lower(), (
        "must NOT be the old stale-state reminder text -- that wording is exactly "
        "what misled the human for hours on 2026-08-14")

    events = signals_db.get_coverage_events(scenario_key='confirmed_fill_dropped_at_gate')
    assert len(events) == 1, events
    assert events[0]['ticker'] == TICKER
    assert events[0]['result'] == 'alerted'
    assert events[0]['node_id'] == node['id']


def test_alert_fires_below_capital_at_stake_threshold(env, fake_broker, monkeypatch):
    """The exemption is the whole point -- an $800 node is far under
    CAPITAL_AT_STAKE_THRESHOLD ($10k), so the routine reminder path is muted
    for it. If the fill re-verification were placed after that gate (as it
    originally was), this exact scenario would stay silent forever."""
    node = _node()
    assert not signals_notify.has_capital_at_stake(node), (
        "test premise: this node must be BELOW the capital-at-stake bar")
    order_id = _seed_resting_buy(fake_broker, price=40.60, qty=19.0)
    _seed_pending_with_stale_reminder(node, order_id=order_id)
    fake_broker.force_fill(order_id, price=40.60)

    posted = _capture_posts(monkeypatch)
    signals_notify.check_buy_reminders()
    assert any('CONFIRMED FILLED' in p for p in posted), posted


def test_no_alert_when_the_order_has_not_actually_filled(env, fake_broker, monkeypatch):
    """Negative case: same opted-out node, same pending row, but the resting
    order is genuinely still WORKING. Must not fire the confirmed-fill alert,
    and must not log a coverage event (which would render as false proof in
    the Accountability Grid)."""
    node = _node()
    order_id = _seed_resting_buy(fake_broker, price=40.60, qty=19.0)
    _seed_pending_with_stale_reminder(node, order_id=order_id)
    assert fake_broker.orders[order_id]['status'] == 'WORKING'

    posted = _capture_posts(monkeypatch)
    signals_notify.check_buy_reminders()

    assert not any('CONFIRMED FILLED' in (p or '') for p in posted), posted
    assert signals_db.get_coverage_events(scenario_key='confirmed_fill_dropped_at_gate') == []


def test_broker_lookup_failure_does_not_break_the_reminder_loop(env, fake_broker, monkeypatch):
    """A transient broker/API failure must not take down the whole reminder
    loop for every other pending row -- the re-verification is an enhancement
    on top of the reminder, not a new precondition for it."""
    node = _node()
    _seed_pending_with_stale_reminder(node)

    def _boom(*a, **kw):
        raise RuntimeError("transient broker outage")

    monkeypatch.setattr(signals_notify.schwab_client, 'get_filled_order', _boom)
    posted = _capture_posts(monkeypatch)
    signals_notify.check_buy_reminders()  # must not raise
    assert not any('CONFIRMED FILLED' in (p or '') for p in posted), posted


def test_opted_in_node_still_auto_reconciles_with_no_gate_drop_alert(env, fake_broker, monkeypatch):
    """Regression (step 6): opting the ticker AND node in restores the normal
    auto-reconciliation path -- the fill is recorded by check_auto_fills, the
    pending row clears, and the reminder loop then has nothing left to shout
    about. Proves the new alert is scoped to the genuine gate-drop case and
    isn't just firing on every confirmed fill."""
    node = _node()
    schwab_safety.enable_auto_fill_detection(TICKER)
    schwab_safety.enable_node_auto_fill_detection(node['id'])

    order_id = _seed_resting_buy(fake_broker, price=40.60, qty=19.0)
    _seed_pending_with_stale_reminder(node, order_id=order_id)
    fake_broker.force_fill(order_id, price=40.60)

    posted = _capture_posts(monkeypatch)
    signals_notify.check_auto_fills([])

    assert signals_db.get_open_position(TICKER) is not None, (
        "opted-in node must still auto-reconcile exactly as before")
    assert signals_db.get_pending_buy_by_wl_id(node['id']) is None

    signals_notify.check_buy_reminders()
    assert not any('CONFIRMED FILLED' in (p or '') for p in posted), posted
    assert signals_db.get_coverage_events(scenario_key='confirmed_fill_dropped_at_gate') == []
