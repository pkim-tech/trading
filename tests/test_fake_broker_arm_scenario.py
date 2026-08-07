"""fake_broker scenarios for the arm transition (notify_trailing_activated ->
_attempt_automated_sell), which had zero fake_broker coverage before
2026-07-31 (found via scripts/fake_broker_coverage_matrix.py) -- every
existing fake_broker scenario (test_fake_broker_sh_scenario.py,
test_fake_broker_trail_exit_scenario.py) starts from an ALREADY-armed
position (trail_state.trailing=True seeded directly), never actually driving
the arm transition itself through real production code. Two real, distinct
paths: replacing an existing resting SL with a trailing-sell (the common
case), and a fresh trailing-sell placement when there's no SL to replace."""
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

TICKER = 'TEST_ARM_SCENARIO'
IN_WINDOW_TIME = datetime(2026, 7, 29, 10, 30)


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

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='soxl_ira' WHERE ticker=?", (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def test_arm_replaces_resting_sl_with_a_real_trailing_sell(env, fake_broker):
    """The common arm path: a resting protective SL genuinely gets replaced
    (REPLACED status) with a real TRAILING_STOP sell -- verified against the
    fake broker's own order book, not a mocked replace_order call."""
    node = _node()
    now = datetime.now()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    sl_order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'STOP', 'SELL', 100, stop_price=49.50)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='soxl_ira', sl_order_id=? WHERE ticker=?",
                   (sl_order_id, TICKER))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    assert fake_broker.orders[sl_order_id]['status'] == 'WORKING'

    signals_notify.notify_trailing_activated(pos, current_price=52.0)

    assert fake_broker.orders[sl_order_id]['status'] == 'REPLACED'
    new_orders = [o for o in fake_broker.orders.values()
                  if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                  and o['orderType'] == 'TRAILING_STOP' and o['status'] == 'WORKING']
    assert len(new_orders) == 1
    assert new_orders[0]['orderLegCollection'][0]['instruction'] == 'SELL'

    updated = signals_db.get_open_position(TICKER)
    assert updated['trail_state'].get('order_placed') is True
    assert updated['sl_order_id'] == new_orders[0]['orderId']  # 2026-07-31 writeback fix


def test_arm_places_a_fresh_trailing_sell_when_no_sl_to_replace(env, fake_broker):
    """No resting SL exists (e.g. the entry-time SL placement previously
    failed) -- a fresh TRAILING_STOP sell must still be placed directly, not
    silently skipped."""
    node = _node()
    now = datetime.now()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='soxl_ira' WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    assert pos['sl_order_id'] is None

    signals_notify.notify_trailing_activated(pos, current_price=52.0)

    new_orders = [o for o in fake_broker.orders.values()
                  if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                  and o['orderType'] == 'TRAILING_STOP' and o['status'] == 'WORKING']
    assert len(new_orders) == 1
    assert new_orders[0]['orderLegCollection'][0]['instruction'] == 'SELL'

    updated = signals_db.get_open_position(TICKER)
    assert updated['trail_state'].get('order_placed') is True
    # No prior sl_order_id existed to update (that's what makes this the
    # "fresh placement" case) -- the new order's id correctly lives in
    # trail_state.exit_order_id instead, not open_positions.sl_order_id
    # (which stays None; the 2026-07-31 writeback fix only refreshes an
    # existing sl_order_id after a REPLACE, it doesn't invent one).
    assert updated['sl_order_id'] is None
    assert updated['trail_state'].get('exit_order_id') == new_orders[0]['orderId']
