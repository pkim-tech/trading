"""fake_broker scenarios for the real add-on leg's lockstep close
(signals_notify.close_addon_leg_real_if_open), Part 7 of docs/plans/
real_order_execution_drought_addon.md.

Covers: parent SL/TRAIL/TIME exit closes the leg in lockstep with the
parent's reason; a leg still 'placed' (unfilled) when the parent exits is
cancelled, never sold (shares never bought); a cancel race to FILLED falls
through to a real SELL instead; check_addon_leg_reconciliation's orphaned-leg
alert (parent already closed, leg still open) fires loudly and never
auto-closes."""
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

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_ADDON_LOCKSTEP_SCENARIO'
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
    monkeypatch.setattr(schwab_safety, 'NODE_BREAKER_PATH', tmp_path / "schwab_node_breaker_state.json")
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_node_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: IN_WINDOW_TIME)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account='soxl_ira', starting_notional=2000)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET addon_enabled=1 WHERE ticker=?", (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _open_core_position(node, shares=20, entry_price=50.0):
    now = datetime.now()
    signals_db.open_position(node, signal_price=entry_price, signal_time=now, entry_price=entry_price,
                              entry_time=now, shares=shares)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='soxl_ira' WHERE ticker=?", (node['ticker'],))
        c.commit()
    return signals_db.get_open_position(node['ticker'])


def _real_orders(fake_broker_, ticker, side=None):
    out = []
    for o in fake_broker_.orders.values():
        leg = o['orderLegCollection'][0]
        if leg['instrument']['symbol'] != ticker:
            continue
        if side is not None and leg['instruction'] != side:
            continue
        out.append(o)
    return out


def test_open_filled_leg_closes_in_lockstep_via_real_market_sell(env, fake_broker):
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    pos = _open_core_position(node)
    leg_id = signals_db.open_addon_leg(pos, shares=20, entry_price=51.0, entry_time=datetime.now(),
                                        paper=False, entry_status='filled')

    signals_notify.close_addon_leg_real_if_open(pos, exit_price=53.0, exit_reason='SL', exit_time=datetime.now())

    sells = _real_orders(fake_broker, TICKER, side='SELL')
    assert len(sells) == 1
    assert sells[0]['orderLegCollection'][0]['quantity'] == 20
    assert signals_db.get_open_addon_leg_by_parent(pos['id']) is None
    events = signals_db.get_coverage_events(scenario_key='addon_exit_fill')
    assert any(e['ticker'] == TICKER and e['result'] == 'closed' for e in events)


def test_lockstep_close_succeeds_after_parent_position_already_deleted(env, fake_broker):
    """The real call-site ordering (all 7 production sites): db.close_position
    deletes the parent's open_positions row BEFORE close_addon_leg_real_if_open
    ever runs (Part 7's own stated order -- "called AFTER the core exit's own
    coverage event/alert"). A first version of this fix based the SELL-side
    is_addon_leg exemption on the PARENT's still-open position
    (get_open_addon_leg_by_parent(pos['id'])), which is always gone by this
    point -- schwab_safety.check_order's fail-closed no-position guard then
    blocked the leg's own real exit SELL 100% of the time (CRITICAL, found by
    cold Opus review). This test drives the exact real ordering to prove the
    node-scoped fix actually closes the leg, not just the isolated function."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    pos = _open_core_position(node)
    signals_db.open_addon_leg(pos, shares=20, entry_price=51.0, entry_time=datetime.now(),
                               paper=False, entry_status='filled')

    signals_db.close_position(pos['id'], exit_signal_price=53.0, exit_price=53.0,
                               exit_time=datetime.now(), exit_reason='SL')
    assert signals_db.get_open_position(TICKER) is None, "parent row must be gone before the leg close runs"

    signals_notify.close_addon_leg_real_if_open(pos, exit_price=53.0, exit_reason='SL', exit_time=datetime.now())

    sells = _real_orders(fake_broker, TICKER, side='SELL')
    assert len(sells) == 1, (
        "the leg's real exit SELL must succeed even though the parent's open_positions "
        "row is already deleted -- if this is empty, the is_addon_leg SELL exemption is "
        "still parent-position-scoped instead of node-scoped"
    )
    assert signals_db.get_open_addon_legs(paper=False) == []


def test_leg_still_placed_when_parent_exits_is_cancelled_never_sold(env, fake_broker):
    """Never sell shares never bought."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    pos = _open_core_position(node)
    entry_order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'MARKET', 'BUY', 20)
    # A MARKET order fake_broker fills immediately on placement, so seed it
    # as a still-resting order and mark the leg 'placed' to simulate the
    # slow window between real placement and confirmation.
    fake_broker.orders[entry_order_id]['status'] = 'WORKING'
    signals_db.open_addon_leg(pos, shares=20, entry_price=51.0, entry_time=datetime.now(),
                               paper=False, entry_order_id=entry_order_id, entry_status='placed')

    signals_notify.close_addon_leg_real_if_open(pos, exit_price=53.0, exit_reason='SL', exit_time=datetime.now())

    assert fake_broker.orders[entry_order_id]['status'] == 'CANCELED'
    sells = _real_orders(fake_broker, TICKER, side='SELL')
    assert len(sells) == 0, "must never place a real SELL for shares that were never actually bought"
    assert signals_db.get_open_addon_leg_by_parent(pos['id']) is None
    events = signals_db.get_coverage_events(scenario_key='addon_exit_placement')
    assert any(e['ticker'] == TICKER and e['result'] == 'cancelled_unfilled_leg' for e in events)


