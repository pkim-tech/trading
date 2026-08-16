"""Phase 2 scenario: `addon_leg_reconciliation`, the 4th of the 4 targeted
addon Grid rows -- the standalone poll (signals_notify.check_addon_leg_
reconciliation, called each poll cycle alongside check_own_sell_fills/
check_auto_fills) that catches the two ways an add-on leg can drift out of
sync with its parent without either side's own lockstep-close code ever
running:

  (1) a leg still entry_status='placed' (a real order resting, unfilled)
      past a timeout -- polled for a late fill, else the real order is
      CANCELLED and the leg marked ABANDONED. Mirrors check_entry_abandon's
      cancel-with-confirmation pattern for the parent's own entry.

  (2) an open leg whose parent core position has already closed WITHOUT
      close_addon_leg_real_if_open ever running for it (the real lockstep
      close was missed somewhere) -- ALERTED LOUDLY, never auto-closed at a
      guessed price. Pure observation, matching reconcile_daily_track_nodes'
      own stance elsewhere in this codebase.

Both sub-flows run sequentially against the SAME fake node (the real
one-leg-per-parent invariant means only one can be open at a time) --
matching fake_venue/scenarios_addon_lifecycle.py's own two-cycle shape,
which covers the OTHER 3 addon rows (addon_double_buy_exemption,
addon_entry_placement, addon_exit_placement) via the entry/lockstep-exit
chain this scenario deliberately does NOT re-exercise.

  Cycle A (timeout -> abandoned)
    core position #1 opens and arms; the addon leg's entry side is seeded
    directly (bypassing check_addon_trigger_real -- already proven by the
    lifecycle sibling scenario) with a real resting, UNFILLED broker order.
    _ADDON_LEG_ENTRY_TIMEOUT_MINUTES is forced negative (the same technique
    tests/test_fake_broker_addon_lockstep_exit_scenario.py's own
    test_placed_leg_past_timeout_is_cancelled_and_marked_abandoned uses) so
    the very next poll treats the leg as past-timeout without a real sleep.
    check_addon_leg_reconciliation finds no late fill, cancels the real
    order, and marks the leg ABANDONED.
      => coverage_events['addon_leg_reconciliation'] = 'abandoned'  <-- TARGET

  Cycle B (orphaned leg, never auto-closed)
    core position #2 opens and arms; the addon leg is seeded directly as
    already FILLED (a normal successful entry -- this sub-flow's target is
    the missed-lockstep detection, not the entry chain). The parent's
    open_positions row is then closed WITHOUT ever calling
    close_addon_leg_real_if_open -- simulating exactly the "the real
    lockstep close was missed somewhere" case this check exists for.
    check_addon_leg_reconciliation must detect this (trade_log's row for
    the parent is closed, but the leg is still open) and alert loudly --
    critically, it must NOT touch the leg's own open_positions-equivalent
    row (addon_legs stays status='open' at the end of this scenario; this
    is deliberate, not a bug in the scenario).
      => coverage_events['addon_leg_reconciliation'] = 'orphaned_leg_parent_closed'  <-- TARGET
"""
from dataclasses import dataclass
from datetime import datetime

from fake_venue import venue
from fake_venue.scenarios_meta import MARGIN_ACCOUNT_NUMBER, MARGIN_ALIAS, PRICE_SOURCE_TICKER, TICKER

