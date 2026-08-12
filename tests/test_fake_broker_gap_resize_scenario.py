"""Fifth fake_broker scenario: check_gap_resize's running_low staleness gap
(found live 2026-07-29, RETL) and its fix (update_real_pending_buys_running_low).
pending_buys.running_low used to be updated only by update_dry_run_buys (the
dry_run-sim feature) -- check_gap_resize (an older, unrelated feature) reads
the same column assuming it's live-tracked for real orders too, but nothing
updated it for a real pending buy before this fix. Proves the two-phase real
consequence: a real trailing-buy resting for hours, price falls (running_low
should track it down), then gaps back up past the TRUE trigger but not the
stale one -- exactly the shape that would have been silently missed before
update_real_pending_buys_running_low existed."""
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

TICKER = 'TEST_GAP_RESIZE_SCENARIO'


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
    # 9:20 ET -- inside _GAP_CHECK_WINDOW (9,15,9,29).
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 7, 29, 9, 20))
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


def test_gap_resize_catches_correction_once_running_low_is_tracked(env, fake_broker, monkeypatch):
    """Mirrors RETL's real numbers, but as a full two-phase scenario proving
    the fix (update_real_pending_buys_running_low): staged with
    signal_price=$10.15, trail_buy_pct=1%.

    Phase 1 -- price genuinely falls to $9.80 overnight. Before the fix,
    running_low would stay frozen at $10.15 forever for a real order (only
    dry_run accounts were tracked). After the fix, it should update to $9.80.

    Phase 2 -- price then gaps back UP to $10.00: above the trigger a
    correctly-tracked running_low produces ($9.80 * 1.01 = $9.898), but still
    below the STALE trigger the old frozen value would have produced
    ($10.15 * 1.01 = $10.2515). This is exactly the failure shape discussed
    live: the bug can only actually cause a wrong missed-correction on a
    gap-UP that clears the true trigger but not the stale one. Confirms
    check_gap_resize now correctly fires where the unfixed code would have
    silently done nothing."""
    node = _node()
    signal_price = 10.15

    # Seed the real resting order FIRST so its real order_id can be attached
    # to the pending_buys row -- using a disconnected literal here would let
    # check_gap_resize "succeed" against a nonexistent order_id (silently
    # creating a phantom replacement) without ever touching the actual seeded
    # order, masking a real bug as a false pass.
    order_id = fake_broker.seed_resting_order(
        'soxl_ira', TICKER, 'TRAILING_STOP', 'BUY', 50, trail_offset=1.0)

    sig = {'current_price': signal_price, 'last_bar': datetime(2026, 7, 29, 0, 4, 58)}
    signals_db.add_pending_buy(node, sig, channel='C0TEST', ts='1234.5', order_id=order_id)
    signals_db.mark_pending_buy_placed_by_wl_id(node['id'])

    pending = [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER][0]
    assert pending['running_low'] == signal_price, "pre-state: running_low starts at signal_price"

    # --- Phase 1: price falls overnight, tracking should follow it down ---
    fake_broker.set_quote(TICKER, last=9.80, bid=9.80, ask=9.81)
    signals_notify.update_real_pending_buys_running_low()

    tracked = [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER][0]
    assert tracked['running_low'] == pytest.approx(9.80), (
        f"expected running_low to track the real price fall to $9.80, got {tracked['running_low']} "
        f"-- the fix (update_real_pending_buys_running_low) should update it for a real order now"
    )

    # --- Phase 2: price gaps back up, clearing the TRUE trigger but not the
    # stale one -- exactly the case that would have been missed pre-fix ---
    fake_broker.set_quote(TICKER, last=10.00, bid=10.00, ask=10.01)
    true_trigger = 9.80 * 1.01
    stale_trigger = signal_price * 1.01
    assert true_trigger < 10.00 < stale_trigger, "test setup: price must clear the true trigger, not the stale one"

    signals_notify.check_gap_resize()

    # --- post-state: full check ---
    pending_after = [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER]
    assert pending_after == [], "gap_resize should have cleared the pending_buys row (replaced with a market buy)"

    ticker_orders = [o for o in fake_broker.orders.values()
                      if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER]
    replaced = fake_broker.orders[order_id]
    assert replaced['status'] == 'REPLACED', (
        f"expected the resting trailing-buy to be replaced once the real "
        f"(tracked) trigger cleared, got status={replaced['status']}"
    )
    market_buys = [o for o in ticker_orders if o['orderType'] == 'MARKET'
                    and o['orderLegCollection'][0]['instruction'] == 'BUY']
    # gap_resize's own replacement is the first (largest) one -- a second,
    # smaller top-up buy is expected, legitimate downstream behavior (this
    # node's real fill lands under target_notional, triggering
    # _reconcile_fill's post-fill top-up, the same mechanism proven in
    # test_fake_broker_topup_scenario.py), not a bug in this test.
    assert len(market_buys) >= 1, f"expected at least one real market buy replacement, found: {ticker_orders}"
    assert market_buys[0]['orderLegCollection'][0]['quantity'] == 76, (
        f"expected gap_resize's own replacement to size ~$760/{'$'}10.00 = 76 shares "
        f"(5% pad on the $800 target_notional), got {market_buys[0]['orderLegCollection'][0]['quantity']}"
    )

    gap_events = [e for e in signals_db.get_coverage_events(scenario_key='gap_resize')
                  if e['ticker'] == TICKER]
    assert any(e['result'] == 'replaced' for e in gap_events), (
        f"expected a gap_resize coverage_event with result='replaced', got: {gap_events}"
    )


