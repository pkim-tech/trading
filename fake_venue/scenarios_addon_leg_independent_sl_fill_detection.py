"""Phase 2 scenario: `addon_leg_independent_sl_fill_detection`, one level
down from `sl_order_fills_independent_detection.py` (read that file first --
this scenario is its add-on-leg-scoped sibling and follows the same shape).

Grid row (scripts/coverage_registry.py, id='addon_leg_independent_sl_fill_
detection'): an add-on leg's OWN protective stop (`_place_stop_loss_for_
addon_leg`, `leg['sl_order_id']`) rests continuously at the broker, entirely
independent of the parent core position's lockstep exit signal -- it can fill
on its own before that signal is ever computed. `check_addon_leg_
reconciliation`'s `sl_order_id` branch (signals_notify.py ~2855) exists to
catch exactly this, mirroring `check_sl_order_fills` for the core position
one level down. The row's own notes are explicit: "No fake_broker regression
test written for this specific branch (unlike the core-position sibling,
which has 5) -- open follow-up." This scenario is that follow-up, at the
fake-venue level (not a fake_broker unit test).

Deliberately NOT re-exercising what the sibling scenarios already cover:
  - scenarios_addon_lifecycle.py / scenarios_addon_reconciliation.py cover
    entry placement, lockstep exit-on-parent-close, the timeout->abandoned
    path, and the orphaned-leg-parent-closed path.
  - This scenario's only target is the `sl_order_id` branch: a leg's stop
    fills ON ITS OWN, with the parent core position still open and no
    lockstep exit ever computed.

Shape (one fake node, one fake margin account, mirrors the core-position
scenario's 3-leg structure):

  Setup   a core position is opened and armed (seeded, matching the addon
          sibling scenarios), then a REAL add-on leg is opened via the real
          `_open_addon_leg_real`-adjacent path is NOT exercised here (already
          proven by scenarios_addon_lifecycle.py) -- the leg's entry side is
          seeded directly as already `entry_status='filled'`, same technique
          scenarios_addon_reconciliation.py's cycle B uses, since this
          scenario's target starts at the leg's protective-stop placement,
          not its entry.

  Leg 1   the REAL protective stop for the add-on leg is placed via
          `signals_notify._place_stop_loss_for_addon_leg` -- exercises the
          genuine `schwab_client.place_stop_loss(..., is_addon_leg=True)`
          call, a real broker STOP order lands in FakeBroker's order book,
          and `addon_legs.sl_order_id` is set by the real production write
          (`db.set_addon_leg_sl_order_id`), not test scaffolding.
          => coverage_events['sl_placement'] = 'placed'             <-- checked

  Leg 2   the stop fires ON ITS OWN at the broker -- FakeBroker's own resting-
          STOP auto-trigger, zero involvement from any of our code. The core
          parent position is asserted STILL OPEN throughout (no lockstep exit
          ever computed -- that absence is the point: this scenario proves
          detection independent of the parent's own exit signal, exactly the
          gap the Grid row's notes flag as unproven). The leg is asserted
          still `status='open'` at this instant, reproducing the exact
          undetected-fill window before the poll below ever runs.

  Leg 3   `signals_notify.check_addon_leg_reconciliation` (the real function,
          called exactly as `active_signals.py`'s `run_loop` calls it every
          cycle) detects the fill via the standalone `sl_order_id` poll and
          closes the leg -- the parent core position is untouched (still
          open, its own separate lifecycle), proving this is leg-scoped
          independent detection, not a lockstep close riding on the parent.
          => coverage_events['addon_exit_fill'] = 'sl_closed_reconcile'
             <-- TARGET (the exact result string coverage_registry.py's
             bad_results list is built to exclude every OTHER value for)
          => addon_legs row closed (status='closed', exit_reason=
             'SL_RECONCILED') at the broker's real fill price
          => core parent position (open_positions) still open, untouched
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from fake_venue import venue
from fake_venue.scenarios_meta import MARGIN_ACCOUNT_NUMBER, MARGIN_ALIAS, PRICE_SOURCE_TICKER, TICKER

FAKE_ACCOUNTS = [
    dict(alias=MARGIN_ALIAS, notional_cap=100_000, daily_order_cap=100,
         cash_settlement_type='margin', margin_capable=1),
]
CORE_SHARES = 10
LEG_SHARES = 4
# Matches the core-position sibling's LABD-faithful hair-trigger choice --
# keeps this scenario's stop close to the seeded price so advance_price's
# fill move is small and unambiguous.
FIXED_SL_PCT = 1.0


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

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version='fake_venue_addon_sl_poll',
                window=10, take_profit=16, stop_loss=1, max_hold_hours=105,
                state='live', account=MARGIN_ALIAS, starting_notional=5_000,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=FIXED_SL_PCT,
                label='fake-venue harness node (addon_leg_independent_sl_fill_detection)')
    with db._conn() as c:
        c.execute("UPDATE watch_list SET addon_enabled=1 WHERE ticker=?", (TICKER,))
        c.commit()
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]


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
    say(f"[setup] node wl_id={node['id']} ({MARGIN_ALIAS}), addon_enabled=1, fixed_sl={FIXED_SL_PCT}%")

    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking schwab_safety._is_trading_day "
            "True for this run (orthogonal to the mechanism under test)")
        schwab_safety._is_trading_day = lambda date_str: True

    # Core parent position -- entry side seeded directly (this scenario's
    # target starts at the leg's protective-stop placement, not the entry
    # chain -- already covered by the lifecycle sibling scenario).
    entry_time = datetime.now() - timedelta(hours=1)
    entry_price = price
    opened = db.open_position(node, signal_price=entry_price, signal_time=entry_time, entry_price=entry_price,
                              entry_time=entry_time, shares=CORE_SHARES)
    checks.append(Check("core parent position opened", opened))
    pos = db.get_open_position(TICKER)
    db.update_position_trail_state(pos['id'], {'trailing': True, 'peak': entry_price})
    pos = db.get_open_position(TICKER)
    checks.append(Check("core position has no sl_order_id (core's own stop is a separate, "
                        "untouched mechanism -- not this scenario's target)",
                        pos is not None and pos.get('sl_order_id') is None))

    # Add-on leg -- entry side seeded directly as already 'filled', same
    # technique scenarios_addon_reconciliation.py's cycle B uses (the entry
    # chain is proven by the lifecycle sibling, not this scenario's target).
    leg_entry_price = round(price * 0.995, 4)
    leg_id = db.open_addon_leg(pos, shares=LEG_SHARES, entry_price=leg_entry_price, entry_time=datetime.now(),
                               paper=False, entry_status='filled')
    say(f"[setup] add-on leg id={leg_id} seeded as already-filled ({LEG_SHARES}sh @ ${leg_entry_price:.4f})")
    leg = db.get_open_addon_leg_by_parent(pos['id'])
    checks.append(Check("add-on leg visible via get_open_addon_leg_by_parent", leg is not None))
    checks.append(Check("leg has no sl_order_id yet (placement hasn't run)",
                        leg is not None and leg.get('sl_order_id') is None))

    # ---------------------------------------------------------------- leg 1
    # The REAL production placement path for the LEG's own stop -- not a
    # hand-seeded resting order. Anchored to the PARENT's entry_price, per
    # _place_stop_loss_for_addon_leg's own docstring (the leg has no
    # independent exit rule -- it tracks the parent's real stop level).
    say(f"[leg 1] calling the real signals_notify._place_stop_loss_for_addon_leg({leg_id}, pos, node)")
    notify._place_stop_loss_for_addon_leg(leg_id, pos, node)

    sl_events = db.get_coverage_events(scenario_key="sl_placement")
    placed = [e for e in sl_events if e['result'] == 'placed' and e['node_id'] == node['id']
              and f"addon_leg={leg_id}" in (e['detail'] or '')]
    checks.append(Check("_place_stop_loss_for_addon_leg's real placement fired 'placed'",
                        len(placed) == 1,
                        f"events={[(e['result'], e['detail']) for e in sl_events]}"))

    leg = db.get_open_addon_leg_by_parent(pos['id'])
    sl_order_id = leg.get('sl_order_id') if leg else None
    checks.append(Check("addon_legs.sl_order_id set by the real placement write "
                        "(db.set_addon_leg_sl_order_id, not test scaffolding)",
                        sl_order_id is not None,
                        f"sl_order_id={sl_order_id}"))

    stop_orders = [o for oid, o in broker.orders.items()
                   if oid == sl_order_id and o['orderType'] == 'STOP']
    checks.append(Check("a genuine broker STOP order exists for the leg's sl_order_id, "
                        "sized for the LEG's own share count (not the core position's)",
                        len(stop_orders) == 1 and stop_orders[0]['orderLegCollection'][0]['quantity'] == LEG_SHARES,
                        f"stop_orders={[(o['orderType'], o['stopPrice'], o['orderLegCollection'][0]['quantity']) for o in stop_orders]}"))
    stop_price = stop_orders[0]['stopPrice'] if stop_orders else None
    # _place_stop_loss_for_addon_leg anchors to the PARENT's entry_price, not
    # the leg's own fill price -- per its own docstring.
    expected_stop_price = round(entry_price * (1 - FIXED_SL_PCT / 100), 4)
    checks.append(Check("leg's stop is anchored to the PARENT's entry_price * (1 - fixed_sl%), "
                        "not the leg's own (different) entry_price -- matches "
                        "_place_stop_loss_for_addon_leg's documented behavior",
                        stop_price is not None and abs(stop_price - expected_stop_price) < 0.01,
                        f"stop_price={stop_price} expected~={expected_stop_price}"))

    # ---------------------------------------------------------------- leg 2
    # The leg's stop fires ON ITS OWN at the broker -- zero involvement from
    # any of our code. No bar-close scan, no lockstep-exit computation, no
    # signal-window check anywhere in this scenario. The core parent stays
    # open throughout: this is what proves detection is independent of the
    # parent's own exit signal, not riding along with it.
    fill_price = round(stop_price * 0.999, 4)
    say(f"[leg 2] broker fires the leg's resting STOP on its own (advance_price to ${fill_price:.4f}, "
        f"no code of ours involved)")
    broker.advance_price(TICKER, last=fill_price, bid=fill_price, ask=fill_price)
    checks.append(Check("the leg's stop is FILLED at the broker",
                        broker.orders[sl_order_id]['status'] == 'FILLED',
                        f"status={broker.orders[sl_order_id]['status']}"))

    checks.append(Check("core parent position is STILL OPEN throughout -- no lockstep exit was "
                        "ever computed (proves this isn't a lockstep close riding on the parent)",
                        db.get_open_position(TICKER) is not None))
    leg_before_poll = db.get_open_addon_leg_by_parent(pos['id'])
    checks.append(Check("leg is STILL OPEN locally -- the fill is real but undetected "
                        "(reproduces the exact undetected-fill window, one level down from LABD)",
                        leg_before_poll is not None and leg_before_poll.get('id') == leg_id,
                        f"leg={'present' if leg_before_poll else None}"))

    # ---------------------------------------------------------------- leg 3
    # The real, standalone poll -- called exactly as active_signals.py's
    # run_loop calls it every cycle. This scenario deliberately never invokes
    # any lockstep-exit/bar-close code at all.
    say("[leg 3] calling the real signals_notify.check_addon_leg_reconciliation([]) -- "
        "the standalone poll, independent of any lockstep-exit/bar-close check")
    notify.check_addon_leg_reconciliation([])

    leg_after_poll = db.get_open_addon_leg_by_parent(pos['id'])
    checks.append(Check("leg closed by the independent poll -- no stuck-open window",
                        leg_after_poll is None))
    checks.append(Check("core parent position UNTOUCHED by the leg's independent close "
                        "(still open, its own separate lifecycle)",
                        db.get_open_position(TICKER) is not None))

    with db._conn() as c:
        rows = c.execute(
            "SELECT exit_price, exit_reason, wl_id, account, status FROM addon_legs WHERE id=?",
            (leg_id,)).fetchone()
    checks.append(Check("addon_legs row closed with exit_reason='SL_RECONCILED'",
                        rows is not None and rows[1] == 'SL_RECONCILED' and rows[4] == 'closed',
                        f"row={tuple(rows) if rows else None}"))
    if rows is not None:
        exit_price = rows[0]
        checks.append(Check("leg's exit_price matches the broker's real fill price, not a "
                            "theoretical/target price",
                            exit_price is not None and abs(exit_price - fill_price) < 0.0005,
                            f"exit_price={exit_price} fill_price={fill_price}"))
        checks.append(Check("addon_legs row attributed to the real node/account",
                            rows[2] == node['id'] and rows[3] == MARGIN_ALIAS,
                            f"wl_id={rows[2]} account={rows[3]}"))

    exit_events = db.get_coverage_events(scenario_key="addon_exit_fill")
    sl_reconciled = [e for e in exit_events
                     if e['result'] == 'sl_closed_reconcile' and e['node_id'] == node['id']
                     and f"leg_id={leg_id}" in (e['detail'] or '')]
    checks.append(Check("addon_exit_fill fired 'sl_closed_reconcile' -- TARGET result, "
                        "distinct from every lockstep-close result "
                        "(coverage_registry.py's bad_results list depends on this exact string)",
                        len(sl_reconciled) == 1,
                        f"events={[(e['result'], e['detail']) for e in exit_events]}"))

    observations['node_wl_id'] = node['id']
    observations['price'] = price
    observations['leg_id'] = leg_id
    observations['stop_price'] = stop_price
    observations['fill_price'] = fill_price
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT al.exit_reason, al.status, al.wl_id, al.account, wl.state,
       (SELECT COUNT(*) FROM open_positions WHERE wl_id = al.wl_id) AS core_still_open,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='addon_exit_fill'
         AND result='sl_closed_reconcile' AND node_id=al.wl_id) AS sl_reconcile_events
  FROM addon_legs al
  JOIN watch_list wl ON wl.id = al.wl_id
 WHERE al.ticker = ?
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly one closed addon_legs row, with
    exit_reason='SL_RECONCILED', exactly one still-open core position for the
    node (untouched by the leg's close), and exactly one 'sl_closed_reconcile'
    coverage_events row -- directly from the harness DB."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['exit_reason'] == 'SL_RECONCILED' and rows[0]['status'] == 'closed'
          and rows[0]['core_still_open'] == 1 and rows[0]['sl_reconcile_events'] == 1
          and rows[0]['state'] == 'live')
    return ok, rows
