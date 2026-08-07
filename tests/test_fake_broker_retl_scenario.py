"""Second fake_broker scenario -- reproduces the RETL SL-anchored-to-stale-
signal_price finding (2026-07-29): _place_stop_loss_for_position used to
anchor the real stop-loss to pending_buys.signal_price (the original trigger
reference), not the real fill price. When a real order rests for hours
before filling and the real fill drifts far from the stale signal_price, the
resulting stop could land in a nonsensical place relative to the real entry
(RETL: signal_price=$10.15, real fill=$9.905, the computed 1%-below-
signal_price stop landed at $10.05 -- ABOVE the real entry).

Fixed 2026-07-31 (independently rediscovered and confirmed via a fresh
execution-path walkthrough): _place_stop_loss_for_position now anchors to
pos['entry_price'] (the real fill), matching exactly what strategies.py's
own check_exit already uses for the SL comparison -- correct for every
strategy, not just the market-buy ones where signal_price and entry_price
happen to coincide. This test now asserts the FIXED behavior."""
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

TICKER = 'TEST_RETL_SCENARIO'


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
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 7, 29, 9, 30))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])

    signals_db.ensure_tables()
    # v4-style node: fixed_sl is the real SL basis (uses_fixed_sl strategies),
    # matches RETL's real node (fixed_sl=1.0).
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


def test_sl_anchored_to_real_entry_lands_below_it(env, fake_broker, monkeypatch):
    node = _node()
    signal_price = 10.15   # frozen reference from when the order was staged
    real_fill_price = 9.905  # real fill, hours later, well below signal_price

    # --- pre-state: a real, resting, order_placed pending buy, no position yet ---
    sig = {'current_price': signal_price, 'last_bar': datetime(2026, 7, 29, 0, 4, 58)}
    signals_db.add_pending_buy(node, sig, channel='C0TEST', ts='1234.5', order_id=9999999999)
    signals_db.mark_pending_buy_placed_by_wl_id(node['id'])
    pending_before = [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER]
    assert len(pending_before) == 1
    assert signals_db.get_open_position(TICKER) is None
    assert fake_broker.orders == {}

    fake_broker.set_quote(TICKER, last=10.06, bid=10.00, ask=10.13)

    # --- act: reconcile the real fill, exactly as check_auto_fills would ---
    signals_notify._reconcile_buy_fill(TICKER, fill_price=real_fill_price, filled_shares=50.0,
                                        wl_id=node['id'])

    # --- post-state: full check, not just one field ---
    assert [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER] == [], \
        "pending_buys row should be cleared on reconciliation"

    pos = signals_db.get_open_position(TICKER)
    assert pos is not None, "position should be opened"
    assert pos['entry_price'] == real_fill_price
    assert pos['shares'] == 50.0

    # Hold-time origin fix (2026-07-31): pos['signal_time'] must reflect the
    # real fill moment, not the pending buy's original signal_time
    # (2026-07-29 00:04:58, hours/days stale by the time this test runs) --
    # _bars_held counts hold time from this field, and the backtest kernel's
    # real basis for a trailing-buy fill is the fill bar, not the signal bar
    # (the wait itself is tracked separately and never charged against hold
    # time). Before the fix, a position opened this way would already show
    # as having been held for however long the real order sat waiting to
    # bounce -- causing premature TIME exits.
    stored_signal_time = datetime.strptime(pos['signal_time'], '%Y-%m-%d %H:%M:%S')
    assert (datetime.now() - stored_signal_time).total_seconds() < 60, (
        f"pos['signal_time']={pos['signal_time']} should be ~now (the real fill moment), "
        f"not the stale pending-buy signal_time (2026-07-29 00:04:58)"
    )

    resting = [o for o in fake_broker.orders.values()
               if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
               and o['orderType'] == 'STOP']
    assert len(resting) == 1, f"expected exactly one real STOP order placed, found {len(resting)}"
    stop_order = resting[0]
    assert stop_order['status'] == 'WORKING'
    assert stop_order['orderLegCollection'][0]['quantity'] == 50

    real_stop_price = float(stop_order['stopPrice'])  # OrderBuilder formats to a
                                                        # 2-decimal string, matching
                                                        # the real order seen live tonight
    expected_stop = real_fill_price * (1 - node['fixed_sl'] / 100)  # fixed formula: anchored to the real fill
    assert real_stop_price == pytest.approx(expected_stop, abs=0.01)

    # The fix: this stop is correctly BELOW the real entry -- genuine
    # downside protection, unlike the pre-fix behavior (anchored to the
    # stale signal_price, which could land the stop above the real entry).
    assert real_stop_price < pos['entry_price'], (
        f"stop ${real_stop_price:.4f} should be BELOW real entry ${pos['entry_price']:.4f} "
        f"-- anchoring to signal_price (${signal_price:.4f}) instead of the real fill "
        f"would be meaningless as downside protection from the actual entry."
    )

    fill_events = signals_db.get_coverage_events(scenario_key='buy_fill_reconciled')
    assert any(e['ticker'] == TICKER for e in fill_events), \
        "_reconcile_buy_fill should log the buy_fill_reconciled coverage event"

    # broker_stop_price wiring (2026-08-01): a real automated SL placement
    # should record the price it actually placed at, so the SL alert can
    # trust it instead of falling back to a generic guess.
    pos_after = signals_db.get_open_position(TICKER)
    assert pos_after['broker_stop_price'] == pytest.approx(real_stop_price, abs=0.01)
