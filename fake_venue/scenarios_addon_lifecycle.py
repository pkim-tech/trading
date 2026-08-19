"""Phase 2 scenario: the margin add-on-at-arm leg's real lifecycle, end to
end through TWO real broker order sequences on ONE fake node -- covering 3 of
the 4 addon Grid rows the fake_broker pytest suite already proves at the
per-function level (tests/test_fake_broker_addon_entry_scenario.py,
tests/test_fake_broker_addon_lockstep_exit_scenario.py), now driven through
the harness's own real-subprocess isolation (own DB file, own schwab_safety
state dir, isolation tripwire) instead of pytest's monkeypatched fixtures.

Rows covered: `addon_double_buy_exemption`, `addon_entry_placement`,
`addon_exit_placement` (`addon_entry_fill`/`addon_exit_fill` also fire as a
side effect and are checked, but aren't this scenario's primary target --
see fake_venue's own addon_leg_reconciliation sibling scenario for the 4th
targeted row).

Why one node, two sequential core-position cycles, rather than two nodes:
matches the real one-leg-per-parent invariant (get_open_addon_leg_by_parent)
-- a node can only ever have ONE addon leg open at a time, so the two
addon-leg shapes this scenario needs to prove (a leg that fills immediately
and closes via a REPLACE-to-market, vs. a leg still resting unfilled when its
parent exits and must be CANCELLED, never sold) have to happen one after the
other against the same ticker/account, exactly the way two real add-on
episodes for the same live node would happen months apart in production.

  Cycle A  core position #1 opens and arms -> the REAL production tail call
           (signals_notify.notify_trailing_activated, exactly as
           check_sell_condition's arm branch calls it in production) fires
           check_addon_trigger_real -> schwab_safety.check_order's
           is_addon_leg exemption verifies all five preconditions against
           the DB (not trusted from the caller) and logs
           addon_double_buy_exemption='preconditions_passed' -> a genuine
           MARKET BUY for exactly the parent's share count lands at the
           broker DESPITE the parent's own just-placed resting protective
           SELL (addon_entry_placement='placed') -> the leg's own synchronous
           fast-confirm poll (schwab_client.get_filled_order) confirms the
           fill (addon_entry_fill='filled') -> D3 places the leg's own real
           resting protective STOP.

           Core position #1 then exits (seeded directly, matching every
           sibling Phase 2 scenario's accepted entry/exit-computation
           caveat -- this scenario's target is the addon mechanism, not
           bar-close signal computation) via the REAL production ordering
           (docs/plans/real_order_execution_drought_addon.md Part 7.2: the
           parent's open_positions row is deleted BEFORE
           close_addon_leg_real_if_open ever runs, at all 7 real call
           sites) -> the leg's REST-ing D3 stop gets REPLACED with a real
           market SELL, confirmed filled -> addon_exit_fill='closed'.

  Cycle B  a SECOND core position opens on the SAME node (only reachable
           because cycle A's leg is fully closed -- get_open_addon_leg_by_
           parent's one-leg-per-parent invariant would otherwise block a
           second leg). This time the addon leg's ENTRY side is seeded
           directly (bypassing check_addon_trigger_real, since cycle A
           already proved the entry gate/placement chain) with its real
           broker order deliberately left resting/unfilled (FakeBroker fills
           a MARKET order immediately on placement -- forced back to
           'WORKING' after seeding, the identical technique
           test_leg_still_placed_when_parent_exits_is_cancelled_never_sold
           uses, to reproduce the real window between order placement and
           broker confirmation). Core position #2 then exits the same real
           way as cycle A -> close_addon_leg_real_if_open finds the leg
           still entry_status='placed' -> cancels the real resting order
           (never sells shares never bought) -> addon_exit_placement=
           'cancelled_unfilled_leg'.
"""
from dataclasses import dataclass
from datetime import datetime

from fake_venue import venue
from fake_venue.scenarios_meta import MARGIN_ACCOUNT_NUMBER, MARGIN_ALIAS, PRICE_SOURCE_TICKER, TICKER

FAKE_ACCOUNTS = [
    dict(alias=MARGIN_ALIAS, notional_cap=100_000, daily_order_cap=100,
         cash_settlement_type='margin', margin_capable=1),
]
CORE_A_SHARES = 20
CORE_B_SHARES = 15


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ''
    required: bool = True

    def __post_init__(self):
        self.ok = bool(self.ok)


def _add_node():
    import signals_db as db

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version='fake_venue_addon',
                window=10, take_profit=16, stop_loss=1, max_hold_hours=105,
                state='live', account=MARGIN_ALIAS, starting_notional=5_000,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                label='fake-venue harness node (addon_lifecycle)')
    with db._conn() as c:
        c.execute("UPDATE watch_list SET addon_enabled=1 WHERE ticker=?", (TICKER,))
        c.commit()
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]


