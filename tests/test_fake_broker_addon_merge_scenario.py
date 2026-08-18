"""fake_broker scenarios for Part 8 (docs/backlog_cache.md, 2026-08-17): the
merged-order design for a core position with an open add-on leg. User's
explicit call -- fold the leg's own real resting stop into the SAME
trailing-sell order the core's own arm event already places
(_attempt_automated_sell), instead of maintaining two independent resting
orders for the same ticker/account (which guarantees an orderType+quantity
collision on the leg-close side, since an add-on always sizes shares==core's
shares). A separate-independent-trailing-order design for the leg was
explicitly rejected for this reason.

Covers: arm-time merge (leg's own STOP cancelled, core's replace/placement
carries core_shares+leg_shares, leg row marked merged_into_core=1, leg's own
sl_order_id cleared); the merged leg's later close being bookkeeping-only
(no second broker order -- signals_notify.close_addon_leg_real_if_open);
the pre-arm case where the leg has no real resting stop yet (nothing to
merge, falls through to the unmerged single-quantity path unchanged); and a
cancel racing a genuine fill of the leg's own stop (must not merge, must not
touch the leg's DB row)."""
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

TICKER = 'TEST_ADDON_MERGE_SCENARIO'
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
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account='soxl_ira', starting_notional=2000)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='soxl_ira', addon_enabled=1 WHERE ticker=?", (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _open_core_position(node, shares=100, entry_price=50.0):
    now = datetime.now()
    signals_db.open_position(node, signal_price=entry_price, signal_time=now, entry_price=entry_price,
                              entry_time=now, shares=shares)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='soxl_ira' WHERE ticker=?", (node['ticker'],))
        c.commit()
    return signals_db.get_open_position(node['ticker'])


def _real_sell_orders(fake_broker_, ticker):
    out = []
    for o in fake_broker_.orders.values():
        leg = o['orderLegCollection'][0]
        if leg['instrument']['symbol'] == ticker and leg['instruction'] == 'SELL':
            out.append(o)
    return out


def test_arm_merges_open_addon_leg_stop_into_core_trailing_sell(env, fake_broker):
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    pos = _open_core_position(node, shares=100, entry_price=50.0)
    core_sl_order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'STOP', 'SELL', 100, stop_price=49.50)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET sl_order_id=? WHERE ticker=?", (core_sl_order_id, TICKER))
        c.commit()
    pos = signals_db.get_open_position(TICKER)

    leg_id = signals_db.open_addon_leg(pos, shares=20, entry_price=51.0, entry_time=datetime.now(),
                                        paper=False, entry_status='filled')
    _, leg_sl_order_id = schwab_client.place_stop_loss('soxl_ira', TICKER, 20, 45.0, is_addon_leg=True,
                                                         node_dry_run=False, node_id=node['id'])
    signals_db.set_addon_leg_sl_order_id(leg_id, leg_sl_order_id, broker_stop_price=45.0)

    signals_notify.notify_trailing_activated(pos, current_price=52.0)

    # Both the core's old STOP and the leg's own old STOP must be gone --
    # only one new merged TRAILING_STOP resting for the combined quantity.
    assert fake_broker.orders[core_sl_order_id]['status'] == 'REPLACED'
    assert fake_broker.orders[leg_sl_order_id]['status'] == 'CANCELED'
    trailing_sells = [o for o in fake_broker.orders.values()
                       if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                       and o['orderType'] == 'TRAILING_STOP' and o['status'] == 'WORKING']
    assert len(trailing_sells) == 1
    assert trailing_sells[0]['orderLegCollection'][0]['quantity'] == 120, (
        "merged order must carry core_shares (100) + leg_shares (20) = 120, not just the core's own 100"
    )

    leg = signals_db.get_open_addon_leg_by_parent(pos['id'])
    assert leg['merged_into_core'] == 1
    assert leg['sl_order_id'] is None, "leg's own dead order id must be cleared, not left pointing at a cancelled order"

    events = signals_db.get_coverage_events(scenario_key='addon_leg_merge')
    assert any(e['ticker'] == TICKER and e['result'] == 'merged' for e in events)


