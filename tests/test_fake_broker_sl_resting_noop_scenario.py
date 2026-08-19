"""Task #1 dispatch (2026-08-19), root incident SOXS/ira same day: a stale
cached price fed a false reason='SL' classification and
_attempt_automated_exit_sell market-replaced a resting protective stop that
had never actually been breached -- it happened to fill at a better price
than entry, but only by luck, not correctness. Redesign under test:

  - reason='SL' with a genuinely resting stop (pos['sl_order_id'] set) is now
    a no-op -- check_sl_order_fills (this module's own independent poll)
    detects a real fill on its own, so nothing here needs to actively
    replace it with a market SELL.
  - reason='SL' with NO resting stop (entry-time placement failed, or was
    cleared) restores a protective STOP order instead of falling back to a
    market SELL -- a stop is passive/risk-reducing and safe to place
    liberally, a market SELL is active/risk-creating and is the wrong tool
    for "restore protection."

TP/TIME/hold-time-forced-TRAIL are unaffected by this change -- covered by
the existing test_fake_broker_sh_scenario.py, not re-tested here."""
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
import schwab_client

from fake_broker import fake_broker  # noqa: F401 (pytest fixture import)

TICKER = 'TEST_SL_NOOP_SCENARIO'


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
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 8, 19, 10, 30))
    monkeypatch.setattr(signals_notify, '_market_session_open_now', lambda now=None: True)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=50,
                         stop_loss=5, max_hold_hours=24, state='live',
                         trail_buy_pct=1.0, trail_pct=50.0, fixed_sl_override=5.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='soxl_ira', arm_sell_pct=0.3, trail_sell_pct=50.0 "
                   "WHERE ticker=?", (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _ticker_orders(fake_broker):
    return [o for o in fake_broker.orders.values()
            if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER]


def test_sl_with_resting_stop_is_left_alone(env, fake_broker, monkeypatch):
    """A resting protective STOP exists (normal case -- placed at entry
    time). A reason='SL' exit signal fires (in the real incident, off a
    stale/wrong price read) -- the redesigned code must NOT touch the
    resting order at all: no cancel, no replace, no new order."""
    node = _node()
    entry_time = datetime(2026, 8, 18, 9, 31, 0)
    signals_db.open_position(node, signal_price=20.00, signal_time=entry_time,
                              entry_price=20.00, entry_time=entry_time, shares=100)
    pos = signals_db.get_open_position(TICKER)

    fake_broker.set_quote(TICKER, last=19.50, bid=19.49, ask=19.51)
    sl_order_id = fake_broker.seed_resting_order(
        'soxl_ira', TICKER, 'STOP', 'SELL', 100, stop_price=19.00)  # 5% below entry
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET sl_order_id=? WHERE id=?", (sl_order_id, pos['id']))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    assert pos['sl_order_id'] == sl_order_id

    order_id = signals_notify._attempt_automated_exit_sell(pos, 'SL', current_price=19.50)
    assert order_id == sl_order_id, "should hand back the same resting stop's order id, nothing new placed"

    orders = _ticker_orders(fake_broker)
    assert len(orders) == 1, f"expected the ONE seeded resting stop untouched, found: {orders}"
    assert orders[0]['orderId'] == sl_order_id
    assert orders[0]['status'] == 'WORKING', "resting stop must not be cancelled/replaced"
    assert orders[0]['orderType'] == 'STOP', "must not have been turned into a MARKET order"

    noop_events = signals_db.get_coverage_events(scenario_key='sl_exit_resting_noop')
    assert any(e['ticker'] == TICKER for e in noop_events), \
        "expected the sl_exit_resting_noop coverage event to fire"


def test_sl_with_no_resting_stop_places_a_stop_not_a_market_sell(env, fake_broker, monkeypatch):
    """No resting stop exists at all (entry-time placement failed or was
    cleared -- pos['sl_order_id'] is None). A reason='SL' exit signal fires;
    the redesigned code must restore protection via a real STOP order, NOT
    the old fallback (a fresh market SELL)."""
    node = _node()
    entry_time = datetime(2026, 8, 18, 9, 31, 0)
    signals_db.open_position(node, signal_price=20.00, signal_time=entry_time,
                              entry_price=20.00, entry_time=entry_time, shares=100)
    pos = signals_db.get_open_position(TICKER)
    assert pos['sl_order_id'] is None, "pre-state: no resting stop"

    fake_broker.set_quote(TICKER, last=19.50, bid=19.49, ask=19.51)

    order_id = signals_notify._attempt_automated_exit_sell(pos, 'SL', current_price=19.50)
    assert order_id is not None, "should have placed a real restore-stop order"

    orders = _ticker_orders(fake_broker)
    assert len(orders) == 1, f"expected exactly one new order (the restore-stop), found: {orders}"
    placed = orders[0]
    assert placed['orderType'] == 'STOP', (
        f"expected a passive protective STOP, not a market SELL -- got {placed['orderType']}"
    )
    assert placed['orderId'] == order_id
    assert placed['orderLegCollection'][0]['quantity'] == 100
    # Anchored to entry_price (20.00) * (1 - 5%) = 19.00, matching
    # _place_stop_loss_for_position's own basis -- NOT signal_price (also
    # 20.00 here, so this assertion alone wouldn't distinguish the two; see
    # the second test below for that).
    assert placed['stopPrice'] == pytest.approx(19.00)

    closed_pos = signals_db.get_open_position(TICKER)
    assert closed_pos is not None, "position stays open -- a resting stop was placed, not a market fill"
    assert closed_pos['sl_order_id'] == order_id, \
        "sl_order_id should be repointed at the newly-restored stop so the next poll no-ops on it"

    restored_events = signals_db.get_coverage_events(scenario_key='sl_stop_restored')
    assert any(e['ticker'] == TICKER for e in restored_events), \
        "expected the sl_stop_restored coverage event to fire"

    # A second call (simulating the very next bar-close poll, condition still
    # true) must now take the no-op branch against the just-restored stop,
    # not place a second stop.
    order_id_2 = signals_notify._attempt_automated_exit_sell(closed_pos, 'SL', current_price=19.50)
    assert order_id_2 == order_id
    assert len(_ticker_orders(fake_broker)) == 1, "must not place a second restore-stop on the next poll"


def test_sl_stop_restore_anchors_to_entry_price_not_signal_price(env, fake_broker, monkeypatch):
    """Distinct entry_price vs signal_price (a late/manual/backdated entry,
    or a trailing-buy bounce-fill) -- the restore-stop must anchor to the
    real fill (entry_price), matching strategies.py's own check_exit
    comparison, not the earlier trigger price."""
    node = _node()
    entry_time = datetime(2026, 8, 18, 9, 31, 0)
    signal_time = datetime(2026, 8, 18, 9, 30, 0)
    # entry_price (22.00) intentionally != signal_price (20.00), mirroring a
    # trailing-buy bounce-fill entering higher than the original signal.
    signals_db.open_position(node, signal_price=20.00, signal_time=signal_time,
                              entry_price=22.00, entry_time=entry_time, shares=100)
    pos = signals_db.get_open_position(TICKER)
    fake_broker.set_quote(TICKER, last=21.00, bid=20.99, ask=21.01)

    order_id = signals_notify._attempt_automated_exit_sell(pos, 'SL', current_price=21.00)
    assert order_id is not None
    placed = _ticker_orders(fake_broker)[0]
    # 22.00 * (1 - 5%) = 20.90, NOT 20.00 * 0.95 = 19.00
    assert placed['stopPrice'] == pytest.approx(20.90), (
        f"restore-stop must anchor to entry_price (22.00), not signal_price "
        f"(20.00) -- got stopPrice={placed['stopPrice']}"
    )


def test_sl_restore_stop_placement_failure_falls_back_to_market_when_already_breached(env, fake_broker, monkeypatch):
    """The already-breached self-correcting fallback (signals_notify.py,
    _attempt_automated_exit_sell's except branch for reason='SL' with no
    resting order): if the restore-stop placement itself fails AND a fresh
    price recheck shows the market has already crossed the target stop, a
    real market SELL fires instead -- mirroring _place_stop_loss_for_position's
    established entry-time fallback. fake_broker's place_order does no
    stop-side validation (a stop order is always accepted regardless of
    price), so the placement failure has to be forced directly rather than
    relying on a natural broker rejection."""
    node = _node()
    entry_time = datetime(2026, 8, 18, 9, 31, 0)
    signals_db.open_position(node, signal_price=20.00, signal_time=entry_time,
                              entry_price=20.00, entry_time=entry_time, shares=100)
    pos = signals_db.get_open_position(TICKER)
    assert pos['sl_order_id'] is None

    fake_broker.set_quote(TICKER, last=18.50, bid=18.49, ask=18.51)

    real_place_stop_loss = schwab_client.place_stop_loss
    calls = {'n': 0}

    def _fail_once(*a, **kw):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError("simulated broker rejection: stop must be on the correct side of the market")
        return real_place_stop_loss(*a, **kw)

    monkeypatch.setattr(schwab_client, 'place_stop_loss', _fail_once)
    # Fresh recheck price is already through the target stop (19.00 =
    # 20.00 * 0.95) -- the fallback should fire a market SELL instead of
    # giving up.
    monkeypatch.setattr(schwab_client, 'get_current_price', lambda ticker: 18.50)

    order_id = signals_notify._attempt_automated_exit_sell(pos, 'SL', current_price=18.50)
    assert order_id is not None, "expected the already-breached market-sell fallback to fire"

    orders = _ticker_orders(fake_broker)
    assert len(orders) == 1, f"expected exactly one order (the fallback market SELL), found: {orders}"
    fallback = orders[0]
    assert fallback['orderType'] == 'MARKET', (
        f"expected the already-breached fallback to be a MARKET sell, got {fallback['orderType']}"
    )
    assert fallback['orderId'] == order_id
    assert fallback['orderLegCollection'][0]['quantity'] == 100

    restored_events = signals_db.get_coverage_events(scenario_key='automated_exit_execution')
    already_breached = [e for e in restored_events
                         if e['ticker'] == TICKER and e['result'] == 'placed_as_market_already_breached']
    assert already_breached, "expected a placed_as_market_already_breached automated_exit_execution event"


def test_sl_noop_via_notify_sell_signal_does_not_poison_exit_pending(env, fake_broker, monkeypatch):
    """Regression for a real bug caught by paired review (2026-08-19): the
    SL no-op must NOT write trail_state['exit_pending'] -- doing so would let
    _attempt_automated_exit_sell's generic pending_order_id reuse guard
    short-circuit every LATER exit attempt for this position (including a
    genuine TIME/TP exit on a subsequent poll) straight back to the resting
    stop's id, permanently blocking the real exit. Exercised through
    notify_sell_signal (not _attempt_automated_exit_sell directly), since
    that's the only call site that ever writes exit_pending."""
    node = _node()
    entry_time = datetime(2026, 8, 18, 9, 31, 0)
    signals_db.open_position(node, signal_price=20.00, signal_time=entry_time,
                              entry_price=20.00, entry_time=entry_time, shares=100)
    pos = signals_db.get_open_position(TICKER)

    fake_broker.set_quote(TICKER, last=19.50, bid=19.49, ask=19.51)
    sl_order_id = fake_broker.seed_resting_order(
        'soxl_ira', TICKER, 'STOP', 'SELL', 100, stop_price=19.00)
    signals_db.set_sl_order_id_by_position(pos['id'], sl_order_id)
    pos = signals_db.get_open_position(TICKER)

    posted = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (posted.append(a or kw), (None, None))[1])

    signals_notify.notify_sell_signal(pos, 'SL', current_price=19.50, target_price=19.00)

    after = signals_db.get_open_position(TICKER)
    assert after is not None, "position should stay open -- the resting stop wasn't touched"
    assert (after.get('trail_state') or {}).get('exit_pending') is None, (
        "SL no-op must not write exit_pending -- would poison the generic "
        "pending_order_id reuse guard for every later exit reason"
    )
    assert not posted, "SL no-op is routine -- no Slack alert expected"
    assert fake_broker.orders[sl_order_id]['status'] == 'WORKING'


