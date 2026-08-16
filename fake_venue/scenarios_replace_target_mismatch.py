"""Phase 2 scenario: `replace_target_mismatch`, proving a verify-then-replace
sequence (2 broker round-trips -- confirm the target order's current state,
then replace it) still lands on the RIGHT order even when broker state
changes between them.

Real incident this proves against: SOXS, 2026-08-14. A human placed a manual
(mispriced) stop; the daemon's own SL logic later replaced it with no record
that this had happened. Fixed via signals_notify._verify_resting_before_replace
(bug #4, called from _attempt_automated_sell and _attempt_automated_exit_sell)
-- see that function's docstring and tests/test_replace_target_mismatch.py's
12 unit tests, which already cover the check's own branch logic exhaustively
against a MOCKED _open_orders and a MOCKED schwab_client.replace_*. What none
of those 12 tests exercise: the REAL two-round-trip sequence against a REAL
broker, where round-trip 1 (_verify_resting_before_replace's own
schwab_safety._open_orders(account) call) and round-trip 2 (the real
replace_order_with_trailing_sell/replace_equity_order_with_market call, which
re-reads broker state a SECOND time via check_order's own
_has_open_sell_order guard) are genuinely two separate broker reads, not one
mocked value reused twice. That gap matters because the two reads can
disagree -- broker state can move between them -- and nothing in the unit
suite proves what happens when it does.

Three legs, two nodes:

  LEG 0  (node A, _attempt_automated_sell / TRAIL-arm call site) -- baseline,
         nothing has drifted. Round-trip 1 finds the recorded sl_order_id
         genuinely resting, correctly priced/sized -> silent. Round-trip 2
         (replace_order_with_trailing_sell) succeeds cleanly. Proves the
         ordinary, unchanged-state case correctly targets the same order
         across both round-trips with no noise.
         => coverage_events['replace_target_mismatch'] empty for node A  <-- checked
         => sl_order_id repointed to the new resting order; old one REPLACED,
            exactly one resting SELL afterward                          <-- checked

  LEG A  (node A, continuing; _attempt_automated_exit_sell / TP-SL-TIME call
         site) -- the DETECTED-BEFORE-THE-CHECK case, the literal 2026-08-14
         shape. Between leg 0 and leg A, broker state is mutated OUTSIDE any
         call this scenario is timing (i.e. before round-trip 1 even runs):
         the order leg 0 left resting is canceled and a human-shaped
         mispriced stop takes its place under a NEW id. Round-trip 1 catches
         it (stale id + a real substitute + a mispriced substitute) and
         alerts loudly but does not block. Round-trip 2 (the real replace
         call, using the STALE recorded id) then hits check_order's own
         independent _has_open_sell_order guard, which sees the human's
         order still resting and refuses a second concurrent SELL --
         converting to the manual UNPROTECTED fallback rather than either
         silently adopting the human's order OR leaving two live sells
         resting.
         => coverage_events['replace_target_mismatch'] has
            resting_order_id_stale + stop_price_mismatch for node A      <-- checked
         => coverage_events['automated_exit_execution']='blocked'        <-- checked
         => the human's order is untouched (still WORKING, not REPLACED),
            exactly one resting SELL for the ticker in this account       <-- checked

  LEG B  (node B, fresh; _attempt_automated_sell / TRAIL-arm call site) --
         THE RACE round-trip 1 cannot see: state is unchanged and correct at
         the instant round-trip 1's own broker read executes (so it is
         genuinely, correctly silent -- not a detection miss), but is
         mutated by a human IMMEDIATELY AFTER that read returns and BEFORE
         round-trip 2's own independent broker read (inside check_order)
         runs. This is exactly the residual gap schwab_client.py's
         _submit_replace_with_retry docstring names as "the inherent limit
         of any check-then-act pattern... if the broker accepts a request a
         moment AFTER this loop's own broker-state read completes." What
         this leg proves is that the gap is closed one level up: round-trip
         2 does its OWN fresh read (check_order's _has_open_sell_order), so
         even a check that returned a stale "all clear" a moment earlier
         still cannot result in a duplicate/wrong-target order reaching the
         broker.
         => coverage_events['replace_target_mismatch'] EMPTY for node B
            (round-trip 1 genuinely saw nothing wrong -- this is the honest
            TOCTOU gap, not a bug in the check)                          <-- checked
         => coverage_events['automated_sell_execution']='blocked'        <-- checked
         => the human's order is untouched, exactly one resting SELL for
            the ticker in this account, no orphan third order created     <-- checked

Entry-side state is SEEDED throughout (sl_order_id/resting orders inserted
directly), matching every other Phase 2 scenario's accepted caveat -- this
scenario's target is the replace-targeting sequence, not entry placement.
"""
from dataclasses import dataclass
from datetime import datetime

