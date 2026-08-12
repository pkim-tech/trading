"""fake_broker scenarios for the real margin add-on-at-arm entry
(signals_notify.check_addon_trigger_real, called from the tail of
notify_trailing_activated), Part 5 of docs/plans/
real_order_execution_drought_addon.md.

The single most important assertion here: a real MARKET BUY for exactly
pos['shares'] is placed DESPITE the core position's own resting protective
SELL already at the broker -- without schwab_safety.check_order's
is_addon_leg exemption (verified against five DB preconditions, not trusted
from the caller), the ordinary _has_open_order guard would block this 100%
of the time by construction (the parent's own resting SELL is ALWAYS present
at the exact moment arm fires). If this test doesn't see the add-on order
land in fake_broker.orders, the mechanism silently never fires."""
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

TICKER = 'TEST_ADDON_ENTRY_SCENARIO'
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
                         account='soxl_ira', starting_notional=5000)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET addon_enabled=1 WHERE ticker=?", (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _real_orders(fake_broker_, ticker, side=None, order_type=None):
    out = []
    for o in fake_broker_.orders.values():
        leg = o['orderLegCollection'][0]
        if leg['instrument']['symbol'] != ticker:
            continue
        if side is not None and leg['instruction'] != side:
            continue
        if order_type is not None and o['orderType'] != order_type:
            continue
        out.append(o)
    return out


def _open_core_position(node, account='soxl_ira', shares=20, entry_price=50.0):
    now = datetime.now()
    signals_db.open_position(node, signal_price=entry_price, signal_time=now, entry_price=entry_price,
                              entry_time=now, shares=shares)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account=? WHERE ticker=?", (account, node['ticker']))
        c.commit()
    pos = signals_db.get_open_position(node['ticker'])
    # In real production, signals_compute.check_sell_condition persists
    # trail_state.trailing=True BEFORE notify_trailing_activated is ever
    # called (see that function's own docstring) -- calling
    # notify_trailing_activated directly in a test bypasses that, so it must
    # be seeded here to faithfully reproduce the real precondition
    # check_order's is_addon_leg exemption checks (#3: parent genuinely armed).
    signals_db.update_position_trail_state(pos['id'], {'trailing': True, 'peak': entry_price})
    return signals_db.get_open_position(node['ticker'])


def test_addon_leg_places_real_market_buy_despite_resting_protective_sell(env, fake_broker):
    """The regression assertion this whole mechanism lives or dies on."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    fake_broker.set_buying_power('soxl_ira', 1_000_000.0)
    pos = _open_core_position(node)

    # arm: places the parent's own resting protective TRAILING_STOP SELL --
    # this is the order that would ordinarily self-block a same-ticker BUY.
    signals_notify.notify_trailing_activated(pos, current_price=52.0)

    protective_sells = _real_orders(fake_broker, TICKER, side='SELL')
    assert len(protective_sells) == 2, (
        "expected the parent's own resting protective TRAILING_STOP SELL plus the leg's own "
        "D3 protective STOP SELL -- both are real, separate, wanted orders"
    )

    addon_buys = _real_orders(fake_broker, TICKER, side='BUY')
    assert len(addon_buys) == 1, (
        "the add-on leg's real MARKET BUY must land at the broker despite the parent's "
        "resting protective SELL -- if this is empty, is_addon_leg's exemption never fired"
    )
    assert addon_buys[0]['orderLegCollection'][0]['quantity'] == pos['shares']
    assert addon_buys[0]['orderType'] == 'MARKET'

    leg = signals_db.get_open_addon_leg_by_parent(pos['id'])
    assert leg is not None
    assert leg['entry_status'] == 'filled'
    assert leg['shares'] == pos['shares']

    events = signals_db.get_coverage_events(scenario_key='addon_double_buy_exemption')
    assert any(e['ticker'] == TICKER and e['result'] == 'preconditions_passed' for e in events)
    placement_events = signals_db.get_coverage_events(scenario_key='addon_entry_placement')
    assert any(e['ticker'] == TICKER and e['result'] == 'placed' for e in placement_events)
    fill_events = signals_db.get_coverage_events(scenario_key='addon_entry_fill')
    assert any(e['ticker'] == TICKER and e['result'] == 'filled' for e in fill_events)


def test_second_resting_buy_for_same_ticker_still_blocks_addon(env, fake_broker):
    """Same-side dup-BUY protection is preserved -- is_addon_leg only exempts
    the any-side _has_open_order check, not a genuine second resting BUY."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    fake_broker.set_buying_power('soxl_ira', 1_000_000.0)
    pos = _open_core_position(node)
    fake_broker.seed_resting_order('soxl_ira', TICKER, 'MARKET', 'BUY', 50)

    signals_notify.notify_trailing_activated(pos, current_price=52.0)

    addon_buys = _real_orders(fake_broker, TICKER, side='BUY', order_type='MARKET')
    # Only the seeded order -- the add-on's own attempt must have been blocked.
    assert len(addon_buys) == 1
    assert signals_db.get_open_addon_leg_by_parent(pos['id']) is None


