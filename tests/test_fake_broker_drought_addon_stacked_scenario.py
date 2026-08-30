"""fake_broker scenario for the STACKED drought+addon combination -- a single
node with BOTH drought_overlay_enabled=1 AND addon_enabled=1, real order
placement/dispatch for both mechanisms exercised together against one
node/ticker rather than in isolation.

Closes a real Trade-Flow Accountability Grid gap flagged 2026-08-20 (see
docs/backlog_cache.md / docs/deep_backlog.md's "AGQ paper-node cleanup"
entry): individual addon/drought mechanisms already had real fake_broker
coverage (test_fake_broker_addon_entry_scenario.py,
test_fake_broker_drought_entry_scenario.py), but every one of those fixtures
enables only ONE overlay flag on its test node -- the actual paper node this
was probing (v5-overlay-test-da, wl_id=186/187) had BOTH flags on the SAME
node, which nothing here proved.

evaluate_drought_entry() itself gates a fresh drought entry on
`db.get_open_position_by_wl_id(wl_id) or pending` being falsy (paper_trading.py
~253) -- a core position and a drought-overlay position can never genuinely
coexist open at the same time for one node, so "stacked" doesn't mean
simultaneous positions. It means: does a node with both flags survive a real
core-position addon-leg lifecycle (open -> arm -> addon fires -> lockstep
close) followed by a real drought-overlay lifecycle (gap elapses -> drought
entry fires -> fills -> opens a position_source='drought_overlay' position)
on the SAME node/ticker, without any state left over from the first phase
(a stale addon_legs row, a stale get_open_position(ticker) resolution)
leaking into or blocking the second -- and does the addon guard
(position_source=='core' only) still correctly refuse to fire again once the
node's addon-enabled drought position itself arms."""
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
import paper_trading
import schwab_safety

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_DROUGHT_ADDON_STACKED_SCENARIO'
IN_WINDOW_TIME = datetime(2026, 7, 29, 10, 30)

_DROUGHT_DECISION = {'price': 50.0, 'shares': 100, 'confirm_days': 3, 'vol_gate': None,
                     'vol_pctile': None, 'gap_start': '2026-07-20 09:30:00'}


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
    monkeypatch.setattr(signals_notify, 'time', type('T', (), {'sleep': staticmethod(lambda *a: None)}))
    monkeypatch.setattr(signals_notify.cfg, 'INTERACTIVE', False)

    signals_db.ensure_tables()
    # starting_notional=2000, not 5000 -- soxl_ira's real notional_cap is
    # $3,000 and the drought entry's own sizing (buy_order_sizing, NOT
    # decision['shares']) draws on this same field, so 5000 would blow the
    # cap on the drought leg even though it's fine for the addon leg alone.
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account='soxl_ira', starting_notional=2000)
    with signals_db._conn() as c:
        # The real combination under test -- both overlay flags on ONE node,
        # matching v5-overlay-test-da's actual config.
        c.execute("UPDATE watch_list SET addon_enabled=1, drought_overlay_enabled=1, "
                   "drought_confirm_days=3 WHERE ticker=?", (TICKER,))
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


def _open_core_position(node, fake_broker_, shares=20, entry_price=50.0, seed_initial_sl=False):
    now = datetime.now()
    signals_db.open_position(node, signal_price=entry_price, signal_time=now, entry_price=entry_price,
                              entry_time=now, shares=shares)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='soxl_ira' WHERE ticker=?", (node['ticker'],))
        c.commit()
    pos = signals_db.get_open_position(node['ticker'])
    if seed_initial_sl:
        # Real production always places an initial SL at entry
        # (_place_stop_loss_for_position) -- without it, arm's own trailing-
        # sell placement is a fresh PLACE rather than a REPLACE, and
        # pos['sl_order_id'] (which _attempt_automated_exit_sell's later SL
        # branch keys off) is never repointed at the new resting order, so a
        # subsequent real exit can't find/replace it and it's left resting
        # forever -- see test_fake_broker_addon_entry_scenario.py::
        # test_parents_own_exit_still_works_once_the_leg_has_its_own_resting_stop.
        initial_sl_id = fake_broker_.seed_resting_order(
            'soxl_ira', node['ticker'], 'STOP', 'SELL', shares, stop_price=entry_price * 0.95)
        with signals_db._conn() as c:
            c.execute("UPDATE open_positions SET sl_order_id=? WHERE ticker=?",
                       (initial_sl_id, node['ticker']))
            c.commit()
        pos = signals_db.get_open_position(node['ticker'])
    # Same reasoning as test_fake_broker_addon_entry_scenario.py's helper --
    # notify_trailing_activated is called directly here, bypassing the real
    # persist-before-call ordering, so trail_state must be seeded to match
    # check_order's is_addon_leg precondition #3 (parent genuinely armed).
    signals_db.update_position_trail_state(pos['id'], {'trailing': True, 'peak': entry_price})
    return signals_db.get_open_position(node['ticker'])