def test_merged_leg_close_is_bookkeeping_only_no_second_broker_order(env, fake_broker):
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    pos = _open_core_position(node, shares=100, entry_price=50.0)
    core_sl_order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'STOP', 'SELL', 100, stop_price=49.50)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET sl_order_id=? WHERE ticker=?", (core_sl_order_id, TICKER))
        c.commit()
    pos = signals_db.get_open_position(TICKER)

    leg_id = signals_db.open_addon_leg(pos, shares=20, entry_price=51.0, entry_time=datetime.now(),
                                        paper=False, entry_status='filled')
    _, leg_sl_order_id = schwab_client.place_stop_loss('soxl_ira', TICKER, 20, 45.0, is_addon_leg=True,
                                                         node_dry_run=False, node_id=node['id'])
    signals_db.set_addon_leg_sl_order_id(leg_id, leg_sl_order_id, broker_stop_price=45.0)

    # Drive the real arm merge first (same as the test above) so the leg is
    # genuinely in the merged_into_core state this scenario needs.
    signals_notify.notify_trailing_activated(pos, current_price=52.0)
    pos = signals_db.get_open_position(TICKER)
    leg = signals_db.get_open_addon_leg_by_parent(pos['id'])
    assert leg['merged_into_core'] == 1  # sanity check on the setup

    sell_orders_before = len(_real_sell_orders(fake_broker, TICKER))

    signals_notify.close_addon_leg_real_if_open(pos, exit_price=53.0, exit_reason='TRAIL', exit_time=datetime.now())

    sell_orders_after = len(_real_sell_orders(fake_broker, TICKER))
    assert sell_orders_after == sell_orders_before, (
        "a merged leg's close must not place any new broker order -- it already closed "
        "as part of the core's own single merged fill"
    )
    assert signals_db.get_open_addon_leg_by_parent(pos['id']) is None
    closed_leg = signals_db.get_open_addon_legs(paper=False)
    assert closed_leg == []
    events = signals_db.get_coverage_events(scenario_key='addon_exit_fill')
    matches = [e for e in events if e['ticker'] == TICKER and e['result'] == 'merged_closed']
    assert len(matches) == 1


def test_leg_without_own_real_stop_is_not_merged_at_arm(env, fake_broker):
    """Pre-arm case: the leg's fill just confirmed but its own D3 stop hasn't
    landed yet (leg['sl_order_id'] is still None) -- nothing real to fold in,
    so arm must fall through to the ordinary un-merged single-quantity path,
    exactly as it did before Part 8."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    pos = _open_core_position(node, shares=100, entry_price=50.0)
    core_sl_order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'STOP', 'SELL', 100, stop_price=49.50)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET sl_order_id=? WHERE ticker=?", (core_sl_order_id, TICKER))
        c.commit()
    pos = signals_db.get_open_position(TICKER)

    leg_id = signals_db.open_addon_leg(pos, shares=20, entry_price=51.0, entry_time=datetime.now(),
                                        paper=False, entry_status='filled')
    assert signals_db.get_open_addon_leg_by_parent(pos['id'])['sl_order_id'] is None

    signals_notify.notify_trailing_activated(pos, current_price=52.0)

    trailing_sells = [o for o in fake_broker.orders.values()
                       if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                       and o['orderType'] == 'TRAILING_STOP' and o['status'] == 'WORKING']
    assert len(trailing_sells) == 1
    assert trailing_sells[0]['orderLegCollection'][0]['quantity'] == 100, (
        "no real leg stop to merge -- the trailing-sell must still carry only the core's own shares"
    )

    leg = signals_db.get_open_addon_leg_by_parent(pos['id'])
    assert leg['id'] == leg_id
    assert leg['merged_into_core'] == 0
    assert leg['sl_order_id'] is None


def test_merge_cancel_races_leg_stop_fill_skips_merge(env, fake_broker):
    """A genuine fill of the leg's own stop races the merge attempt --
    cancel_order must see status='FILLED', not 'CANCELED', and the merge must
    be skipped entirely rather than guessing. The leg's own fill-reconciliation
    path (check_addon_leg_reconciliation) is what closes it, independently."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    pos = _open_core_position(node, shares=100, entry_price=50.0)
    core_sl_order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'STOP', 'SELL', 100, stop_price=49.50)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET sl_order_id=? WHERE ticker=?", (core_sl_order_id, TICKER))
        c.commit()
    pos = signals_db.get_open_position(TICKER)

    leg_id = signals_db.open_addon_leg(pos, shares=20, entry_price=51.0, entry_time=datetime.now(),
                                        paper=False, entry_status='filled')
    _, leg_sl_order_id = schwab_client.place_stop_loss('soxl_ira', TICKER, 20, 45.0, is_addon_leg=True,
                                                         node_dry_run=False, node_id=node['id'])
    signals_db.set_addon_leg_sl_order_id(leg_id, leg_sl_order_id, broker_stop_price=45.0)
    fake_broker.force_fill(leg_sl_order_id, price=45.0)

    signals_notify.notify_trailing_activated(pos, current_price=52.0)

    trailing_sells = [o for o in fake_broker.orders.values()
                       if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                       and o['orderType'] == 'TRAILING_STOP' and o['status'] == 'WORKING']
    assert len(trailing_sells) == 1
    assert trailing_sells[0]['orderLegCollection'][0]['quantity'] == 100, (
        "a raced fill of the leg's own stop must never be folded into the core's merged quantity"
    )

    leg = signals_db.get_open_addon_leg_by_parent(pos['id'])
    assert leg['merged_into_core'] == 0
    assert leg['sl_order_id'] == leg_sl_order_id, "must not clear the leg's order id off an unconfirmed-cancel/raced-fill path"

    events = signals_db.get_coverage_events(scenario_key='addon_leg_merge')
    assert any(e['ticker'] == TICKER and e['result'] == 'cancel_saw_fill' for e in events)