def _real_orders(broker, ticker, side=None, order_type=None):
    out = []
    for oid, o in broker.orders.items():
        leg = o['orderLegCollection'][0]
        if leg['instrument']['symbol'] != ticker:
            continue
        if side is not None and leg['instruction'] != side:
            continue
        if order_type is not None and o['orderType'] != order_type:
            continue
        out.append((oid, o))
    return out


def run(price=None, verbose=True):
    """Runs the scenario against the already-isolated, already-imported
    environment. Returns (checks, observations)."""
    import schwab_safety
    import signals_db as db
    import signals_notify as notify

    def say(msg):
        if verbose:
            print(msg)

    checks, observations = [], {}

    db.ensure_tables()
    if [n for n in db.get_watchlist() if n['ticker'] == TICKER]:
        raise RuntimeError("this harness DB has already been used -- point --db-path at a fresh "
                           "file (the default temp dir is fresh every run)")
    venue.seed_fake_accounts(FAKE_ACCOUNTS)
    broker = venue.install_fake_broker([MARGIN_ALIAS])
    venue.seed_account_number_env({MARGIN_ALIAS: MARGIN_ACCOUNT_NUMBER})
    price = venue.seed_quote(broker, TICKER, price, price_source_ticker=PRICE_SOURCE_TICKER)
    broker.set_cash_balance(MARGIN_ALIAS, 1_000_000.0)
    broker.set_buying_power(MARGIN_ALIAS, 1_000_000.0)
    say(f"[setup] {TICKER} quote seeded at ${price:.4f}")

    node = _add_node()
    say(f"[setup] node wl_id={node['id']} ({MARGIN_ALIAS}), addon_enabled=1")

    # Same orthogonal-to-the-mechanism-under-test override as post_fill_topup
    # (check_order's trading-day gate is BUY-only/unconditional, correct in
    # production but must not block a deterministic weekend/holiday run).
    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking schwab_safety._is_trading_day "
            "True for this run (see module docstring: orthogonal to the mechanism under test)")
        schwab_safety._is_trading_day = lambda date_str: True

    # 2026-08-19: signals_notify.close_addon_leg_real_if_open now gates on
    # _market_session_open_now() (incident #13's market-hours-close guard,
    # mirrored from _attempt_automated_exit_sell into the addon-leg exit
    # path) -- orthogonal to what this scenario tests, but a real wall-clock
    # run outside 9:30-16:00 ET would silently short-circuit the leg exit
    # this scenario exercises. Pinned to a fixed in-session moment, same
    # orthogonal-override pattern as _is_trading_day above.
    schwab_safety._now = lambda: datetime(2026, 8, 19, 15, 30, 0)

    # ============================================================= cycle A
    now = datetime.now()
    entry_price_a = round(price * 0.98, 4)
    opened_a = db.open_position(node, signal_price=entry_price_a, signal_time=now, entry_price=entry_price_a,
                                entry_time=now, shares=CORE_A_SHARES)
    checks.append(Check("cycle A: core position opened", opened_a))
    pos_a = db.get_open_position(TICKER)
    # check_sell_condition persists trail_state.trailing=True BEFORE calling
    # notify_trailing_activated in production (see that function's own
    # docstring) -- seeded here to faithfully reproduce the real precondition
    # is_addon_leg's preconditions check (#3: parent genuinely armed), same
    # accepted caveat as tests/test_fake_broker_addon_entry_scenario.py's own
    # _open_core_position helper.
    db.update_position_trail_state(pos_a['id'], {'trailing': True, 'peak': entry_price_a})
    pos_a = db.get_open_position(TICKER)

    say(f"[cycle A] calling the real signals_notify.notify_trailing_activated(pos, {price:.4f}) -- "
        f"exercises check_addon_trigger_real via its real production call site")
    orders_before = set(broker.orders)
    notify.notify_trailing_activated(pos_a, current_price=price)

    exemption_events = db.get_coverage_events(scenario_key='addon_double_buy_exemption')
    checks.append(Check("addon_double_buy_exemption fired 'preconditions_passed' -- all five DB-verified "
                        "preconditions passed, not trusted from the caller",
                        any(e['ticker'] == TICKER and e['result'] == 'preconditions_passed'
                            for e in exemption_events),
                        f"events={[(e['result'], e['detail']) for e in exemption_events]}"))

    placement_events = db.get_coverage_events(scenario_key='addon_entry_placement')
    checks.append(Check("addon_entry_placement fired 'placed' -- the real MARKET BUY was placed despite "
                        "the parent's own resting protective SELL",
                        any(e['ticker'] == TICKER and e['result'] == 'placed' for e in placement_events),
                        f"events={[(e['result'], e['detail']) for e in placement_events]}"))

    new_orders = set(broker.orders) - orders_before
    addon_buys = [(oid, o) for oid in new_orders for o in [broker.orders[oid]]
                 if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                 and o['orderLegCollection'][0]['instruction'] == 'BUY']
    checks.append(Check("exactly one real add-on MARKET BUY landed at the broker, sized to the "
                        "parent's own share count",
                        len(addon_buys) == 1 and addon_buys[0][1]['orderLegCollection'][0]['quantity'] == CORE_A_SHARES,
                        f"addon_buys={[(oid, o['orderType'], o['orderLegCollection'][0]['quantity']) for oid, o in addon_buys]}"))

    leg_a = db.get_open_addon_leg_by_parent(pos_a['id'])
    checks.append(Check("cycle A leg opened and filled", leg_a is not None and leg_a.get('entry_status') == 'filled',
                        f"leg={leg_a}"))
    if leg_a:
        checks.append(Check("cycle A leg sized to the parent's share count",
                            leg_a['shares'] == CORE_A_SHARES, f"shares={leg_a['shares']}"))

    fill_events = db.get_coverage_events(scenario_key='addon_entry_fill')
    checks.append(Check("addon_entry_fill fired 'filled' (bonus check -- not this scenario's primary "
                        "target, but a real side effect of the same real call chain)",
                        any(e['ticker'] == TICKER and e['result'] == 'filled' for e in fill_events),
                        f"events={[(e['result'], e['detail']) for e in fill_events]}"))

    leg_a_id = leg_a['id'] if leg_a else None
    leg_a_sl_order_id = leg_a.get('sl_order_id') if leg_a else None
    checks.append(Check("D3: the leg's own real protective stop was placed (sl_order_id set)",
                        leg_a_sl_order_id is not None, f"sl_order_id={leg_a_sl_order_id}"))

    # ------------------------------------------------------- cycle A close
    exit_price_a = round(price * 1.05, 4)
    exit_time_a = datetime.now()
    # Real production ordering (Part 7.2, all 7 call sites): the parent's
    # open_positions row is deleted BEFORE close_addon_leg_real_if_open ever
    # runs -- this scenario drives that exact ordering, not a simplified one,
    # since a first version of this fix (based on the parent's still-open
    # position) shipped with a CRITICAL bug caught by cold Opus review (the
    # is_addon_leg SELL exemption must be node-scoped, not parent-position-
    # scoped -- see the fake_broker sibling test's own docstring).
    db.close_position(pos_a['id'], exit_signal_price=exit_price_a, exit_price=exit_price_a,
                      exit_time=exit_time_a, exit_reason='SL')
    checks.append(Check("cycle A: parent core position row deleted before the leg close call",
                        db.get_open_position(TICKER) is None))

    say(f"[cycle A close] calling the real signals_notify.close_addon_leg_real_if_open "
        f"(leg has its own resting D3 stop -- REPLACE-to-market branch)")
    notify.close_addon_leg_real_if_open(pos_a, exit_price=exit_price_a, exit_reason='SL', exit_time=exit_time_a)

    if leg_a_sl_order_id is not None:
        old_stop = broker.orders.get(leg_a_sl_order_id)
        checks.append(Check("cycle A: the leg's own resting D3 stop was REPLACED (not a fresh SELL, "
                            "since one was already resting)",
                            old_stop is not None and old_stop['status'] == 'REPLACED',
                            f"status={old_stop['status'] if old_stop else None}"))

    market_sells_a = [(oid, o) for oid, o in _real_orders(broker, TICKER, side='SELL', order_type='MARKET')]
    checks.append(Check("cycle A: exactly one real MARKET SELL for the leg's full share count landed",
                        len(market_sells_a) == 1
                        and market_sells_a[0][1]['orderLegCollection'][0]['quantity'] == CORE_A_SHARES,
                        f"market_sells={[(oid, o['orderLegCollection'][0]['quantity']) for oid, o in market_sells_a]}"))

    checks.append(Check("cycle A leg fully closed", db.get_open_addon_leg_by_parent(pos_a['id']) is None))

    exit_fill_events = db.get_coverage_events(scenario_key='addon_exit_fill')
    checks.append(Check("addon_exit_fill fired 'closed' for cycle A (bonus check)",
                        any(e['ticker'] == TICKER and e['result'] == 'closed' for e in exit_fill_events),
                        f"events={[(e['result'], e['detail']) for e in exit_fill_events]}"))

    # ============================================================= cycle B
    # Only reachable now that cycle A's leg is fully closed -- the real
    # one-leg-per-parent invariant (get_open_addon_leg_by_parent) would
    # otherwise refuse a second leg for this node.
    now_b = datetime.now()
    entry_price_b = round(price * 0.99, 4)
    opened_b = db.open_position(node, signal_price=entry_price_b, signal_time=now_b, entry_price=entry_price_b,
                                entry_time=now_b, shares=CORE_B_SHARES)
    checks.append(Check("cycle B: core position opened", opened_b))
    pos_b = db.get_open_position(TICKER)
    db.update_position_trail_state(pos_b['id'], {'trailing': True, 'peak': entry_price_b})
    pos_b = db.get_open_position(TICKER)

    # Entry side seeded directly this time (cycle A already proved the real
    # entry/placement chain) -- the real broker order is deliberately left
    # resting/unfilled, reproducing the exact window between real order
    # placement and broker confirmation. FakeBroker fills a MARKET order
    # immediately on placement, so the order is forced back to 'WORKING'
    # after seeding -- identical technique to
    # test_leg_still_placed_when_parent_exits_is_cancelled_never_sold.
    entry_order_id_b = broker.seed_resting_order(MARGIN_ALIAS, TICKER, 'MARKET', 'BUY', CORE_B_SHARES)
    broker.orders[entry_order_id_b]['status'] = 'WORKING'
    leg_b_id = db.open_addon_leg(pos_b, shares=CORE_B_SHARES, entry_price=entry_price_b, entry_time=now_b,
                                 paper=False, entry_order_id=entry_order_id_b, entry_status='placed')
    say(f"[cycle B] seeded a still-resting, unfilled addon leg entry order {entry_order_id_b}")

    sells_before_b_close = len(_real_orders(broker, TICKER, side='SELL'))

    exit_price_b = round(price * 0.95, 4)
    exit_time_b = datetime.now()
    db.close_position(pos_b['id'], exit_signal_price=exit_price_b, exit_price=exit_price_b,
                      exit_time=exit_time_b, exit_reason='SL')
    checks.append(Check("cycle B: parent core position row deleted before the leg close call",
                        db.get_open_position(TICKER) is None))

    say(f"[cycle B close] calling the real signals_notify.close_addon_leg_real_if_open "
        f"(leg still entry_status='placed' -- cancel branch)")
    notify.close_addon_leg_real_if_open(pos_b, exit_price=exit_price_b, exit_reason='SL', exit_time=exit_time_b)

    checks.append(Check("cycle B: the leg's still-resting real entry order was CANCELLED",
                        broker.orders[entry_order_id_b]['status'] == 'CANCELED',
                        f"status={broker.orders[entry_order_id_b]['status']}"))

    sells_after_b_close = len(_real_orders(broker, TICKER, side='SELL'))
    checks.append(Check("cycle B: no real SELL was placed for shares never actually bought",
                        sells_after_b_close == sells_before_b_close,
                        f"before={sells_before_b_close} after={sells_after_b_close}"))

    checks.append(Check("cycle B leg closed (ABANDONED)", db.get_open_addon_leg_by_parent(pos_b['id']) is None))

    exit_placement_events = db.get_coverage_events(scenario_key='addon_exit_placement')
    cancelled_events = [e for e in exit_placement_events
                        if e['ticker'] == TICKER and e['result'] == 'cancelled_unfilled_leg']
    checks.append(Check("addon_exit_placement fired 'cancelled_unfilled_leg' -- the leg's still-resting "
                        "unfilled real order was cancelled, never sold",
                        len(cancelled_events) == 1,
                        f"events={[(e['result'], e['detail']) for e in exit_placement_events]}"))

    observations['node_wl_id'] = node['id']
    observations['price'] = price
    observations['leg_a_id'] = leg_a_id
    observations['leg_b_id'] = leg_b_id
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT
  (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='addon_double_buy_exemption'
    AND result='preconditions_passed' AND ticker=?) AS exemption_events,
  (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='addon_entry_placement'
    AND result='placed' AND ticker=?) AS entry_placement_events,
  (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='addon_exit_placement'
    AND result='cancelled_unfilled_leg' AND ticker=?) AS exit_cancel_events,
  (SELECT COUNT(*) FROM addon_legs WHERE ticker=? AND status='open') AS still_open_legs,
  (SELECT COUNT(*) FROM addon_legs WHERE ticker=? AND status='closed') AS closed_legs
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly one of each targeted coverage
    event, zero still-open addon legs, and exactly two closed legs (cycle A's
    real close, cycle B's cancelled/abandoned unfilled entry) -- directly
    from the harness DB."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,) * 5).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['exemption_events'] == 1 and rows[0]['entry_placement_events'] == 1
          and rows[0]['exit_cancel_events'] == 1 and rows[0]['still_open_legs'] == 0
          and rows[0]['closed_legs'] == 2)
    return ok, rows