from fake_venue import venue
from fake_venue.scenarios_meta import CASH_ALIAS, MARGIN_ALIAS, PRICE_SOURCE_TICKER, TICKER

FAKE_ACCOUNTS = [
    dict(alias=CASH_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='cash', margin_capable=0),
    dict(alias=MARGIN_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='margin', margin_capable=1),
]
NODE_NOTIONAL = 2_000
FIXED_SL_PCT = 1.0
# Deliberately far off (not just outside _RECONCILE_SL_PRICE_TOLERANCE=$0.005)
# so the mismatch is unmistakable in the recorded detail string, matching the
# unit suite's own 42.50-vs-49.50-shaped fixtures.
MISPRICE_FRACTION = 0.85


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ''
    required: bool = True

    def __post_init__(self):
        self.ok = bool(self.ok)


def _add_node(account, version_suffix):
    import signals_db as db

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout',
                version=f'fake_venue_replacemismatch_{version_suffix}',
                window=20, take_profit=10, stop_loss=FIXED_SL_PCT, max_hold_hours=56,
                state='live', account=account, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=FIXED_SL_PCT,
                label='fake-venue harness node (replace_target_mismatch)')
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER and n['account'] == account][0]


def _open_pos(node, price, shares):
    import signals_db as db

    now = datetime.now()
    db.open_position(node, signal_price=price, signal_time=now, entry_price=price,
                      entry_time=now, shares=shares)
    return db.get_open_position_by_wl_id(node['id'])


def _resting_sells(broker, account, ticker):
    return [o for o in broker.orders.values()
            if o['account'] == account and o['status'] == 'WORKING'
            and o['orderLegCollection'][0]['instruction'] == 'SELL'
            and o['orderLegCollection'][0]['instrument']['symbol'] == ticker]