def _setup_merged_position(node, fake_broker, core_shares=100, leg_shares=20,
                            entry_price=50.0, leg_entry_price=51.0):
    """Shared setup for the rework tests below: opens a core position + addon
    leg, arms (merges) via the real notify_trailing_activated path, and
    returns the fresh pos dict with merged_into_core already confirmed true
    on the leg. The arm-merge mechanics themselves are already covered by
    test_arm_merges_open_addon_leg_stop_into_core_trailing_sell above -- this
    just reaches the same post-merge state as a setup step for tests further
    down the exit-side lifecycle (force-replace exit, re-arm defense,
    check_order bound, reconciliation)."""
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    pos = _open_core_position(node, shares=core_shares, entry_price=entry_price)
    core_sl_order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'STOP', 'SELL', core_shares,
                                                        stop_price=entry_price * 0.99)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET sl_order_id=? WHERE ticker=?", (core_sl_order_id, TICKER))
        c.commit()
    pos = signals_db.get_open_position(TICKER)

    leg_id = signals_db.open_addon_leg(pos, shares=leg_shares, entry_price=leg_entry_price, entry_time=datetime.now(),
                                        paper=False, entry_status='filled')
    _, leg_sl_order_id = schwab_client.place_stop_loss('soxl_ira', TICKER, leg_shares, leg_entry_price * 0.9,
                                                         is_addon_leg=True, node_dry_run=False, node_id=node['id'])
    signals_db.set_addon_leg_sl_order_id(leg_id, leg_sl_order_id, broker_stop_price=leg_entry_price * 0.9)

    signals_notify.notify_trailing_activated(pos, current_price=52.0)
    pos = signals_db.get_open_position(TICKER)
    leg = signals_db.get_open_addon_leg_by_parent(pos['id'])
    assert leg['merged_into_core'] == 1, "setup sanity check: arm-time merge must have succeeded"
    return pos