def test_running_low_bounded_against_a_single_anomalous_print(env, fake_broker):
    """Regression test for the extended-hours contamination fix (2026-07-31
    audit, left open): a single wild/erroneous print (e.g. a thin
    extended-hours odd-lot trade at a fraction of the real price) must not
    permanently crater running_low -- only a bounded (_MAX_RUNNING_LOW_DROP_PCT)
    step is trusted per poll, converging over a few polls instead of adopting
    the bad print outright."""
    node = _node()
    signal_price = 10.15
    order_id = fake_broker.seed_resting_order(
        'soxl_ira', TICKER, 'TRAILING_STOP', 'BUY', 50, trail_offset=1.0)
    sig = {'current_price': signal_price, 'last_bar': datetime(2026, 7, 29, 0, 4, 58)}
    signals_db.add_pending_buy(node, sig, channel='C0TEST', ts='1234.5', order_id=order_id)
    signals_db.mark_pending_buy_placed_by_wl_id(node['id'])

    # A single anomalous print at $1.00 (~90% below signal_price) -- clearly
    # not a real tradeable price for this ticker.
    fake_broker.set_quote(TICKER, last=1.00, bid=1.00, ask=1.01)
    signals_notify.update_real_pending_buys_running_low()
    tracked = [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER][0]
    expected_floor = signal_price * (1 - signals_notify.MAX_RUNNING_LOW_DROP_PCT / 100)
    assert tracked['running_low'] == pytest.approx(expected_floor), (
        f"expected running_low bounded to the {signals_notify.MAX_RUNNING_LOW_DROP_PCT}% floor "
        f"({expected_floor}), got {tracked['running_low']} -- a single bad print was adopted outright"
    )

    # If the bad print immediately reverts, running_low should not have been
    # permanently cratered to $1.00 -- only the bounded floor above.
    fake_broker.set_quote(TICKER, last=10.10, bid=10.10, ask=10.11)
    signals_notify.update_real_pending_buys_running_low()
    tracked_after = [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER][0]
    assert tracked_after['running_low'] == pytest.approx(expected_floor), (
        "running_low should stay at the bounded floor once set (min() never rises), "
        "confirming the earlier bad print wasn't discarded silently but wasn't fully "
        "adopted either"
    )


def test_gap_resize_places_a_fresh_market_buy_when_no_order_id_on_file(env, fake_broker):
    """gap_resize_fresh use case (found by scripts/fake_broker_coverage_matrix.py,
    2026-07-31): the scenario above always seeds a resting order first, so
    check_gap_resize's REPLACE branch (order_id truthy) is well covered but
    its fresh-placement fallback (no order_id at all, e.g. a manually-placed
    trailing buy the daemon never captured an id for) had never actually been
    driven through fake_broker despite check_gap_resize being called by name
    in that scenario (a grep-only coverage check would false-positive here)."""
    node = _node()
    signal_price = 10.15
    sig = {'current_price': signal_price, 'last_bar': datetime(2026, 7, 29, 0, 4, 58)}
    signals_db.add_pending_buy(node, sig, channel='C0TEST', ts='1234.5', order_id=None)
    signals_db.mark_pending_buy_placed_by_wl_id(node['id'])
    pending = [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER][0]
    assert pending['order_id'] is None

    fake_broker.set_quote(TICKER, last=10.50, bid=10.50, ask=10.51)  # clears the 1% trigger

    signals_notify.check_gap_resize()

    # >= 1, not exactly 1 -- the initial market-buy fill can undersize
    # slightly, triggering a real 2nd post-fill top-up market buy
    # (_reconcile_fill), itself a real, correct, already-covered scenario.
    market_buys = [o for o in fake_broker.orders.values()
                    if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                    and o['orderType'] == 'MARKET'
                    and o['orderLegCollection'][0]['instruction'] == 'BUY']
    assert len(market_buys) >= 1
    assert all(o['status'] == 'FILLED' for o in market_buys)
    assert signals_db.get_pending_buys() == []
    events = signals_db.get_coverage_events(scenario_key='gap_resize')
    assert any(e['result'] == 'replaced' for e in events)