def test_leg_entry_cancel_races_to_filled_falls_through_to_real_sell(env, fake_broker):
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    pos = _open_core_position(node)
    entry_order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'MARKET', 'BUY', 20, status='WORKING')
    signals_db.open_addon_leg(pos, shares=20, entry_price=51.0, entry_time=datetime.now(),
                               paper=False, entry_order_id=entry_order_id, entry_status='placed')
    fake_broker.force_fill(entry_order_id, price=51.2)

    signals_notify.close_addon_leg_real_if_open(pos, exit_price=53.0, exit_reason='SL', exit_time=datetime.now())

    sells = _real_orders(fake_broker, TICKER, side='SELL')
    assert len(sells) == 1, "a raced fill must fall through to a real SELL, not silently drop the leg"
    assert signals_db.get_open_addon_leg_by_parent(pos['id']) is None


def test_close_is_a_noop_with_no_open_leg(env, fake_broker):
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    pos = _open_core_position(node)

    signals_notify.close_addon_leg_real_if_open(pos, exit_price=53.0, exit_reason='SL', exit_time=datetime.now())

    assert len(_real_orders(fake_broker, TICKER)) == 0


def test_orphaned_leg_alerted_loudly_never_auto_closed(env, fake_broker):
    """check_addon_leg_reconciliation's second check: a leg still open whose
    parent already closed (the lockstep close was missed somewhere) must be
    surfaced, never guessed-closed."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    pos = _open_core_position(node)
    signals_db.open_addon_leg(pos, shares=20, entry_price=51.0, entry_time=datetime.now(),
                               paper=False, entry_status='filled')
    # Parent closes WITHOUT going through close_addon_leg_real_if_open --
    # simulates a missed lockstep call.
    signals_db.close_position(pos['id'], exit_signal_price=53.0, exit_price=53.0,
                               exit_time=datetime.now(), exit_reason='SL')

    signals_notify.check_addon_leg_reconciliation([])

    leg = signals_db.get_open_addon_legs(paper=False)
    assert len(leg) == 1, "the orphaned leg must stay open, never auto-closed at a guessed price"
    events = signals_db.get_coverage_events(scenario_key='addon_leg_reconciliation')
    assert any(e['ticker'] == TICKER and e['result'] == 'orphaned_leg_parent_closed' for e in events)


def test_placed_leg_past_timeout_is_cancelled_and_marked_abandoned(env, fake_broker, monkeypatch):
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    pos = _open_core_position(node)
    entry_order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'MARKET', 'BUY', 20, status='WORKING')
    old_time = (datetime.now()).strftime('%Y-%m-%d %H:%M:%S')
    signals_db.open_addon_leg(pos, shares=20, entry_price=51.0, entry_time=old_time,
                               paper=False, entry_order_id=entry_order_id, entry_status='placed')
    monkeypatch.setattr(signals_notify, '_ADDON_LEG_ENTRY_TIMEOUT_MINUTES', -1)  # force "past timeout"

    signals_notify.check_addon_leg_reconciliation([])

    assert fake_broker.orders[entry_order_id]['status'] == 'CANCELED'
    legs = signals_db.get_open_addon_legs(paper=False)
    assert len(legs) == 0
    events = signals_db.get_coverage_events(scenario_key='addon_leg_reconciliation')
    assert any(e['ticker'] == TICKER and e['result'] == 'abandoned' for e in events)


def test_leg_reconciliation_never_independently_closes_a_healthy_open_leg(env, fake_broker):
    """The plan's own framing: a leg has no independent exit condition of its
    own -- the ONLY code paths that ever close it are the lockstep close
    (parent exits) and reconciliation's orphan-alert (parent already gone).
    A leg that's simply open with its parent ALSO still open, regardless of
    how far price has moved against it, must be left completely alone by
    check_addon_leg_reconciliation -- no SELL order, no state change. (The
    leg's own D3 resting STOP is a broker-side safety net, not a decision
    this code makes on a poll.)"""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    pos = _open_core_position(node)
    leg_id = signals_db.open_addon_leg(pos, shares=20, entry_price=51.0, entry_time=datetime.now(),
                                        paper=False, entry_status='filled')

    # A large adverse move against the leg -- if any independent notional-
    # based exit existed, this is exactly the input that would trigger it.
    fake_broker.set_quote(TICKER, last=30.0, bid=29.99, ask=30.01)
    signals_notify.check_addon_leg_reconciliation([])

    assert len(_real_orders(fake_broker, TICKER, side='SELL')) == 0
    leg = signals_db.get_open_addon_leg_by_parent(pos['id'])
    assert leg is not None and leg['id'] == leg_id and leg['entry_status'] == 'filled', (
        "the leg must be completely unchanged -- reconciliation is pure observation"
    )


def test_reconciliation_sees_no_false_share_mismatch_with_an_open_leg(env, fake_broker):
    """check_live_state_reconciliation's own add-on patch (signals_notify.py:
    522-531) -- with a real leg open, the broker legitimately holds core+leg
    shares (same ticker/account, two separate real fills), so expected_shares
    must be widened to match before comparing, or this would false-positive a
    'shares mismatch' on every single poll after a real add-on fires and feed
    schwab_safety.record_node_streak's mismatch streak on a healthy position."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    # Real core BUY fill at the broker (20 shares) -- mirrors test_fake_broker_
    # reconciliation_reporting_scenario.py's pattern for exercising
    # get_real_position's actual parsing logic end-to-end, not a mocked return.
    _, core_oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 20, 51.0)
    fake_broker.force_fill(core_oid, 51.0)
    pos = _open_core_position(node, shares=20, entry_price=51.0)
    # is_addon_leg's own precondition #2 requires the parent to be armed --
    # arm it directly (not via notify_trailing_activated, which would place
    # its own extra SELL order this test doesn't care about).
    signals_db.update_position_trail_state(pos['id'], {'trailing': True, 'peak': 51.0})
    pos = signals_db.get_open_position(TICKER)

    # Real add-on leg BUY fill at the broker (another 20 shares, same ticker/account).
    _, leg_oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 20, 52.0, is_addon_leg=True)
    fake_broker.force_fill(leg_oid, 52.0)
    signals_db.open_addon_leg(pos, shares=20, entry_price=52.0, entry_time=datetime.now(),
                               paper=False, entry_order_id=leg_oid, entry_status='filled')

    assert schwab_client.get_real_position('soxl_ira', TICKER) == 40, (
        "sanity check: broker should show core+leg combined"
    )

    signals_notify.check_live_state_reconciliation([signals_db.get_open_position(TICKER)])

    events = signals_db.get_coverage_events(scenario_key='reconciliation_mismatch')
    assert not any(e['ticker'] == TICKER and e.get('result') == 'shares' for e in events), (
        "an open leg must never produce a false shares mismatch -- expected_shares "
        "must be widened by the leg's own share count before comparing"
    )
