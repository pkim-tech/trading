"""First real scenario built on tests/fake_broker.py -- reproduces the SH
TIME-while-armed incident (2026-07-29) against a real, non-dry_run order-book
sequence instead of a single mocked function call. This is exactly the shape
of test the existing per-function-mock style couldn't produce: it exercises
_attempt_automated_exit_sell's TRAIL branch deciding whether to reuse an
already-resting order or force a fresh market exit, against a broker that
actually tracks that resting order's real state.

Root cause under test: strategies.py's TrailingBothZScoreBreakout.check_exit
collapses two different trailing-branch conditions (a genuine trail-stop
breach, and hold-time expiring while armed) into the same WIN/LOSS reason.
Before 2026-08-01, signals_compute.py further collapsed BOTH to 'TRAIL',
indistinguishable from a genuine breach to any human-facing consumer (Slack
messages, trade_log) -- fixed to report 'TIME' for the hold-time-forced case
specifically (see the exit_forced_by_hold_time flag). The tests below still
pass 'TRAIL' as the literal reason string to notify_sell_signal in several
places -- this is intentional, not stale: _attempt_automated_exit_sell keeps
an explicit `and not hold_time_forced` guard as defense-in-depth alongside
the reason string (never trusted the string alone even before this fix), so
these tests double as regression coverage for that guard working correctly
regardless of what the caller's reason string says. See
test_check_sell_condition_reports_time_not_trail_for_hold_time_forced_exit
below for the real end-to-end proof that signals_compute.py itself now
produces 'TIME', not 'TRAIL', for this case."""
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

