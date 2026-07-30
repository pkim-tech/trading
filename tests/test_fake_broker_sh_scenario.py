"""First real scenario built on tests/fake_broker.py -- reproduces the SH
TIME-while-armed incident (2026-07-29) against a real, non-dry_run order-book
sequence instead of a single mocked function call. This is exactly the shape
of test the existing per-function-mock style couldn't produce: it exercises
_attempt_automated_exit_sell's TRAIL branch deciding whether to reuse an
already-resting order or force a fresh market exit, against a broker that
actually tracks that resting order's real state.

Root cause under test: strategies.py's TrailingBothZScoreBreakout.check_exit
collapses two different trailing-branch conditions (a genuine trail-stop
breach, and hold-time expiring while armed) into the same WIN/LOSS reason,
which signals_compute.py further collapses to 'TRAIL'. notify_sell_signal's
automated-exit path can't tell them apart, so it always treats 'TRAIL' as
"a resting trailing-sell order is already correctly tracking this, just poll
it" -- correct for a genuine breach, wrong when the real trigger was hold-time
expiry and the resting order (e.g. a wide 50% trail) is nowhere near firing."""
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
                         stop_loss=0, max_hold_hours=24, mode='live',
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