def test_addon_leg_hard_refused_on_non_margin_account(env, fake_broker):
    # Uses 'sep' -- 'roth' was margin_capable=False before 2026-08-11 (this
    # test's original target) but became real limited-margin this session
    # (margin_capable=True now, matching brokerage/ira/soxl_ira); 'sep' is
    # the one account still non-margin-capable. See accounts table /
    # schwab_safety.ACCOUNTS.
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='sep' WHERE ticker=?", (TICKER,))
        c.commit()
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('sep', 1_000_000.0)
    fake_broker.set_buying_power('sep', 1_000_000.0)
    pos = _open_core_position(node, account='sep')

    signals_notify.notify_trailing_activated(pos, current_price=52.0)

    addon_buys = _real_orders(fake_broker, TICKER, side='BUY')
    assert len(addon_buys) == 0, "add-on must never place a real order against a non-margin account"
    assert signals_db.get_open_addon_leg_by_parent(pos['id']) is None


def test_addon_leg_blocked_by_insufficient_buying_power(env, fake_broker):
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    # notional ~= 20 * 52 = 1040; headroom mult 2.0 requires >= 2080. The real
    # order-time check is leverage-aware (2026-08-12): equity/margin_req, not
    # the raw buyingPower field -- set_equity(500) at the default 50%
    # margin req (2x) gives buying_power=1000, same ceiling as before.
    fake_broker.set_equity('soxl_ira', 500.0)
    pos = _open_core_position(node)

    signals_notify.notify_trailing_activated(pos, current_price=52.0)

    addon_buys = _real_orders(fake_broker, TICKER, side='BUY')
    assert len(addon_buys) == 0
    assert signals_db.get_open_addon_leg_by_parent(pos['id']) is None
    events = signals_db.get_coverage_events(scenario_key='addon_buying_power_check')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked_insufficient' for e in events)


def test_addon_leg_never_fires_a_second_time_for_same_parent(env, fake_broker):
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    fake_broker.set_buying_power('soxl_ira', 1_000_000.0)
    pos = _open_core_position(node)

    signals_notify.notify_trailing_activated(pos, current_price=52.0)
    first_addon_buys = _real_orders(fake_broker, TICKER, side='BUY')
    assert len(first_addon_buys) == 1

    fresh_pos = signals_db.get_open_position(TICKER)
    signals_notify.check_addon_trigger_real(fresh_pos, current_price=53.0)

    second_addon_buys = _real_orders(fake_broker, TICKER, side='BUY')
    assert len(second_addon_buys) == 1, "a second add-on attempt for the same parent must be a no-op"


def test_parents_own_exit_still_works_once_the_leg_has_its_own_resting_stop(env, fake_broker):
    """CRITICAL (found by cold Opus review before this shipped): once the
    leg's own D3 protective stop is resting, the PARENT core position's own
    automated exit (_attempt_automated_exit_sell, e.g. a real SL/TIME/TP
    trigger) must not be blocked by seeing the leg's stop as a duplicate
    resting SELL for the same ticker -- it's a second, real, wanted order,
    not the accidental-duplicate case schwab_safety's resting-SELL guard
    exists to catch."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    fake_broker.set_buying_power('soxl_ira', 1_000_000.0)
    pos = _open_core_position(node)
    # Real production always places an initial SL at entry
    # (_place_stop_loss_for_position) -- seed one here so arm's REPLACE path
    # (not a fresh placement) is exercised, matching
    # test_fake_broker_arm_scenario.py's own realistic setup.
    initial_sl_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'STOP', 'SELL', 20, stop_price=49.50)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET sl_order_id=? WHERE ticker=?", (initial_sl_id, TICKER))
        c.commit()
    pos = signals_db.get_open_position(TICKER)

    signals_notify.notify_trailing_activated(pos, current_price=52.0)
    leg = signals_db.get_open_addon_leg_by_parent(
        signals_db.get_open_position(TICKER)['id'])
    assert leg is not None and leg.get('sl_order_id') is not None, \
        "the leg's own D3 protective stop must be resting before this test is meaningful"

    fresh_pos = signals_db.get_open_position(TICKER)
    assert fresh_pos.get('sl_order_id') is not None, \
        "arm's REPLACE writeback must repoint sl_order_id at the new resting order"
    order_id = signals_notify._attempt_automated_exit_sell(fresh_pos, 'SL', current_price=48.0)

    assert order_id is not None, (
        "the parent's own real SL exit must succeed despite the leg's resting stop -- "
        "if this is None, the resting-SELL dup guard is blocking the parent on its own leg's order"
    )






def test_addon_never_attempted_against_an_open_drought_overlay_position(env, fake_broker):
    """check_addon_trigger_real's own position_source=='core' guard (line
    2025-2026) -- an armed DROUGHT position must never be mistaken for the
    core position add-on is designed against. This is the real collision
    surface between the two mechanisms built this session: without this
    guard, a drought position reaching its own arm point would incorrectly
    trigger a real margin add-on leg."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    fake_broker.set_buying_power('soxl_ira', 1_000_000.0)
    now = datetime.now()
    signals_db.open_drought_overlay_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                                                entry_time=now, confirm_days=3, shares=20)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account=? WHERE ticker=?", ('soxl_ira', TICKER))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    assert pos['position_source'] == 'drought_overlay'
    signals_db.update_position_trail_state(pos['id'], {'trailing': True, 'peak': 50.0})
    pos = signals_db.get_open_position(TICKER)

    signals_notify.check_addon_trigger_real(pos, current_price=52.0)

    assert len(_real_orders(fake_broker, TICKER, side='BUY')) == 0
    assert signals_db.get_open_addon_leg_by_parent(pos['id']) is None