FAKE_ACCOUNTS = [
    dict(alias=MARGIN_ALIAS, notional_cap=100_000, daily_order_cap=100,
         cash_settlement_type='margin', margin_capable=1),
]
CORE_A_SHARES = 12
CORE_B_SHARES = 9


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

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version='fake_venue_addon_reconcile',
                window=10, take_profit=16, stop_loss=1, max_hold_hours=105,
                state='live', account=MARGIN_ALIAS, starting_notional=5_000,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                label='fake-venue harness node (addon_leg_reconciliation)')
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
    say(f"[setup] node wl_id={node['id']} ({MARGIN_ALIAS}), addon_enabled=1")

    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking schwab_safety._is_trading_day "
            "True for this run (see module docstring: orthogonal to the mechanism under test)")
        schwab_safety._is_trading_day = lambda date_str: True

    # ============================================================= cycle A
    # Timeout -> real cancel -> abandoned.
    now_a = datetime.now()
    entry_price_a = round(price * 0.98, 4)
    opened_a = db.open_position(node, signal_price=entry_price_a, signal_time=now_a, entry_price=entry_price_a,
                                entry_time=now_a, shares=CORE_A_SHARES)
    checks.append(Check("cycle A: core position opened", opened_a))
    pos_a = db.get_open_position(TICKER)
    db.update_position_trail_state(pos_a['id'], {'trailing': True, 'peak': entry_price_a})
    pos_a = db.get_open_position(TICKER)

    # Entry side seeded directly (this scenario's target is the standalone
    # reconciliation poll, not the entry/placement chain -- already covered
    # by the lifecycle sibling scenario). FakeBroker fills a MARKET order
    # immediately on placement, so the order is forced back to 'WORKING'
    # right after seeding to reproduce a genuinely still-resting real order,
    # identical technique to the lifecycle sibling's cycle B.
    entry_order_id_a = broker.seed_resting_order(MARGIN_ALIAS, TICKER, 'MARKET', 'BUY', CORE_A_SHARES)
    broker.orders[entry_order_id_a]['status'] = 'WORKING'
    leg_a_id = db.open_addon_leg(pos_a, shares=CORE_A_SHARES, entry_price=entry_price_a, entry_time=now_a,
                                 paper=False, entry_order_id=entry_order_id_a, entry_status='placed')
    say(f"[cycle A] seeded a still-resting, unfilled addon leg entry order {entry_order_id_a}")

    # Force "past timeout" without a real sleep -- identical technique to
    # tests/test_fake_broker_addon_lockstep_exit_scenario.py's own
    # test_placed_leg_past_timeout_is_cancelled_and_marked_abandoned. Plain
    # module-global mutation (not pytest monkeypatch, since this isn't
    # pytest) -- safe because this harness process exits immediately after
    # the run, no other test in this process depends on the original value.
    notify._ADDON_LEG_ENTRY_TIMEOUT_MINUTES = -1

    say("[cycle A] calling the real signals_notify.check_addon_leg_reconciliation([]) -- "
        "leg is past timeout with no late fill available")
    notify.check_addon_leg_reconciliation([])

    checks.append(Check("cycle A: the leg's still-resting real entry order was CANCELLED",
                        broker.orders[entry_order_id_a]['status'] == 'CANCELED',
                        f"status={broker.orders[entry_order_id_a]['status']}"))

    legs_after_a = db.get_open_addon_legs(paper=False)
    checks.append(Check("cycle A: leg is no longer open (closed/abandoned, not left stuck)",
                        all(leg['id'] != leg_a_id for leg in legs_after_a),
                        f"open_legs={[leg['id'] for leg in legs_after_a]}"))

    reconcile_events_a = db.get_coverage_events(scenario_key='addon_leg_reconciliation')
    abandoned_events = [e for e in reconcile_events_a if e['ticker'] == TICKER and e['result'] == 'abandoned']
    checks.append(Check("addon_leg_reconciliation fired 'abandoned' -- TARGET event for the timeout case",
                        len(abandoned_events) == 1,
                        f"events={[(e['result'], e['detail']) for e in reconcile_events_a]}"))

    # Close cycle A's parent so cycle B can open a fresh position on the same
    # ticker/node (the one-leg-per-parent invariant is already satisfied --
    # the leg is closed -- but open_position's own dedup keys on wl_id
    # having no OTHER open row, so the parent must be closed too).
    db.close_position(pos_a['id'], exit_signal_price=price, exit_price=price,
                      exit_time=datetime.now(), exit_reason='TIME')
    checks.append(Check("cycle A: parent core position closed, freeing the node for cycle B",
                        db.get_open_position(TICKER) is None))

    # ============================================================= cycle B
    # Orphaned leg -- parent closes without the real lockstep close ever
    # running. Must be alerted, never auto-closed.
    now_b = datetime.now()
    entry_price_b = round(price * 0.99, 4)
    opened_b = db.open_position(node, signal_price=entry_price_b, signal_time=now_b, entry_price=entry_price_b,
                                entry_time=now_b, shares=CORE_B_SHARES)
    checks.append(Check("cycle B: core position opened", opened_b))
    pos_b = db.get_open_position(TICKER)
    db.update_position_trail_state(pos_b['id'], {'trailing': True, 'peak': entry_price_b})
    pos_b = db.get_open_position(TICKER)

    # A normal, already-FILLED leg (the entry chain isn't this sub-flow's
    # target) -- seeded directly, matching
    # test_orphaned_leg_alerted_loudly_never_auto_closed's own technique.
    leg_b_id = db.open_addon_leg(pos_b, shares=CORE_B_SHARES, entry_price=entry_price_b, entry_time=now_b,
                                 paper=False, entry_status='filled')
    say(f"[cycle B] seeded a filled addon leg (id={leg_b_id})")

    # Parent closes WITHOUT going through close_addon_leg_real_if_open --
    # simulates a missed real lockstep call (the failure mode this check
    # exists to catch).
    db.close_position(pos_b['id'], exit_signal_price=price, exit_price=price,
                      exit_time=datetime.now(), exit_reason='SL')
    checks.append(Check("cycle B: parent core position closed WITHOUT the real lockstep close ever running",
                        db.get_open_position(TICKER) is None))

    leg_b_before_poll = [leg for leg in db.get_open_addon_legs(paper=False) if leg['id'] == leg_b_id]
    checks.append(Check("cycle B: the leg is still open at this instant -- reproduces the exact "
                        "missed-lockstep window",
                        len(leg_b_before_poll) == 1))

    say("[cycle B] calling the real signals_notify.check_addon_leg_reconciliation([]) -- "
        "parent already closed, leg still open")
    notify.check_addon_leg_reconciliation([])

    legs_after_b = [leg for leg in db.get_open_addon_legs(paper=False) if leg['id'] == leg_b_id]
    checks.append(Check("cycle B: the orphaned leg is left OPEN, never auto-closed at a guessed price",
                        len(legs_after_b) == 1 and legs_after_b[0].get('status', 'open') == 'open',
                        f"legs={legs_after_b}"))

    reconcile_events_b = db.get_coverage_events(scenario_key='addon_leg_reconciliation')
    orphan_events = [e for e in reconcile_events_b
                    if e['ticker'] == TICKER and e['result'] == 'orphaned_leg_parent_closed']
    checks.append(Check("addon_leg_reconciliation fired 'orphaned_leg_parent_closed' -- TARGET event "
                        "for the missed-lockstep case",
                        len(orphan_events) == 1,
                        f"events={[(e['result'], e['detail']) for e in reconcile_events_b]}"))

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
  (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='addon_leg_reconciliation'
    AND result='abandoned' AND ticker=?) AS abandoned_events,
  (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='addon_leg_reconciliation'
    AND result='orphaned_leg_parent_closed' AND ticker=?) AS orphaned_events,
  (SELECT COUNT(*) FROM addon_legs WHERE ticker=? AND status='open') AS still_open_legs,
  (SELECT COUNT(*) FROM addon_legs WHERE ticker=? AND status='closed') AS closed_legs
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly one 'abandoned' event, exactly
    one 'orphaned_leg_parent_closed' event, exactly one still-open addon leg
    (cycle B's orphan, deliberately never auto-closed), and exactly one
    closed leg (cycle A's abandoned timeout) -- directly from the harness
    DB."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,) * 4).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['abandoned_events'] == 1 and rows[0]['orphaned_events'] == 1
          and rows[0]['still_open_legs'] == 1 and rows[0]['closed_legs'] == 1)
    return ok, rows