def test_force_replace_exit_includes_merged_leg_shares_and_closes_correctly(env, fake_broker):
    """CRITICAL repro (paired Opus review, 2026-08-17/18 -- both reviewers
    independently found this via different code paths). A hold-time-forced
    TIME exit through _attempt_automated_exit_sell used to replace the
    merged trailing-sell order with a market SELL sized at CORE-ONLY shares
    -- orphaning the leg's shares at the broker with no resting order at all,
    while close_addon_leg_real_if_open's merged_into_core branch (correctly,
    once the quantity is fixed here) then falsely booked the leg's shares as
    sold too. Real result pre-fix: real shares held at the broker, falsely
    recorded as closed and safe in the local DB.

    Drives the real notify_sell_signal entrypoint end to end -- force-replace
    exit -> fill confirm -> leg close (bookkeeping-only) -> core close -- and
    asserts the actual broker order quantity and both DB rows' final state,
    not just the function's return value."""
    node = _node()
    pos = _setup_merged_position(node, fake_broker)
    merged_order_id = pos['sl_order_id']
    assert merged_order_id is not None

    # Simulate hold-time-forced TIME exit -- armed, hold time expired while
    # trailing. signals_compute.py reports this as reason='TIME' (not
    # 'TRAIL') since 2026-08-01.
    state = dict(pos['trail_state'] or {})
    state['trailing'] = True
    state['exit_forced_by_hold_time'] = True
    state['exit_order_id'] = merged_order_id
    signals_db.update_position_trail_state(pos['id'], state)
    pos = signals_db.get_open_position(TICKER)

    signals_notify.notify_sell_signal(pos, 'TIME', current_price=54.0, target_price=54.0)

    assert fake_broker.orders[merged_order_id]['status'] == 'REPLACED'
    market_sells = [o for o in fake_broker.orders.values()
                     if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                     and o['orderType'] == 'MARKET'
                     and o['orderLegCollection'][0]['instruction'] == 'SELL']
    assert len(market_sells) == 1
    assert market_sells[0]['orderLegCollection'][0]['quantity'] == 120, (
        "force-replace exit quantity must include the merged leg's 20 shares, not just the core's 100 "
        "-- this is the exact CRITICAL bug: pre-fix this asserted 100, orphaning 20 real shares"
    )
    assert market_sells[0]['status'] == 'FILLED'

    assert signals_db.get_open_position(TICKER) is None, "core position must close for real, not stay phantom-open"
    assert signals_db.get_open_addon_leg_by_parent(pos['id']) is None, (
        "leg must close too (one real fill genuinely covered both share counts) -- not stay open, "
        "and not be silently dropped either"
    )

    events = signals_db.get_coverage_events(scenario_key='addon_exit_fill')
    matches = [e for e in events if e['ticker'] == TICKER and e['result'] == 'merged_closed']
    assert len(matches) == 1


def test_second_attempt_automated_sell_call_does_not_shrink_merged_order(env, fake_broker):
    """Defensive guard (finding #1 follow-up, paired review 2026-08-17/18):
    verified NOT reachable in current code (state['trailing'] is monotonic
    in strategies.py -- never reset -- so notify_trailing_activated's
    just_activated_trailing gate fires at most once per position lifecycle).
    Guarded anyway: a second _attempt_automated_sell call against an
    already-merged leg must still fold the leg's shares into the replacement
    quantity, never silently shrink the resting order back to core-only."""
    node = _node()
    pos = _setup_merged_position(node, fake_broker)
    merged_order_id = pos['sl_order_id']

    auto_placed, new_order_id = signals_notify._attempt_automated_sell(pos, current_price=53.0)

    assert auto_placed is True
    assert new_order_id is not None
    new_order = fake_broker.orders[new_order_id]
    assert new_order['orderLegCollection'][0]['quantity'] == 120, (
        "a second call against an already-merged leg must not shrink the resting order back to core-only shares"
    )
    assert fake_broker.orders[merged_order_id]['status'] == 'REPLACED'

    leg_after = signals_db.get_open_addon_leg_by_parent(pos['id'])
    assert leg_after['merged_into_core'] == 1

    events = signals_db.get_coverage_events(scenario_key='addon_leg_merge')
    assert any(e['ticker'] == TICKER and e['result'] == 'already_merged_reused' for e in events)