def run(price=None, verbose=True):
    """Runs the scenario against the already-isolated, already-imported
    environment. Returns (checks, observations)."""
    import schwab_client
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
    broker = venue.install_fake_broker([CASH_ALIAS, MARGIN_ALIAS])
    price = venue.seed_quote(broker, TICKER, price, price_source_ticker=PRICE_SOURCE_TICKER)
    broker.set_cash_balance(CASH_ALIAS, 100_000.0)
    broker.set_cash_balance(MARGIN_ALIAS, 100_000.0)
    say(f"[setup] {TICKER} quote seeded at ${price:.4f}")

    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking schwab_safety._is_trading_day "
            "True for this run (orthogonal to the mechanism under test, same override every other "
            "Phase 2 scenario uses)")
        schwab_safety._is_trading_day = lambda date_str: True

    posted = []

    def _capture(text=None, *a, **kw):
        posted.append(text if text is not None else (a[0] if a else kw.get('text')))
        return ('C0FAKEVENUE', '9999.1')

    # Both bindings needed -- schwab_client.py did `from signals_blocks import
    # _post_message` at import time (a separate local name, not reachable by
    # patching signals_notify's or signals_blocks' own attribute), and the
    # BLOCKED-replace alert this scenario needs to see is posted from inside
    # schwab_client.py's own replace_* functions, not from signals_notify.py.
    notify._post_message = _capture
    schwab_client._post_message = _capture

    shares = max(int(NODE_NOTIONAL // price), 1)
    expected_stop = round(price * (1 - FIXED_SL_PCT / 100), 4)
    mispriced_stop = round(price * MISPRICE_FRACTION, 4)

    # ============================================================== LEG 0
    say("[leg 0] node A: clean replace, nothing has drifted")
    node_a = _add_node(CASH_ALIAS, 'a')
    pos_a = _open_pos(node_a, price, shares)
    original_order = broker.seed_resting_order(CASH_ALIAS, TICKER, 'STOP', 'SELL', shares,
                                                stop_price=expected_stop)
    db.set_sl_order_id_by_position(pos_a['id'], original_order)
    pos_a = db.get_open_position_by_wl_id(node_a['id'])
    checks.append(Check("leg 0 setup: seeded stop matches the algo's own expected SL price",
                        abs(expected_stop - broker.orders[original_order]['stopPrice']) < 0.0005))

    ok0, new_id0 = notify._attempt_automated_sell(pos_a, current_price=price)
    checks.append(Check("leg 0: clean replace succeeded (round-trip 1 silent, round-trip 2 placed)",
                        ok0 and new_id0 is not None, f"ok={ok0} new_id={new_id0}"))
    checks.append(Check("leg 0: round-trip 1 found nothing to say -- no replace_target_mismatch events",
                        db.get_coverage_events(scenario_key='replace_target_mismatch') == []))
    checks.append(Check("leg 0: only the expected success confirmation posted -- no mismatch/warning "
                        "alert (nothing drifted, so there's nothing to warn about)",
                        len(posted) == 1 and posted[0].startswith('✅ Replaced')
                        and str(original_order) in posted[0],
                        f"posted={posted}"))
    checks.append(Check("leg 0: the originally-seeded order is now REPLACED, not still resting",
                        broker.orders[original_order]['status'] == 'REPLACED',
                        f"status={broker.orders[original_order]['status']}"))
    resting_after_leg0 = _resting_sells(broker, CASH_ALIAS, TICKER)
    checks.append(Check("leg 0: exactly one resting SELL afterward, at the id round-trip 2 returned",
                        len(resting_after_leg0) == 1 and resting_after_leg0[0]['orderId'] == new_id0,
                        f"resting={[o['orderId'] for o in resting_after_leg0]} expected=[{new_id0}]"))
    pos_a = db.get_open_position_by_wl_id(node_a['id'])
    checks.append(Check("leg 0: position's sl_order_id repointed to the new resting order",
                        pos_a['sl_order_id'] == new_id0,
                        f"sl_order_id={pos_a['sl_order_id']} expected={new_id0}"))

    # ============================================================== LEG A
    # Broker state is mutated NOW, entirely outside any round-trip this
    # scenario is timing -- by the time leg A's own call runs, the drift is
    # already old news, exactly the 2026-08-14 SOXS shape (a human replaced
    # the stop at some point before the daemon's exit logic ever looked).
    say("[leg A] node A: a human cancels the resting stop and replaces it with a mispriced one, "
        "then a genuine SL exit signal fires")
    stale_id = new_id0
    broker.orders[stale_id]['status'] = 'CANCELED'
    human_order = broker.seed_resting_order(CASH_ALIAS, TICKER, 'STOP', 'SELL', shares,
                                             stop_price=mispriced_stop)
    say(f"[leg A] broker: order {stale_id} CANCELED; human order {human_order} now resting "
        f"@ ${mispriced_stop:.4f} (algo expects ${expected_stop:.4f})")

    new_id_a = notify._attempt_automated_exit_sell(pos_a, reason='SL', current_price=price)
    checks.append(Check("leg A: round-trip 2 was BLOCKED, not silently succeeded or silently adopted",
                        new_id_a is None, f"result={new_id_a}"))

    mismatch_a = db.get_coverage_events(scenario_key='replace_target_mismatch')
    results_a = sorted(e['result'] for e in mismatch_a if e['node_id'] == node_a['id'])
    checks.append(Check("leg A: round-trip 1 (the advisory check) caught BOTH the stale id and the "
                        "mispriced substitute",
                        results_a == ['resting_order_id_stale', 'stop_price_mismatch'],
                        f"results={results_a}"))
    checks.append(Check("leg A: round-trip 2's own independent guard (check_order's "
                        "_has_open_sell_order) blocked the replace -- the human's still-resting order "
                        "is what actually prevented a wrong-target/duplicate order, not the advisory "
                        "check alone",
                        any('BLOCKED replace' in p and str(stale_id) in p for p in posted),
                        f"posted={posted}"))
    checks.append(Check("leg A: the UNPROTECTED manual-fallback alert fired with the correct algo SL "
                        "price (not the human's mispriced one)",
                        any('UNPROTECTED' in p and f"{expected_stop:.2f}" in p for p in posted),
                        f"posted={posted}"))
    exec_events_a = db.get_coverage_events(scenario_key='automated_exit_execution')
    blocked_a = [e for e in exec_events_a if e['result'] == 'blocked' and e['node_id'] == node_a['id']]
    checks.append(Check("leg A: automated_exit_execution logged 'blocked' for node A",
                        len(blocked_a) == 1, f"events={[(e['result'], e['detail']) for e in exec_events_a]}"))
    checks.append(Check("leg A: the human's order is untouched -- still WORKING, never REPLACED by us",
                        broker.orders[human_order]['status'] == 'WORKING',
                        f"status={broker.orders[human_order]['status']}"))
    resting_after_lega = _resting_sells(broker, CASH_ALIAS, TICKER)
    checks.append(Check("leg A: exactly one resting SELL afterward (the human's) -- no orphan third "
                        "order, no oversell exposure",
                        len(resting_after_lega) == 1 and resting_after_lega[0]['orderId'] == human_order,
                        f"resting={[o['orderId'] for o in resting_after_lega]} expected=[{human_order}]"))
    pos_a_after = db.get_open_position_by_wl_id(node_a['id'])
    checks.append(Check("leg A: sl_order_id was NOT overwritten by a failed replace (still points at "
                        "the dead, canceled id -- correctly reflects that nothing new was placed)",
                        pos_a_after['sl_order_id'] == stale_id,
                        f"sl_order_id={pos_a_after['sl_order_id']} expected={stale_id}"))

    # ============================================================== LEG B
    say("[leg B] node B: round-trip 1 reads a genuinely clean, correctly-matched state -- then a "
        "human replaces the order in the gap before round-trip 2's own independent broker read")
    node_b = _add_node(MARGIN_ALIAS, 'b')
    pos_b = _open_pos(node_b, price, shares)
    clean_order = broker.seed_resting_order(MARGIN_ALIAS, TICKER, 'STOP', 'SELL', shares,
                                             stop_price=expected_stop)
    db.set_sl_order_id_by_position(pos_b['id'], clean_order)
    pos_b = db.get_open_position_by_wl_id(node_b['id'])

    real_open_orders = schwab_safety._open_orders
    race_state = {'fired': False}

    def _racing_open_orders(account):
        # Real read FIRST -- this is what round-trip 1 (or, on later calls,
        # round-trip 2) actually sees. Only after that snapshot is taken do
        # we mutate broker state, and only once, so exactly one caller (the
        # first) gets the pre-mutation view and every caller after it gets
        # the post-mutation one -- modeling a change that lands in the real
        # gap between two genuinely separate broker reads, not a change this
        # scenario is faking away by patching an internal decision instead
        # of the broker state itself.
        snapshot = real_open_orders(account)
        if account == MARGIN_ALIAS and not race_state['fired']:
            race_state['fired'] = True
            broker.orders[clean_order]['status'] = 'CANCELED'
            new_id = broker.seed_resting_order(MARGIN_ALIAS, TICKER, 'STOP', 'SELL', shares,
                                               stop_price=mispriced_stop)
            race_state['human_order'] = new_id
            say(f"[leg B] (mid-sequence, after round-trip 1's read returned) broker: order "
                f"{clean_order} CANCELED; human order {new_id} now resting @ ${mispriced_stop:.4f}")
        return snapshot

    schwab_safety._open_orders = _racing_open_orders
    try:
        ok_b, new_id_b = notify._attempt_automated_sell(pos_b, current_price=price)
    finally:
        schwab_safety._open_orders = real_open_orders

    checks.append(Check("leg B: round-trip 1's own read genuinely happened before the mutation "
                        "(the wrapper actually fired)", race_state['fired']))
    checks.append(Check("leg B: round-trip 1 was HONESTLY silent -- it saw real, correctly-matched "
                        "state at the moment it read, this is not a detection miss",
                        [e for e in db.get_coverage_events(scenario_key='replace_target_mismatch')
                         if e['node_id'] == node_b['id']] == []))
    checks.append(Check("leg B: round-trip 2 was still BLOCKED despite round-trip 1's clean read -- "
                        "its OWN fresh broker read (check_order's _has_open_sell_order) is what "
                        "actually closed the race, not the advisory check",
                        ok_b is False and new_id_b is None, f"result=({ok_b}, {new_id_b})"))
    human_order_b = race_state.get('human_order')
    checks.append(Check("leg B: the human's order is untouched -- still WORKING, never REPLACED",
                        human_order_b is not None and broker.orders[human_order_b]['status'] == 'WORKING',
                        f"status={broker.orders.get(human_order_b, {}).get('status')}"))
    resting_after_legb = _resting_sells(broker, MARGIN_ALIAS, TICKER)
    checks.append(Check("leg B: exactly one resting SELL afterward (the human's) -- no orphan third "
                        "order created despite the race",
                        len(resting_after_legb) == 1 and resting_after_legb[0]['orderId'] == human_order_b,
                        f"resting={[o['orderId'] for o in resting_after_legb]} expected=[{human_order_b}]"))
    exec_events_b = db.get_coverage_events(scenario_key='automated_sell_execution')
    blocked_b = [e for e in exec_events_b if e['result'] == 'blocked' and e['node_id'] == node_b['id']]
    checks.append(Check("leg B: automated_sell_execution logged 'blocked' for node B",
                        len(blocked_b) == 1, f"events={[(e['result'], e['detail']) for e in exec_events_b]}"))

    observations['node_a_wl_id'] = node_a['id']
    observations['node_b_wl_id'] = node_b['id']
    observations['price'] = price
    observations['shares'] = shares
    observations['expected_stop'] = expected_stop
    observations['mispriced_stop'] = mispriced_stop
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT wl.id AS wl_id, wl.account,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='replace_target_mismatch'
         AND node_id=wl.id) AS mismatch_events,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='automated_sell_execution'
         AND result='blocked' AND node_id=wl.id) AS sell_blocked,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='automated_exit_execution'
         AND result='blocked' AND node_id=wl.id) AS exit_blocked
  FROM watch_list wl
 WHERE wl.ticker = ?
 ORDER BY wl.id
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly 2 nodes (A, B) on file, node A
    logging 2 replace_target_mismatch events (leg A's detected case) plus one
    blocked automated_exit_execution (leg A), and node B logging ZERO
    replace_target_mismatch events (the honest TOCTOU gap in leg B) plus one
    blocked automated_sell_execution (leg B) -- directly from the harness
    DB, not from the in-process checks above."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    if len(rows) != 2:
        return False, rows
    node_a, node_b = rows[0], rows[1]
    ok = (node_a['mismatch_events'] == 2 and node_a['exit_blocked'] == 1
          and node_b['mismatch_events'] == 0 and node_b['sell_blocked'] == 1)
    return ok, rows
