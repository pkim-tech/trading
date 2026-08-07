"""Rehearsal for reconciling GDXU's real, orphaned soxl_ira position
(wl_id=108) before touching the real broker again -- found 2026-08-06: a
2026-07-30 stage_live_test_order.py fill never got a pending_buys row (its
own node lookup failed), so the fill sat unreconciled/unprotected for a week,
2 shares @ $80.18, +35% unrealized, zero stop-loss on file.

Mirrors node 108's exact real config (TrailingBothZScoreBreakout, fixed_sl=
trail_sell_pct=arm_sell_pct=0.3%, account soxl_ira) and its real situation
(entry backdated ~8 days, current price far past the 0.3% arm threshold, no
resting SL to replace) -- confirms the real reconciliation path (open a
backdated position, then let arming detect and place a real trailing-sell)
behaves correctly against the fake broker before doing this for real."""
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import signals_config
import signals_compute
import signals_db
import signals_notify
import schwab_safety

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_GDXU_RECONCILE'
ENTRY_PRICE = 80.18
CURRENT_PRICE = 108.51
SHARES = 2


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
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 8, 6, 10, 30))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=0.3, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=0.3, fixed_sl_override=0.3)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='soxl_ira', arm_sell_pct=0.3 WHERE ticker=?", (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def test_reconciling_a_backdated_far_past_arm_position_places_a_real_trailing_sell(env, fake_broker):
    """The exact GDXU shape: a real fill from over a week ago, way past its
    0.3% arm threshold, with no resting SL to replace -- confirms reconciling
    it (open_position with the real backdated entry_time) and then running
    the arm transition places a real trailing-sell at the fake broker, with
    correct trail_state, and doesn't misbehave because of the unusual
    backdated hold time."""
    node = _node()
    fake_broker.set_quote(TICKER, last=CURRENT_PRICE, bid=CURRENT_PRICE - 0.01, ask=CURRENT_PRICE + 0.01)
    real_entry_time = datetime.now() - timedelta(days=8)

    pos_id = signals_db.open_position(node, signal_price=ENTRY_PRICE, signal_time=real_entry_time,
                                       entry_price=ENTRY_PRICE, entry_time=real_entry_time, shares=SHARES)
    assert pos_id, "reconciliation must actually open a position"

    pos = signals_db.get_open_position(TICKER)
    assert pos['sl_order_id'] is None
    assert pos['shares'] == SHARES
    assert pos['entry_price'] == ENTRY_PRICE

    # Drive the REAL production entry point (active_signals.py's real poll
    # loop calls check_sell_condition, then notify_trailing_activated only if
    # it reports just_activated_trailing) -- not notify_trailing_activated in
    # isolation, so this actually rehearses arm-detection too, not just the
    # notification/order-placement half.
    reason, target, just_activated_trailing = signals_compute.check_sell_condition(
        pos, CURRENT_PRICE, datetime.now(), at_bar_close=True,
        low=CURRENT_PRICE - 0.5, high=CURRENT_PRICE + 0.5, open_price=CURRENT_PRICE)
    assert just_activated_trailing is True, (
        f"expected arming to fire (price is {(CURRENT_PRICE/ENTRY_PRICE - 1)*100:.1f}% above entry, "
        f"arm_sell_pct=0.3%) -- reason={reason} target={target}")
    pos = signals_db.get_open_position(TICKER)  # re-fetch: check_sell_condition already persisted trail_state
    signals_notify.notify_trailing_activated(pos, current_price=CURRENT_PRICE)

    new_orders = [o for o in fake_broker.orders.values()
                  if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                  and o['orderType'] == 'TRAILING_STOP' and o['status'] == 'WORKING']
    assert len(new_orders) == 1, "exactly one real trailing-sell must be placed"
    assert new_orders[0]['orderLegCollection'][0]['instruction'] == 'SELL'
    assert new_orders[0]['orderLegCollection'][0]['quantity'] == SHARES

    updated = signals_db.get_open_position(TICKER)
    assert updated['trail_state'].get('trailing') is True
    assert updated['trail_state'].get('order_placed') is True
    assert updated['trail_state'].get('exit_order_id') == new_orders[0]['orderId']
