"""Second fake_broker scenario -- reproduces the RETL SL-anchored-to-stale-
signal_price finding (2026-07-29): _place_stop_loss_for_position anchors the
real stop-loss to pending_buys.signal_price (the original trigger reference),
not the real fill price. That's an intentional design choice for the normal
case (matches the backtest's zero-slippage assumption) -- but when a real
order rests for hours before filling and the real fill drifts far from the
stale signal_price, the resulting stop can land in a nonsensical place
relative to the real entry (RETL: signal_price=$10.15, real fill=$9.905, the
computed 1%-below-signal_price stop landed at $10.05 -- ABOVE the real entry).

This test doesn't assert a fix (none was decided) -- it pins down the exact,
current, real behavior with full pre/post state, so a future change to this
formula has a concrete regression test to run against instead of re-deriving
this from scratch."""
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
                         stop_loss=1, max_hold_hours=105, mode='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='soxl_ira' WHERE ticker=?", (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def test_sl_anchored_to_stale_signal_price_lands_above_real_entry(env, fake_broker, monkeypatch):
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
    expected_stop = signal_price * (1 - node['fixed_sl'] / 100)  # current (unfixed) formula
    assert real_stop_price == pytest.approx(expected_stop, abs=0.01)

    # The actual finding: this stop is ABOVE the real entry, not below it --
    # backwards for downside protection on a long position.
    assert real_stop_price > pos['entry_price'], (
        f"real finding reproduced: stop ${real_stop_price:.4f} is ABOVE "
        f"real entry ${pos['entry_price']:.4f} (anchored to stale signal_price "
        f"${signal_price:.4f} instead of the real fill) -- meaningless as "
        f"downside protection from the actual entry."
    )

    fill_events = signals_db.get_coverage_events(scenario_key='buy_fill_reconciled')
    assert any(e['ticker'] == TICKER for e in fill_events), \
        "_reconcile_buy_fill should log the buy_fill_reconciled coverage event"
