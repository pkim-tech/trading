"""fake_broker scenarios for signals_notify._check_order_terminal_not_filled
(built 2026-08-25, dispatched fix for a real live incident the same night):
check_sl_order_fills (sl_order_id) and the trailing-buy branches of
check_buy_reminders/check_auto_fills (pending_buys.order_id) all polled a
locally-tracked order_id via schwab_client.get_filled_order every cycle, which
only ever returns non-None for status=='FILLED' -- a REJECTED/CANCELED/
EXPIRED/REPLACED order_id silently returned None forever, no alert, no log
line. Two real live instances hit this the same night: WEBL's resting SL
order REPLACED by a manual market SELL closing the position, and DPST's
resting pending-buy order manually CANCELED by the user -- both confirmed via
schwab_client.get_real_orders, neither ever surfaced by the FILLED-only poll."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import signals_config
import signals_db
import signals_notify
import schwab_safety

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_SCRATCH_TERMINAL_NOT_FILLED'


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
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_node_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 8, 7, 9, 32, 53))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    alerts = []
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda text, *a, **kw: (alerts.append(text), (None, None))[1])

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=50.0,
                         stop_loss=1, max_hold_hours=100, state='live',
                         trail_buy_pct=1.0, trail_pct=0.3, fixed_sl_override=0.3)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='soxl_ira' WHERE ticker=?", (TICKER,))
        c.commit()

    # Module-level throttle dicts persist across tests in the same pytest worker, and
    # fake_broker's order-id counter restarts fresh per test -- without clearing these,
    # a later test's order_id can collide with an earlier test's already-throttled key
    # and silently suppress its alert (found while adding this file's REPLACED test).
    signals_notify._SL_ORDER_TERMINAL_ALERTED.clear()
    signals_notify._PENDING_BUY_ORDER_TERMINAL_ALERTED.clear()

    yield alerts

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def test_sl_order_replaced_alerts_instead_of_silently_polling_forever(env, fake_broker):
    alerts = env
    entry_time = datetime(2026, 8, 7, 9, 32, 53)
    node = _node()
    signals_db.open_position(node, signal_price=7.64, signal_time=entry_time,
                              entry_price=7.685, entry_time=entry_time, shares=1)
    sl_order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'STOP', 'SELL', 1, stop_price=7.6619)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='soxl_ira', sl_order_id=? WHERE ticker=?",
                   (sl_order_id, TICKER))
        c.commit()
    pos = signals_db.get_open_position(TICKER)

    # Simulate the WEBL shape: a manual SELL supersedes the resting SL order.
    fake_broker.orders[sl_order_id]['status'] = 'REPLACED'

    signals_notify.check_sl_order_fills([pos])

    assert any('REPLACED' in a for a in alerts), f"no REPLACED alert posted, got: {alerts}"
    events = signals_db.get_coverage_events(scenario_key='sl_order_terminal_not_filled')
    assert any(e['ticker'] == TICKER and e['result'] == 'alerted' for e in events)
    # Position tracking is NOT auto-mutated (alert-only, matches the existing
    # qty-mismatch precedent in the same function) -- still open locally.
    assert signals_db.get_open_position(TICKER) is not None


def test_pending_buy_canceled_alerts_and_clears_tracking(env, fake_broker):
    alerts = env
    node = _node()
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET trail_buy_pct=1.0 WHERE ticker=?", (TICKER,))
        c.commit()
    node = _node()
    signal_time = datetime(2026, 8, 7, 9, 32, 53)
    sig = {'current_price': 7.64, 'last_bar': signal_time}
    signals_db.add_pending_buy(node, sig, channel=None, ts=None)
    order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'LIMIT', 'BUY', 1)
    signals_db.mark_pending_buy_placed(TICKER)
    with signals_db._conn() as c:
        c.execute("UPDATE pending_buys SET order_id=? WHERE ticker=?", (order_id, TICKER))
        c.commit()

    # Simulate the DPST shape: user manually cancels the resting order.
    fake_broker.orders[order_id]['status'] = 'CANCELED'

    open_positions = []
    signals_notify.check_auto_fills(open_positions)

    assert any('CANCELED' in a for a in alerts), f"no CANCELED alert posted, got: {alerts}"
    events = signals_db.get_coverage_events(scenario_key='pending_buy_order_terminal_not_filled')
    assert any(e['ticker'] == TICKER and e['result'] == 'cleared' for e in events)
    assert signals_db.get_pending_buys() == [] or all(
        p['ticker'] != TICKER for p in signals_db.get_pending_buys())


def test_pending_buy_replaced_alerts_but_never_clears_tracking(env, fake_broker):
    """The CRITICAL bug the independent-cold paired review caught before this fix
    landed: REPLACED means a NEW, live order superseded this one (e.g.
    check_gap_resize's own replace_equity_order_with_market path leaving order_id
    pointed at the now-superseded id after a generic exception) -- it does NOT mean
    nothing is resting at the broker, unlike REJECTED/CANCELED/EXPIRED. Clearing the
    pending_buys row here would orphan a real live replacement order with no local
    row left to reconcile its eventual fill against. Must alert-and-preserve, same as
    a genuine partial fill, never auto-clear."""
    alerts = env
    node = _node()
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET trail_buy_pct=1.0 WHERE ticker=?", (TICKER,))
        c.commit()
    node = _node()
    signal_time = datetime(2026, 8, 7, 9, 32, 53)
    sig = {'current_price': 7.64, 'last_bar': signal_time}
    signals_db.add_pending_buy(node, sig, channel=None, ts=None)
    order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'LIMIT', 'BUY', 1)
    signals_db.mark_pending_buy_placed(TICKER)
    with signals_db._conn() as c:
        c.execute("UPDATE pending_buys SET order_id=? WHERE ticker=?", (order_id, TICKER))
        c.commit()

    # Simulate a real replace landing (a live new order exists) while local order_id
    # still points at the now-superseded one, e.g. check_gap_resize's own
    # `except Exception` branch that deliberately leaves the row as-is.
    fake_broker.orders[order_id]['status'] = 'REPLACED'

    open_positions = []
    signals_notify.check_auto_fills(open_positions)

    assert any('REPLACED' in a for a in alerts), f"no REPLACED alert posted, got: {alerts}"
    events = signals_db.get_coverage_events(scenario_key='pending_buy_order_terminal_not_filled')
    assert any(e['ticker'] == TICKER and e['result'] == 'alerted' for e in events)
    assert not any(e['result'] == 'cleared' for e in events)
    # Tracking PRESERVED -- the real live replacement order still needs this row.
    assert any(p['ticker'] == TICKER for p in signals_db.get_pending_buys())