def test_addon_leg_refused_when_parent_not_actually_armed(env, fake_broker):
    """is_addon_leg's precondition #2 (schwab_safety.check_order:922-928) --
    calling check_addon_trigger_real directly (bypassing notify_trailing_
    activated, which normally arms trail_state first) exercises this guard
    on its own terms: even though check_addon_trigger_real's own guards all
    pass, the deeper is_addon_leg exemption inside check_order must still
    refuse a real order for a parent that was never actually armed."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    fake_broker.set_buying_power('soxl_ira', 1_000_000.0)
    # Open the core position directly (not via _open_core_position, which
    # deliberately seeds trail_state.trailing=True to match the real
    # precondition) -- here we need the position UNARMED, the case under test.
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=20)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account=? WHERE ticker=?", ('soxl_ira', TICKER))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    assert not (pos.get('trail_state') or {}).get('trailing')

    signals_notify.check_addon_trigger_real(pos, current_price=52.0)

    assert len(_real_orders(fake_broker, TICKER, side='BUY')) == 0
    assert signals_db.get_open_addon_leg_by_parent(pos['id']) is None
    events = signals_db.get_coverage_events(scenario_key='addon_precondition_blocked')
    assert any(e['ticker'] == TICKER and e['detail'] == 'parent_not_armed' for e in events)


def test_addon_leg_blocked_by_combined_exposure_ceiling(env, fake_broker):
    """D5 (schwab_safety.check_order:941-956) -- core+addon combined notional
    exceeding the account's own notional_cap must refuse the real order,
    even with ample buying power. Deliberately conservative: reuses the
    account's existing cap rather than a bespoke add-on multiplier (see D5
    in docs/plans/real_order_execution_drought_addon.md)."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    fake_broker.set_buying_power('soxl_ira', 1_000_000.0)
    # core notional 40*50=2000, addon notional 40*52=2080, combined 4080 --
    # soxl_ira's real notional_cap is 3000, so this must be refused.
    pos = _open_core_position(node, shares=40, entry_price=50.0)

    signals_notify.notify_trailing_activated(pos, current_price=52.0)

    assert len(_real_orders(fake_broker, TICKER, side='BUY')) == 0
    assert signals_db.get_open_addon_leg_by_parent(pos['id']) is None
    events = signals_db.get_coverage_events(scenario_key='addon_combined_exposure_blocked')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events)


def test_addon_leg_still_blocked_by_global_kill_switch(env, fake_broker, monkeypatch):
    """The kill switch is a rogue-algo off-switch that must block everything,
    including add-on's own is_addon_leg exemption -- that exemption widens
    ONE specific guard (the resting-order dup check), never the kill switch
    itself. Calling check_addon_trigger_real directly (parent already armed
    without going through notify_trailing_activated) isolates this from the
    parent's own arm-time SELL placement, which the kill switch would also
    correctly block."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    fake_broker.set_buying_power('soxl_ira', 1_000_000.0)
    pos = _open_core_position(node)
    signals_db.update_position_trail_state(pos['id'], {'trailing': True, 'peak': 50.0})
    pos = signals_db.get_open_position(TICKER)
    schwab_safety.engage_kill_switch("test halt")

    signals_notify.check_addon_trigger_real(pos, current_price=52.0)

    assert len(_real_orders(fake_broker, TICKER, side='BUY')) == 0
    assert signals_db.get_open_addon_leg_by_parent(pos['id']) is None
    events = signals_db.get_coverage_events(scenario_key='kill_switch_block')
    assert any(e['ticker'] == TICKER for e in events)


def test_addon_size_mismatch_refused_by_check_order_directly(env, fake_broker):
    """is_addon_leg's precondition #4 (schwab_safety.check_order:934-940) --
    quantity must exactly equal the parent's own share count. Not reachable
    through check_addon_trigger_real's real call path (it always passes
    int(pos['shares']) verbatim, so this can only diverge from a caller bug),
    but the guard itself is real defense-in-depth and must be proven to
    exist -- calling schwab_safety.check_order directly with a deliberately
    mismatched quantity, the same way is_addon_leg's other 4 preconditions
    are proven above via the real check_addon_trigger_real integration path."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    fake_broker.set_buying_power('soxl_ira', 1_000_000.0)
    pos = _open_core_position(node, shares=20)

    with pytest.raises(schwab_safety.SafetyViolation, match="quantity"):
        schwab_safety.check_order('soxl_ira', TICKER, 21, 52.0, 'BUY', is_addon_leg=True)

    events = signals_db.get_coverage_events(scenario_key='addon_size_mismatch_blocked')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events)