def test_stacked_node_runs_core_addon_lifecycle_then_drought_lifecycle_cleanly(env, fake_broker, monkeypatch):
    """The single most important assertion in this file: a node with BOTH
    addon_enabled=1 and drought_overlay_enabled=1 must run a real core-addon
    lifecycle to completion, then a real drought-overlay entry, on the SAME
    ticker/node, with neither mechanism's real order/DB state contaminating
    the other."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    fake_broker.set_buying_power('soxl_ira', 1_000_000.0)

    # --- Phase A: core position arms, addon leg fires (real market buy). ---
    pos = _open_core_position(node, fake_broker, seed_initial_sl=True)
    signals_notify.notify_trailing_activated(pos, current_price=52.0)

    addon_buys = _real_orders(fake_broker, TICKER, side='BUY')
    assert len(addon_buys) == 1, "addon leg's real MARKET BUY must land despite the parent's resting SELL"
    leg = signals_db.get_open_addon_leg_by_parent(pos['id'])
    assert leg is not None and leg['entry_status'] == 'filled'

    fill_events = signals_db.get_coverage_events(scenario_key='addon_entry_fill')
    assert any(e['ticker'] == TICKER and e['result'] == 'filled' for e in fill_events)
    placement_events = signals_db.get_coverage_events(scenario_key='addon_entry_placement')
    assert any(e['ticker'] == TICKER and e['result'] == 'placed' for e in placement_events)

    # Close out the core position for real. _attempt_automated_exit_sell's SL
    # branch is correctly a no-op here (schwab_safety._exit_order_resting
    # confirms the parent's own arm-time TRAILING_STOP is genuinely still
    # resting at the broker -- "a market SELL is the wrong tool for a stop
    # that's already doing its job", per that function's own docstring), so
    # the realistic way this actually closes in production is the broker
    # filling that resting order on its own -- force_fill it and let
    # check_sl_order_fills detect the fill and close both the parent AND (via
    # its own close_addon_leg_real_if_open call) the addon leg, exactly as
    # active_signals.py's real poll loop does.
    fresh_pos = signals_db.get_open_position(TICKER)
    assert fresh_pos['sl_order_id'] is not None, (
        "arm's REPLACE writeback must repoint sl_order_id at the new resting trailing-sell order"
    )
    fake_broker.force_fill(fresh_pos['sl_order_id'], price=48.0)
    signals_notify.check_sl_order_fills([fresh_pos])

    assert signals_db.get_open_position(TICKER) is None
    assert signals_db.get_open_addon_legs(paper=False) == [], (
        "the addon leg from phase A must be fully closed before phase B begins"
    )

    # --- Phase B: gap elapses, real drought-overlay entry fires on the SAME node. ---
    monkeypatch.setattr(paper_trading, 'evaluate_drought_entry', lambda n, paper=False: dict(_DROUGHT_DECISION))
    fake_broker.set_quote(TICKER, last=50.0, bid=49.99, ask=50.01)

    signals_notify.check_drought_entry(node)

    # Filter to only the TRAILING_STOP order type -- phase A's now-closed
    # addon MARKET buy order object is still present in fake_broker.orders
    # (orders are never deleted, only transitioned to a terminal status), so
    # a bare side='BUY' filter would pick up both.
    drought_buys = _real_orders(fake_broker, TICKER, side='BUY', order_type='TRAILING_STOP')
    assert len(drought_buys) == 1, "a real trailing-buy drought entry must be placed"
    assert drought_buys[0]['orderType'] == 'TRAILING_STOP'
    pending = signals_db.get_drought_pending_buy(node['id'])
    assert pending is not None and pending['position_source'] == 'drought_overlay'

    decision_events = signals_db.get_coverage_events(scenario_key='drought_entry')
    assert any(e['ticker'] == TICKER and e['mode'] != 'paper' and e['result'] == 'signalled'
               for e in decision_events)
    placement_events = signals_db.get_coverage_events(scenario_key='drought_entry_placement')
    assert any(e['ticker'] == TICKER and e['result'] == 'signalled' for e in placement_events)

    order_id = drought_buys[0]['orderId']
    fake_broker.force_fill(order_id, price=50.5)
    signals_notify._reconcile_buy_fill(TICKER, 50.5, 100, wl_id=node['id'])

    drought_pos = signals_db.get_open_position(TICKER)
    assert drought_pos is not None
    assert drought_pos['position_source'] == 'drought_overlay', (
        "the real fill must open a drought_overlay position, not silently reuse a stale "
        "core-position code path left over from phase A"
    )
    assert signals_db.get_drought_pending_buy(node['id']) is None
    # No stray leg from phase A's addon (already closed) mistakenly attached
    # to this fresh drought position.
    assert signals_db.get_open_addon_leg_by_parent(drought_pos['id']) is None

    # --- Phase C: the drought position itself arms -- addon must still be
    # correctly refused (position_source guard), even though this exact node
    # already has real addon-fire history on file from phase A. ---
    signals_db.update_position_trail_state(drought_pos['id'], {'trailing': True, 'peak': 50.5})
    drought_pos = signals_db.get_open_position(TICKER)

    orders_before_phase_c = set(fake_broker.orders.keys())

    signals_notify.check_addon_trigger_real(drought_pos, current_price=52.0)

    new_orders = set(fake_broker.orders.keys()) - orders_before_phase_c
    assert new_orders == set(), (
        "addon must never fire a second real BUY against an armed drought_overlay position, "
        "even on a node that already has real core-addon history"
    )
    assert signals_db.get_open_addon_leg_by_parent(drought_pos['id']) is None


def test_addon_still_fires_for_a_stacked_node_core_position_after_a_prior_drought_cycle(env, fake_broker, monkeypatch):
    """Mirror-order regression: drought entry/close first, THEN a fresh core
    position arms and addon fires -- proves the ordering isn't a fluke of
    phase A always running first above."""
    node = _node()
    fake_broker.set_quote(TICKER, last=50.0, bid=49.99, ask=50.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    fake_broker.set_buying_power('soxl_ira', 1_000_000.0)
    monkeypatch.setattr(paper_trading, 'evaluate_drought_entry', lambda n, paper=False: dict(_DROUGHT_DECISION))

    signals_notify.check_drought_entry(node)
    drought_buys = _real_orders(fake_broker, TICKER, side='BUY')
    order_id = drought_buys[0]['orderId']
    fake_broker.force_fill(order_id, price=50.5)
    signals_notify._reconcile_buy_fill(TICKER, 50.5, 100, wl_id=node['id'])
    drought_pos = signals_db.get_open_position(TICKER)
    assert drought_pos['position_source'] == 'drought_overlay'

    signals_db.close_position(drought_pos['id'], exit_signal_price=51.0, exit_price=51.0,
                               exit_time=datetime.now(), exit_reason='SL')
    assert signals_db.get_open_position(TICKER) is None

    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    pos = _open_core_position(node, fake_broker, shares=20, entry_price=50.0)
    signals_notify.notify_trailing_activated(pos, current_price=52.0)

    addon_buys = _real_orders(fake_broker, TICKER, side='BUY',
                               order_type='MARKET')
    assert len(addon_buys) == 1, (
        "addon must still fire for a genuine core position even after this node's earlier "
        "real drought-overlay cycle"
    )
    leg = signals_db.get_open_addon_leg_by_parent(pos['id'])
    assert leg is not None and leg['entry_status'] == 'filled'