TICKER = 'TEST_SH_SCENARIO'


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
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 7, 29, 10, 30))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    # Real 'soxl_ira' account config, but this test never touches the real
    # network -- fake_broker patches schwab_client._get_client() before any
    # of this runs, so exercising the actual non-dry_run code path is safe.
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=50,
                         stop_loss=0, max_hold_hours=24, state='live',
                         trail_buy_pct=1.0, trail_pct=50.0, fixed_sl_override=50.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='soxl_ira', arm_sell_pct=0.3, trail_sell_pct=50.0 "
                   "WHERE ticker=?", (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def test_armed_position_past_max_hold_gets_forced_market_exit(env, fake_broker, monkeypatch):
    """Reproduces the real SH state: armed (trailing=True), held well past
    max_hold_hours, a wide (50%) trailing-sell order resting and nowhere near
    triggering. The backtest kernel assumes an immediate exit in this case
    (strategies.py TrailingBothZScoreBreakout.check_exit: exit_px = cp when
    held >= max_hours_to_hold, even inside the trailing branch) -- so the
    real automated-exit path should force a market sell here, not silently
    keep waiting on the passive trailing order.

    Failed against the unfixed code (as of 2026-07-29, before the
    exit_forced_by_hold_time fix in strategies.py/_attempt_automated_exit_sell)
    -- that's what proved the bug was real, not a claim from inspection."""
    node = _node()
    signals_db.add_scenario_expectation = signals_db.add_scenario_expectation  # no-op, keeps import used

    entry_time = datetime(2026, 7, 24, 7, 38, 38)
    signals_db.open_position(node, signal_price=33.52, signal_time=entry_time,
                              entry_price=33.52, entry_time=entry_time, shares=50)
    pos = signals_db.get_open_position(TICKER)

    fake_broker.set_quote(TICKER, last=33.61, bid=33.60, ask=33.62)
    exit_order_id = fake_broker.seed_resting_order(
        'soxl_ira', TICKER, 'TRAILING_STOP', 'SELL', 50, trail_offset=50.0)

    # Confirm strategies.py itself actually produces the hold-time-forced
    # flag for this exact scenario (peak/trail_pct/hours_held/max_hold) --
    # not just assumed, checked directly, so this test doesn't silently
    # bypass the fix the way the original (pre-fix) version of this test did.
    import strategies
    strat = strategies.TrailingBothZScoreBreakout(window=99, trail_pct=0.5)
    check_reason, check_price, check_state = strat.check_exit({
        'current_price': 33.61, 'open': 33.61, 'low': 33.60, 'high': 33.62,
        'entry_price': 33.52, 'take_profit': 0.003, 'stop_loss': 0.5,
        'max_hours_to_hold': 24, 'hours_held': 28, 'at_bar_close': True,
        'state': {'trailing': True, 'peak': 33.72},
    })
    assert check_state.get('exit_forced_by_hold_time') is True, (
        "strategies.py should tag this exit as hold-time-forced (low=33.60 "
        "never crossed trail_stop=33.72*0.5=16.86, only hours_held>=24 did)"
    )

    armed_state = {
        'trailing': True, 'peak': 33.72, 'order_placed': True,
        'exit_order_id': exit_order_id,
        'exit_forced_by_hold_time': check_state['exit_forced_by_hold_time'],
    }
    signals_db.update_position_trail_state(pos['id'], armed_state)
    pos = signals_db.get_open_position(TICKER)

    # Simulate the real strategy's own exit decision: held well past
    # max_hold_hours (24) while still armed -- strategies.py's trailing
    # branch would return WIN/LOSS (collapsed to 'TRAIL') here, exiting at
    # current_price, exactly like a TIME exit would for an unarmed position.
    signals_notify.notify_sell_signal(pos, 'TRAIL', current_price=33.61, target_price=33.61)

    resting_orders_for_ticker = [
        o for o in fake_broker.orders.values()
        if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
        and o['status'] not in ('CANCELED', 'REPLACED')
    ]
    market_sells = [o for o in resting_orders_for_ticker if o['orderType'] == 'MARKET']
    assert market_sells, (
        "expected a forced MARKET sell once hold-time expired while armed, "
        f"but only found: {[(o['orderId'], o['orderType'], o['status']) for o in resting_orders_for_ticker]} "
        "-- this is the real SH bug: reason='TRAIL' from hold-time-expiry is "
        "indistinguishable from a genuine trail-stop breach, so the code just "
        "reused the still-far-away passive trailing order instead of exiting now."
    )


def test_unarmed_position_past_max_hold_replaces_resting_sl_with_market_exit(env, fake_broker, monkeypatch):
    """The other of the two valid TIME-exit setups: position never armed
    (trail_state stays empty, arm/take_profit threshold never reached), a
    real protective STOP order resting far out-of-the-money (matches SH's
    actual documented design intent -- 'a real SL is placed far out-of-the-
    money so it's protected without pre-empting the TIME-exit'). Once held
    hours exceed max_hold_hours, strategies.py's non-trailing branch returns
    'TIME' directly (not collapsed through WIN/LOSS), and
    _attempt_automated_exit_sell's non-TRAIL branch calls
    replace_equity_order_with_market unconditionally -- so this path is
    expected to already work correctly, unlike the TRAIL-variant above."""
    node = _node()
    entry_time = datetime(2026, 7, 24, 7, 38, 38)
    signals_db.open_position(node, signal_price=33.52, signal_time=entry_time,
                              entry_price=33.52, entry_time=entry_time, shares=50)
    pos = signals_db.get_open_position(TICKER)

    fake_broker.set_quote(TICKER, last=33.55, bid=33.54, ask=33.56)
    sl_order_id = fake_broker.seed_resting_order(
        'soxl_ira', TICKER, 'STOP', 'SELL', 50, stop_price=26.57)  # far OTM, matches SH's real design

    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET sl_order_id=? WHERE id=?", (sl_order_id, pos['id']))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    assert pos['trail_state'] == {}, "pre-state: never armed"
    assert pos['sl_order_id'] == sl_order_id

    # Real strategy behavior for an unarmed position past max_hold_hours:
    # strategies.py's non-trailing branch returns 'TIME' directly.
    signals_notify.notify_sell_signal(pos, 'TIME', current_price=33.55, target_price=33.55)

    # --- post-state: full check ---
    old_sl = fake_broker.orders[sl_order_id]
    assert old_sl['status'] == 'REPLACED', \
        f"expected the resting SL to be replaced, got status={old_sl['status']}"

    ticker_orders = [o for o in fake_broker.orders.values()
                      if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER]
    market_sells = [o for o in ticker_orders if o['orderType'] == 'MARKET' and o['status'] == 'FILLED']
    assert len(market_sells) == 1, (
        f"expected exactly one filled MARKET sell replacing the SL, found: "
        f"{[(o['orderId'], o['orderType'], o['status']) for o in ticker_orders]}"
    )
    assert market_sells[0]['orderLegCollection'][0]['quantity'] == 50

    closed_pos = signals_db.get_open_position(TICKER)
    assert closed_pos is None, "position should be auto-closed on confirmed fill"

    time_exit_events = signals_db.get_coverage_events(scenario_key='time_exit_trigger')
    assert any(e['ticker'] == TICKER for e in time_exit_events), \
        "notify_sell_signal(reason='TIME') should log the time_exit_trigger coverage event"


def test_armed_position_past_max_hold_with_failed_arm_placement_still_exits(env, fake_broker, monkeypatch):
    """A third valid armed/hold-time-forced setup, distinct from the first
    test above: arming (trailing=True) succeeded, but the arm-time trailing-
    sell PLACEMENT failed (broker exception / SafetyViolation / automation
    paused) -- signals_compute.check_sell_condition persists 'trailing' state
    independently of whether _attempt_automated_sell's order placement itself
    succeeded, so this is a real reachable state: armed, order_placed=False,
    exit_order_id=None, and the ORIGINAL protective SL still resting at the
    broker (the atomic replace never ran).

    Before the signals_notify.py:211 fix (found live via execution-path
    walkthrough, 2026-07-31), resting_order_id resolved to None here (only
    exit_order_id was ever checked for reason='TRAIL'), so
    _attempt_automated_exit_sell placed a fresh, un-linked market sell with no
    replacing_order_id -- schwab_safety's resting-SELL guard then saw the
    still-live SL and raised SafetyViolation on every attempt, permanently
    self-blocking the hold-time-forced exit exactly like the original SH
    incidents, just through a different precondition."""
    node = _node()
    entry_time = datetime(2026, 7, 24, 7, 38, 38)
    signals_db.open_position(node, signal_price=33.52, signal_time=entry_time,
                              entry_price=33.52, entry_time=entry_time, shares=50)
    pos = signals_db.get_open_position(TICKER)

    fake_broker.set_quote(TICKER, last=33.61, bid=33.60, ask=33.62)
    # The original protective SL, still resting -- the arm-time replace never
    # happened because placement failed.
    sl_order_id = fake_broker.seed_resting_order(
        'soxl_ira', TICKER, 'STOP', 'SELL', 50, stop_price=16.76)  # far OTM, matches SH's real design
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET sl_order_id=? WHERE id=?", (sl_order_id, pos['id']))
        c.commit()

    import strategies
    strat = strategies.TrailingBothZScoreBreakout(window=99, trail_pct=0.5)
    check_reason, check_price, check_state = strat.check_exit({
        'current_price': 33.61, 'open': 33.61, 'low': 33.60, 'high': 33.62,
        'entry_price': 33.52, 'take_profit': 0.003, 'stop_loss': 0.5,
        'max_hours_to_hold': 24, 'hours_held': 28, 'at_bar_close': True,
        'state': {'trailing': True, 'peak': 33.72},
    })
    assert check_state.get('exit_forced_by_hold_time') is True

    # Armed, but the arm-time order placement failed -- no order_placed, no
    # exit_order_id (matches notify_trailing_activated's real persisted state
    # on a failed _attempt_automated_sell).
    armed_state = {
        'trailing': True, 'peak': 33.72,
        'exit_forced_by_hold_time': check_state['exit_forced_by_hold_time'],
    }
    signals_db.update_position_trail_state(pos['id'], armed_state)
    pos = signals_db.get_open_position(TICKER)
    assert pos['trail_state'].get('exit_order_id') is None
    assert pos['sl_order_id'] == sl_order_id

    signals_notify.notify_sell_signal(pos, 'TRAIL', current_price=33.61, target_price=33.61)

    old_sl = fake_broker.orders[sl_order_id]
    assert old_sl['status'] == 'REPLACED', (
        f"expected the still-live SL to be replaced (not left resting while a "
        f"second, unlinked order gets blocked), got status={old_sl['status']}"
    )
    ticker_orders = [o for o in fake_broker.orders.values()
                      if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER]
    market_sells = [o for o in ticker_orders if o['orderType'] == 'MARKET']
    assert market_sells, (
        f"expected a forced MARKET sell replacing the still-live SL, found: "
        f"{[(o['orderId'], o['orderType'], o['status']) for o in ticker_orders]} "
        "-- resting_order_id must fall back to sl_order_id when exit_order_id "
        "is None, or this self-blocks forever against the still-resting SL"
    )


def test_hold_time_forced_exit_does_not_re_replace_on_a_later_bar(env, fake_broker, monkeypatch):
    """Regression test for a composition bug introduced (and caught before
    landing) 2026-07-31: the exit_order_id-refresh fix and the
    still_unreplaced_trail_order reuse guard both read/write exit_order_id,
    which made the guard permanently True after the very first replace (since
    exit_pending['order_id'] and the freshly-refreshed exit_order_id become
    equal) -- causing _attempt_automated_exit_sell to re-issue a brand new
    replace_equity_order_with_market against the SAME still-resting market
    sell on every subsequent bar, forever, until it happened to fill. Fixed
    via a dedicated `hold_time_replaced` flag instead of reusing
    exit_order_id as its own proof-of-replacement. This only reproduces when
    the first replace's fill ISN'T confirmed within notify_sell_signal's
    bounded poll (the real-world case this mechanism exists for at all) --
    fake_broker's default same-tick MARKET fill would mask it, so this test
    disables that auto-fill to model an order still genuinely resting.

    Also advances schwab_safety's real wall-clock time between the two calls
    -- without this, schwab_safety's UNRELATED 60s duplicate-order-fingerprint
    guard (same side/ticker/quantity within DUPLICATE_ORDER_WINDOW_SECS)
    coincidentally blocks a second same-second replace attempt regardless of
    whether the still_unreplaced_trail_order bug is present, producing a
    false pass either way (confirmed by hand: this test passed against the
    deliberately-reintroduced buggy comparison-based guard until this fix,
    because both calls landed within the same wall-clock second). Real bars
    are an hour apart, so bridging that window here is the faithful setup,
    not a workaround for the test alone."""
    monkeypatch.setattr(fake_broker, '_maybe_immediate_fill', lambda o: None)
    import schwab_safety
    _clock = {'t': 1_800_000_000.0}
    monkeypatch.setattr(schwab_safety.time, 'time', lambda: _clock['t'])

    node = _node()
    entry_time = datetime(2026, 7, 24, 7, 38, 38)
    signals_db.open_position(node, signal_price=33.52, signal_time=entry_time,
                              entry_price=33.52, entry_time=entry_time, shares=50)
    pos = signals_db.get_open_position(TICKER)

    fake_broker.set_quote(TICKER, last=33.61, bid=33.60, ask=33.62)
    exit_order_id = fake_broker.seed_resting_order(
        'soxl_ira', TICKER, 'TRAILING_STOP', 'SELL', 50, trail_offset=50.0)

    armed_state = {
        'trailing': True, 'peak': 33.72, 'order_placed': True,
        'exit_order_id': exit_order_id, 'exit_forced_by_hold_time': True,
    }
    signals_db.update_position_trail_state(pos['id'], armed_state)
    pos = signals_db.get_open_position(TICKER)

    def _ticker_orders():
        return [o for o in fake_broker.orders.values()
                if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER]

    def _market_sells():
        return [o for o in _ticker_orders() if o['orderType'] == 'MARKET']

    # Bar 1: force-replace fires, order isn't (in this test) confirmed
    # filled -- exit_pending gets set, exit_order_id refreshed, and the new
    # hold_time_replaced flag set.
    signals_notify.notify_sell_signal(pos, 'TRAIL', current_price=33.61, target_price=33.61)
    first_pass_market_sells = _market_sells()
    assert len(first_pass_market_sells) == 1, (
        f"expected exactly one MARKET sell after the first forced replace, "
        f"found {len(first_pass_market_sells)}: {[o['orderId'] for o in first_pass_market_sells]}"
    )
    first_order_id = first_pass_market_sells[0]['orderId']

    pos_after_bar1 = signals_db.get_open_position(TICKER)
    assert pos_after_bar1['trail_state'].get('hold_time_replaced') is True
    assert pos_after_bar1['trail_state'].get('exit_order_id') == first_order_id
    assert pos_after_bar1['trail_state'].get('exit_pending', {}).get('order_id') == first_order_id

    # Bar 2: condition is still true (still armed, still hold-time-forced,
    # still no confirmed fill) -- this must reuse the existing pending order,
    # NOT place a second replace against the still-resting market sell.
    # Advance well past DUPLICATE_ORDER_WINDOW_SECS (60s) and real bar
    # spacing (1h) so only still_unreplaced_trail_order's own logic is under
    # test, not an unrelated guard incidentally blocking a same-second retry.
    _clock['t'] += 3600
    signals_notify.notify_sell_signal(pos_after_bar1, 'TRAIL', current_price=33.61, target_price=33.61)

    second_pass_market_sells = _market_sells()
    assert len(second_pass_market_sells) == 1, (
        f"expected NO additional MARKET sell on a second bar with the condition still true, "
        f"but found {len(second_pass_market_sells)}: "
        f"{[(o['orderId'], o['status']) for o in second_pass_market_sells]} -- "
        "the still_unreplaced_trail_order guard is re-firing every bar instead of "
        "correctly recognizing the order was already replaced once"
    )
    assert second_pass_market_sells[0]['orderId'] == first_order_id, (
        "the single resting MARKET sell must still be the SAME order from bar 1, "
        "not a second one that replaced it"
    )


def test_check_sell_condition_reports_time_not_trail_for_hold_time_forced_exit(env):
    """Direct proof that signals_compute.check_sell_condition -- the real
    production function every live/paper/dry-run exit check calls, not just
    strategies.py's raw check_exit -- reports reason='TIME' for a
    hold-time-forced exit, not 'TRAIL'. This is the actual fix (2026-08-01);
    every other test in this file exercises the downstream routing (which
    was always robust to either string via the hold_time_forced flag), not
    this label itself."""
    import signals_compute
    import pandas as pd

    node = _node()
    entry_time = datetime(2026, 7, 24, 7, 38, 38)
    signals_db.open_position(node, signal_price=33.52, signal_time=entry_time,
                              entry_price=33.52, entry_time=entry_time, shares=50)
    pos = signals_db.get_open_position(TICKER)
    signals_db.update_position_trail_state(pos['id'], {'trailing': True, 'peak': 33.72})
    pos = signals_db.get_open_position(TICKER)

    # _bars_held just counts df_hourly rows with index > signal_time -- no
    # real cache file exists for this synthetic ticker, so pass a minimal
    # synthetic frame directly (check_sell_condition accepts df_hourly as an
    # override) with 30 hourly bars past signal_time, comfortably over
    # max_hold_hours=24.
    bars = pd.date_range(entry_time, periods=31, freq='h')[1:]
    df_hourly = pd.DataFrame({'Open': 33.61, 'High': 33.62, 'Low': 33.60, 'Close': 33.61}, index=bars)

    # Same real scenario as the first test above: armed, held well past
    # max_hold_hours (24), low never crosses the 50%-wide trail_stop -- a
    # genuine hold-time-forced exit, not a real breach.
    reason, price, just_activated = signals_compute.check_sell_condition(
        pos, current_price=33.61, now=datetime(2026, 7, 25, 15, 30, 0),
        at_bar_close=True, low=33.60, high=33.62, open_price=33.61, df_hourly=df_hourly,
    )
    assert reason == 'TIME', (
        f"expected 'TIME' for a hold-time-forced exit (armed, but low=33.60 "
        f"never crossed the real trail_stop), got {reason!r} -- this is the "
        f"actual regression this whole fix exists to prevent"
    )
    closed_pos = signals_db.get_open_position(TICKER)
    assert closed_pos['trail_state'].get('exit_forced_by_hold_time') is True