def test_check_order_sell_bound_pre_merge_tight_post_merge_widened(env, fake_broker):
    """HIGH finding #2 (paired Opus review, 2026-08-17/18): schwab_safety.
    check_order's SELL position-size bound must stay TIGHT (core-only) while
    the leg still has its own real resting stop (pre-merge) -- widening here
    would let core+leg worth of SELL orders through while core+leg worth of
    stops are ALSO simultaneously resting (the leg's own live stop, plus a
    widened core order), a real naked-short exposure this guard exists to
    prevent. Must only widen to core+leg once the leg's own stop is
    confirmed gone (sl_order_id cleared) -- exactly the moment
    _attempt_automated_sell's merge attempt needs it to, per its real
    ordering (clears sl_order_id BEFORE attempting the merged placement)."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    pos = _open_core_position(node, shares=100, entry_price=50.0)
    core_sl_order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'STOP', 'SELL', 100, stop_price=49.50)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET sl_order_id=? WHERE ticker=?", (core_sl_order_id, TICKER))
        c.commit()
    pos = signals_db.get_open_position(TICKER)

    leg_id = signals_db.open_addon_leg(pos, shares=20, entry_price=51.0, entry_time=datetime.now(),
                                        paper=False, entry_status='filled')
    _, leg_sl_order_id = schwab_client.place_stop_loss('soxl_ira', TICKER, 20, 45.0, is_addon_leg=True,
                                                         node_dry_run=False, node_id=node['id'])
    signals_db.set_addon_leg_sl_order_id(leg_id, leg_sl_order_id, broker_stop_price=45.0)

    # Pre-merge: leg still has its own real resting stop -- the bound must
    # stay TIGHT (core-only, 100), so a 120-share SELL (core+leg) must be
    # refused as a would-be short.
    with pytest.raises(schwab_safety.SafetyViolation):
        schwab_client.place_equity_sell('soxl_ira', TICKER, 120, 54.0, node_id=node['id'])

    # Post-merge: simulate exactly what _attempt_automated_sell does right
    # before its own merged placement call -- clear the leg's sl_order_id.
    signals_db.set_addon_leg_sl_order_id(leg_id, None, broker_stop_price=None)

    # Must NOT raise now -- 120 is within the widened core+leg bound.
    _, order_id = schwab_client.place_equity_sell('soxl_ira', TICKER, 120, 54.0, node_id=node['id'])
    assert order_id is not None


def test_reconciliation_detects_merged_quantity_mismatch(env, fake_broker, monkeypatch):
    """MEDIUM finding #3 (paired Opus review, 2026-08-17/18): every existing
    check_live_state_reconciliation protective-order check is presence-only
    (has_sell_order = bool(resting_sells)) -- never comparing the resting
    order's actual quantity against what a merged core+leg exit should
    cover. That's exactly the detection gap that let the CRITICAL
    force-replace bug (finding #1) go unnoticed. This proves the new
    merged_trailing_sell_quantity_mismatch check actually catches a real
    quantity drop -- simulated here by force-replacing the merged order with
    a smaller core-only one directly at the fake broker (mirrors the exact
    pre-fix bug shape), bypassing the now-fixed code path so the
    reconciliation check is exercised in isolation."""
    node = _node()
    pos = _setup_merged_position(node, fake_broker)
    merged_order_id = pos['sl_order_id']

    # This module's fixture mocks schwab_safety._open_orders -> [] (keeps the
    # merge-mechanics tests above isolated from the dup-order guard). This
    # test needs the REAL resting-order list back, since check_live_state_
    # reconciliation reads it directly to build resting_sells.
    monkeypatch.setattr(schwab_safety, '_open_orders',
                         lambda account: [o for o in fake_broker.orders.values()
                                          if o.get('account') == account
                                          and o.get('status') not in schwab_safety._OPEN_ORDER_STATUSES_EXCLUDED])

    # Simulate the pre-fix bug directly: replace the merged (120-share) order
    # with a smaller core-only (100-share) one, exactly what the CRITICAL bug
    # used to do. fake_broker.seed_resting_order mints a fresh resting order;
    # the old one's status is set directly to mimic an atomic replace's end
    # state (mirrors what fake_broker.replace_order does internally).
    fake_broker.orders[merged_order_id]['status'] = 'REPLACED'
    shrunk_order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'TRAILING_STOP', 'SELL', 100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET sl_order_id=? WHERE ticker=?", (shrunk_order_id, TICKER))
        c.commit()
    pos = signals_db.get_open_position(TICKER)

    # _setup_merged_position drives the real arm via notify_trailing_activated
    # directly (bypassing check_sell_condition, which is what normally
    # persists trail_state['trailing']=True) -- set it explicitly so this
    # test reaches the (trailing and order_placed and has_sell_order) branch
    # the new check lives in, matching real post-arm state.
    state = dict(pos['trail_state'] or {})
    state['trailing'] = True
    state['order_placed'] = True
    signals_db.update_position_trail_state(pos['id'], state)
    pos = signals_db.get_open_position(TICKER)

    signals_notify.check_live_state_reconciliation([pos])

    events = signals_db.get_coverage_events(scenario_key='reconciliation_mismatch')
    matches = [e for e in events if e['ticker'] == TICKER and e['result'] == 'merged_trailing_sell_quantity_mismatch']
    assert matches, (
        "reconciliation must detect a merged order whose real resting quantity (100) no longer "
        "covers the expected core+leg total (120) -- the exact detection gap finding #3 closes"
    )