def test_sl_noop_does_not_block_a_later_time_exit(env, fake_broker, monkeypatch):
    """Follow-on to the exit_pending poisoning regression above: after an SL
    no-op, a genuine LATER exit for a different reason (TIME) must still
    place its own real market exit, not get silently swallowed by a stale
    exit_pending entry left over from the SL no-op."""
    node = _node()
    entry_time = datetime(2026, 8, 18, 9, 31, 0)
    signals_db.open_position(node, signal_price=20.00, signal_time=entry_time,
                              entry_price=20.00, entry_time=entry_time, shares=100)
    pos = signals_db.get_open_position(TICKER)

    fake_broker.set_quote(TICKER, last=19.50, bid=19.49, ask=19.51)
    sl_order_id = fake_broker.seed_resting_order(
        'soxl_ira', TICKER, 'STOP', 'SELL', 100, stop_price=19.00)
    signals_db.set_sl_order_id_by_position(pos['id'], sl_order_id)
    pos = signals_db.get_open_position(TICKER)

    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))
    # First poll: SL condition true, resting stop untouched (no-op).
    signals_notify.notify_sell_signal(pos, 'SL', current_price=19.50, target_price=19.00)
    pos = signals_db.get_open_position(TICKER)
    assert (pos.get('trail_state') or {}).get('exit_pending') is None

    # Later poll: hold-time has expired (unarmed TIME exit) -- must still
    # force a real market exit, replacing the still-resting SL.
    signals_notify.notify_sell_signal(pos, 'TIME', current_price=19.50, target_price=19.00)

    assert fake_broker.orders[sl_order_id]['status'] == 'REPLACED', (
        "the TIME exit must have replaced the resting SL with a market sell -- "
        "if this is still WORKING, the SL no-op silently blocked the later TIME exit"
    )
    market_sells = [o for o in _ticker_orders(fake_broker)
                     if o['orderType'] == 'MARKET' and o['status'] == 'FILLED']
    assert len(market_sells) == 1
    assert signals_db.get_open_position(TICKER) is None, "position should be closed on the TIME exit's fill"
