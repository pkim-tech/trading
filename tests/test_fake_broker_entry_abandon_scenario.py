"""fake_broker scenario for signals_notify.check_entry_abandon -- proves the
real cancel actually happens against a stateful order book, not just that
schwab_client.cancel_order was called with the right arguments (the per-
function-mock style already used in tests/test_entry_abandon.py). Built
2026-07-31 while expanding deterministic test coverage beyond independent
code review, after a HIGH real-money bug in an earlier version of this
function (could claim "resting order cancelled" while nothing was actually
cancelled) was found by review rather than by a test.

Distinct value over the mocked version: fake_broker.cancel_order really does
flip the seeded order's status, so asserting fake_broker.orders[order_id]
afterward proves the broker-side effect happened, not just that a function
was invoked. Also exercises _confirm_order_status's real post-cancel poll
(schwab_client.py) against fake_broker.get_order, which no other fake_broker
test had reached before this file (see fake_broker.py's get_order docstring)."""
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

from fake_broker import fake_broker  # noqa: F401 (pytest fixture import)

TICKER = 'TEST_ENTRY_ABANDON_SCENARIO'


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
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))
    signals_notify._ENTRY_ABANDON_ALERTED.clear()

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                         stop_loss=1, max_hold_hours=7, mode='live',
                         trail_buy_pct=1.0, trail_pct=1.0, starting_notional=5000, fixed_sl_override=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account = 'soxl_ira' WHERE ticker = ?", (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _sig(price, hours_ago):
    from tests.conftest import _synthetic_timestamps
    timestamps = _synthetic_timestamps()
    last_bar = timestamps[-1 - hours_ago] if hours_ago < len(timestamps) else timestamps[0]
    return {'ticker': TICKER, 'current_price': price, 'z_score': -2.5, 'last_bar': last_bar}


def test_real_cancel_actually_flips_the_broker_order_status(env, fake_broker):
    """The core deterministic claim: after check_entry_abandon runs on an
    overdue pending buy with a real resting order, that exact order is
    genuinely CANCELED in the broker's own order book -- not just that
    cancel_order was called."""
    from tests.conftest import make_synthetic_csv, cleanup_csv
    make_synthetic_csv(TICKER, last_close=100.0)
    try:
        node = _node()
        order_id = fake_broker.seed_resting_order(
            'soxl_ira', TICKER, 'TRAILING_STOP', 'BUY', 50, trail_offset=1.0)
        signals_db.add_pending_buy(node, _sig(100.0, hours_ago=10), channel=None, ts=None, order_id=order_id)
        signals_db.mark_pending_buy_placed_by_wl_id(node['id'])

        assert fake_broker.orders[order_id]['status'] == 'WORKING'

        signals_notify.check_entry_abandon()

        assert fake_broker.orders[order_id]['status'] == 'CANCELED'
        assert signals_db.get_pending_buys() == []
        events = signals_db.get_coverage_events(scenario_key="entry_abandon_timeout")
        assert any(e['result'] == 'abandoned' for e in events)
    finally:
        cleanup_csv(TICKER)


def test_real_bounce_fill_racing_the_cancel_is_reconciled_not_abandoned(env, fake_broker):
    """The order fills (broker-side, e.g. a real bounce the instant before
    our cancel lands) before check_entry_abandon gets to act -- proves
    against real broker state, not a mocked return value, that a genuine
    fill is opened as a real position rather than silently discarded."""
    from tests.conftest import make_synthetic_csv, cleanup_csv
    make_synthetic_csv(TICKER, last_close=100.0)
    try:
        node = _node()
        order_id = fake_broker.seed_resting_order(
            'soxl_ira', TICKER, 'TRAILING_STOP', 'BUY', 50, trail_offset=1.0)
        signals_db.add_pending_buy(node, _sig(100.0, hours_ago=10), channel=None, ts=None, order_id=order_id)
        signals_db.mark_pending_buy_placed_by_wl_id(node['id'])

        # Broker-side fill happens BEFORE our cancel attempt -- cancel_order's
        # real broker-side guard (status not in _TERMINAL_STATUSES) means the
        # fake broker's own cancel_order call is then a correct no-op, exactly
        # mirroring a real race.
        fake_broker.set_quote(TICKER, last=101.5, bid=101.5, ask=101.6)
        fake_broker.force_fill(order_id, price=101.5)
        assert fake_broker.orders[order_id]['status'] == 'FILLED'

        signals_notify.check_entry_abandon()

        assert fake_broker.orders[order_id]['status'] == 'FILLED'  # cancel_order correctly left it alone
        assert signals_db.get_pending_buys() == []
        positions = signals_db.get_open_positions()
        assert len(positions) == 1
        assert positions[0]['entry_price'] == 101.5
        events = signals_db.get_coverage_events(scenario_key="entry_abandon_timeout")
        assert any(e['result'] == 'raced_fill' for e in events)
    finally:
        cleanup_csv(TICKER)
