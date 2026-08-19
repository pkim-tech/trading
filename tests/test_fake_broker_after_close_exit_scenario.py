"""fake_broker scenario for trading_incidents #13 (docs/deep_backlog.md,
2026-08-19): a real 75sh SOXL position in `ira` had its resting protective
STOP replaced by _attempt_automated_exit_sell with a MARKET SELL at
16:00:52 ET -- 52s after the 16:00:00 regular-session close. The MARKET
order sat PENDING_ACTIVATION for hours (nothing to fill against, market
closed), leaving the position with neither a working stop nor a completed
exit. Root cause: the replace-to-market exit path had no market-hours check
before submitting a MARKET order.

Fix: _market_session_open_now() gates the actual order submission inside
_attempt_automated_exit_sell -- past the 16:00 ET close (or before 9:30, or
on a non-trading day), the function is a deliberate no-op: it returns None
without touching the resting order, routing notify_sell_signal to the same
manual-fallback alert any other placement failure already takes. This mirrors
test_fake_broker_exit_fresh_scenario.py's structure (a real, non-dry_run
order-book sequence, not a per-call mock) -- exactly the shape needed to
prove BOTH that no order was submitted AND that the pre-existing resting STOP
survives untouched, which a per-function mock can't verify simultaneously."""
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

TICKER = 'TEST_AFTER_CLOSE_EXIT_SCENARIO'
IN_WINDOW_TIME = datetime(2026, 8, 19, 15, 30)  # a real trading day (Wednesday), used for schwab_safety's clock


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
    monkeypatch.setattr(schwab_safety, '_now', lambda: IN_WINDOW_TIME)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))
    monkeypatch.setattr(signals_notify, 'time', type('T', (), {'sleep': staticmethod(lambda *a: None)}))

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=2, max_hold_hours=100, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=2.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='ira' WHERE ticker=?", (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _open_pos_with_resting_stop(fake_broker):
    entry_time = datetime(2026, 8, 19, 9, 30, 0)
    signals_db.open_position(_node(), signal_price=128.7588, signal_time=entry_time,
                              entry_price=128.7588, entry_time=entry_time, shares=75)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira' WHERE ticker=?", (TICKER,))
        c.commit()
    sl_order_id = fake_broker.seed_resting_order(
        'ira', TICKER, 'STOP', 'SELL', 75, stop_price=126.18)
    signals_db.set_sl_order_id_by_position(
        signals_db.get_open_position(TICKER)['id'], sl_order_id)
    return signals_db.get_open_position(TICKER), sl_order_id


def _market_sells(fake_broker):
    return [o for o in fake_broker.orders.values()
            if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
            and o['orderType'] == 'MARKET'
            and o['orderLegCollection'][0]['instruction'] == 'SELL']


def test_sell_signal_after_close_does_not_replace_resting_stop_with_market_order(env, fake_broker, monkeypatch):
    """Incident #13 repro: SELL SIGNAL (SL) decision arrives at 16:00:52 ET,
    52s after the 16:00:00 close. Must NOT submit a MARKET order, and the
    pre-existing resting STOP must be left exactly as it was -- still WORKING,
    same order id, not REPLACED/CANCELED."""
    pos, sl_order_id = _open_pos_with_resting_stop(fake_broker)
    fake_broker.set_quote(TICKER, last=123.86, bid=123.86, ask=123.96)
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 8, 19, 16, 0, 52))

    signals_notify.notify_sell_signal(pos, 'SL', current_price=123.86, target_price=126.18)

    assert len(_market_sells(fake_broker)) == 0, "no MARKET order should be submitted after the 16:00 close"
    assert fake_broker.orders[sl_order_id]['status'] == 'WORKING', \
        "the pre-existing resting STOP must be left untouched, still resting"

    # Position stays open -- unresolved until the next session, protected only
    # by the still-resting STOP (real overnight protection, just not the
    # discretion-free bar-close exit).
    assert signals_db.get_open_position(TICKER) is not None

    events = signals_db.get_coverage_events(scenario_key='automated_exit_execution')
    matches = [e for e in events if e['ticker'] == TICKER and e['result'] == 'skipped']
    assert len(matches) == 1


def test_sell_signal_before_open_does_not_replace_resting_stop_with_market_order(env, fake_broker, monkeypatch):
    """Same shape, before the 9:30 open -- a real pre-market bar-close style
    decision must not fire a MARKET order either."""
    pos, sl_order_id = _open_pos_with_resting_stop(fake_broker)
    fake_broker.set_quote(TICKER, last=123.86, bid=123.86, ask=123.96)
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 8, 19, 9, 15, 0))

    signals_notify.notify_sell_signal(pos, 'SL', current_price=123.86, target_price=126.18)

    assert len(_market_sells(fake_broker)) == 0
    assert fake_broker.orders[sl_order_id]['status'] == 'WORKING'
    assert signals_db.get_open_position(TICKER) is not None


def test_sell_signal_intraday_still_replaces_resting_stop_with_market_order(env, fake_broker, monkeypatch):
    """Negative/regression case: an intraday (pre-close) SELL SIGNAL must
    still replace the resting STOP with a MARKET sell exactly as before --
    the guard must not block the normal in-session path."""
    pos, sl_order_id = _open_pos_with_resting_stop(fake_broker)
    fake_broker.set_quote(TICKER, last=123.86, bid=123.86, ask=123.96)
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 8, 19, 14, 32, 0))

    signals_notify.notify_sell_signal(pos, 'SL', current_price=123.86, target_price=126.18)

    assert fake_broker.orders[sl_order_id]['status'] == 'REPLACED'
    sells = _market_sells(fake_broker)
    assert len(sells) == 1
    assert sells[0]['status'] == 'FILLED'
    assert signals_db.get_open_position(TICKER) is None  # closed on the immediate fill

    events = signals_db.get_coverage_events(scenario_key='automated_exit_execution')
    matches = [e for e in events if e['ticker'] == TICKER and e['result'] == 'placed']
    assert len(matches) == 1
