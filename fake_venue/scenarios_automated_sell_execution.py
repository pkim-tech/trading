"""Phase 2 scenario: `automated_sell_execution` / `manual_sl_fallback_alert`,
proving both branches of `_attempt_automated_sell` (signals_notify.py:90) --
the ARM-time atomic replace of a resting protective stop-loss with a
TRAILING_STOP SELL -- against a real broker: the clean success path, and the
genuinely-failed-replace fallback that surfaces a manual SL price instead of
silently leaving the position unprotected.

Grid rows (scripts/coverage_registry.py): both `automated_sell_execution`
and `manual_sl_fallback_alert` already have real live evidence on file (8x
and 7x respectively per each row's not_prod_required_note), but neither has
a repeatable, deterministic proof against a controlled broker -- this
scenario gives both.

Real bug history this codifies (see `_attempt_automated_sell`'s own
docstring): the 2026-07-27 atomic-replace fix (schwab_client.
replace_order_with_trailing_sell, a single cancel-old+create-new broker call
instead of two independent calls) closed the window where a confirmed
cancel could be followed by a failed/blocked new placement, leaving nothing
resting at the broker -- but a genuinely failed replace is still possible
(any broker-side rejection/exception), and when it happens there's no safe
auto-recovery (re-placing the same SL could hit the same failure), so the
function surfaces the SL price a human needs to manually re-enter (found via
Opus review, 2026-07-22).

Two legs, two nodes (mirrors scenarios_replace_target_mismatch.py's
two-account shape):

  LEG 1  (node A, fv_cash) -- clean replace. A resting protective STOP is
         seeded (the pre-arm state a real entry-time SL placement leaves
         behind); `_attempt_automated_sell` is called directly, the same
         call `notify_trailing_activated` makes at a real arm event.
         Round-trip 1 (_verify_resting_before_replace) finds nothing to
         say; round-trip 2 (the real atomic replace) succeeds.
         => coverage_events['automated_sell_execution']='placed'     <-- checked
         => sl_order_id repointed to the new resting TRAILING_STOP SELL;
            the old STOP is REPLACED, exactly one resting SELL afterward
                                                                       <-- checked
         => broker_stop_price cleared (the SL price it described is now
            gone, replaced by the trailing-sell)                     <-- checked
         => no manual_sl_fallback_alert, no UNPROTECTED alert          <-- checked

  LEG 2  (node B, fv_margin) -- genuinely failed replace.
         `schwab_client.replace_order_with_trailing_sell` is monkeypatched
         to raise BEFORE reaching the broker at all (a real broker-side
         rejection/exception, not something round-trip 1's advisory check
         could have caught -- that check is about TARGETING the right
         order, not about whether the replace call itself succeeds).
         => coverage_events['automated_sell_execution']='failed_unexpectedly'
                                                                       <-- checked
         => coverage_events['manual_sl_fallback_alert']='alerted', with the
            CORRECT algo SL price (computed from the position's own
            persisted fixed_sl/signal_price, not fabricated)          <-- checked
         => a real 🚨 UNPROTECTED Slack alert posted with that price and
            the resting stop-loss id                                 <-- checked
         => the originally-seeded STOP is UNTOUCHED -- still WORKING, never
            replaced (the raise happens before schwab_client ever calls
            the broker, so nothing at the broker moved)               <-- checked
         => open_positions.sl_order_id UNCHANGED -- still points at the
            original (still-live) stop, not overwritten by a failed
            attempt                                                   <-- checked

Entry-side state is SEEDED throughout (sl_order_id / resting STOP inserted
directly), matching every other Phase 2 scenario's accepted caveat -- this
scenario's target is the replace-and-fallback mechanics, not entry
placement or the arm-time signal computation.
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
                version=f'fake_venue_sellexec_{version_suffix}',
                window=20, take_profit=10, stop_loss=FIXED_SL_PCT, max_hold_hours=56,
                state='live', account=account, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=FIXED_SL_PCT,
                label='fake-venue harness node (automated_sell_execution)')
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

    notify._post_message = _capture
    schwab_client._post_message = _capture

    shares = max(int(NODE_NOTIONAL // price), 1)
    expected_stop = round(price * (1 - FIXED_SL_PCT / 100), 4)

    # ============================================================== LEG 1
    say("[leg 1] node A: clean replace at a real arm event")
    node_a = _add_node(CASH_ALIAS, 'a')
    pos_a = _open_pos(node_a, price, shares)
    original_order = broker.seed_resting_order(CASH_ALIAS, TICKER, 'STOP', 'SELL', shares,
                                                stop_price=expected_stop)
    db.set_sl_order_id_by_position(pos_a['id'], original_order)
    db.set_broker_stop_price_by_position(pos_a['id'], expected_stop)
    pos_a = db.get_open_position_by_wl_id(node_a['id'])
    checks.append(Check("leg 1 setup: seeded stop matches the algo's own expected SL price",
                        abs(expected_stop - broker.orders[original_order]['stopPrice']) < 0.0005))

    ok1, new_id1 = notify._attempt_automated_sell(pos_a, current_price=price)
    checks.append(Check("leg 1: clean replace succeeded", ok1 and new_id1 is not None,
                        f"ok={ok1} new_id={new_id1}"))

    sell_events_a = [e for e in db.get_coverage_events(scenario_key='automated_sell_execution')
                     if e['node_id'] == node_a['id']]
    checks.append(Check("automated_sell_execution fired 'placed' for node A",
                        [e['result'] for e in sell_events_a] == ['placed'],
                        f"events={[(e['result'], e['detail']) for e in sell_events_a]}"))

    checks.append(Check("leg 1: the originally-seeded STOP is now REPLACED, not still resting",
                        broker.orders[original_order]['status'] == 'REPLACED',
                        f"status={broker.orders[original_order]['status']}"))
    resting_after_leg1 = _resting_sells(broker, CASH_ALIAS, TICKER)
    checks.append(Check("leg 1: exactly one resting SELL afterward, at the id round-trip 2 returned",
                        len(resting_after_leg1) == 1 and resting_after_leg1[0]['orderId'] == new_id1,
                        f"resting={[o['orderId'] for o in resting_after_leg1]} expected=[{new_id1}]"))
    checks.append(Check("leg 1: the new resting order is a TRAILING_STOP SELL",
                        resting_after_leg1[0]['orderType'] == 'TRAILING_STOP' if resting_after_leg1 else False,
                        f"orderType={resting_after_leg1[0]['orderType'] if resting_after_leg1 else None}"))

    pos_a_after = db.get_open_position_by_wl_id(node_a['id'])
    checks.append(Check("leg 1: position's sl_order_id repointed to the new resting trailing-sell",
                        pos_a_after['sl_order_id'] == new_id1,
                        f"sl_order_id={pos_a_after['sl_order_id']} expected={new_id1}"))
    checks.append(Check("leg 1: broker_stop_price cleared -- the SL price it described is now gone, "
                        "replaced by the trailing-sell (Opus review, 2026-08-01: left uncleared, "
                        "stop_status() would report 'known' off a dead price)",
                        pos_a_after.get('broker_stop_price') is None,
                        f"broker_stop_price={pos_a_after.get('broker_stop_price')}"))

    fallback_a = [e for e in db.get_coverage_events(scenario_key='manual_sl_fallback_alert')
                 if e['node_id'] == node_a['id']]
    checks.append(Check("leg 1: no manual_sl_fallback_alert fired -- nothing failed",
                        fallback_a == []))
    checks.append(Check("leg 1: no UNPROTECTED alert posted",
                        not any('UNPROTECTED' in p for p in posted), f"posted={posted}"))

    # ============================================================== LEG 2
    say("[leg 2] node B: replace_order_with_trailing_sell fails BEFORE reaching the broker at all "
        "-- a genuine broker-side rejection/exception, not a targeting problem round-trip 1 could "
        "have caught")
    node_b = _add_node(MARGIN_ALIAS, 'b')
    pos_b = _open_pos(node_b, price, shares)
    original_order_b = broker.seed_resting_order(MARGIN_ALIAS, TICKER, 'STOP', 'SELL', shares,
                                                  stop_price=expected_stop)
    db.set_sl_order_id_by_position(pos_b['id'], original_order_b)
    pos_b = db.get_open_position_by_wl_id(node_b['id'])

    real_replace_with_trailing_sell = schwab_client.replace_order_with_trailing_sell

    def _raise_replace_failure(account, ticker, order_id, quantity, price, trail_pct,
                                node_dry_run=False, node_id=None):
        raise RuntimeError("simulated broker replace failure (fake_venue scenario)")

    schwab_client.replace_order_with_trailing_sell = _raise_replace_failure
    try:
        ok2, new_id2 = notify._attempt_automated_sell(pos_b, current_price=price)
    finally:
        schwab_client.replace_order_with_trailing_sell = real_replace_with_trailing_sell

    checks.append(Check("leg 2: the failed replace returned (False, None), falling back to manual",
                        ok2 is False and new_id2 is None, f"result=({ok2}, {new_id2})"))

    sell_events_b = [e for e in db.get_coverage_events(scenario_key='automated_sell_execution')
                     if e['node_id'] == node_b['id']]
    checks.append(Check("automated_sell_execution fired 'failed_unexpectedly' for node B",
                        [e['result'] for e in sell_events_b] == ['failed_unexpectedly'],
                        f"events={[(e['result'], e['detail']) for e in sell_events_b]}"))

    fallback_b = [e for e in db.get_coverage_events(scenario_key='manual_sl_fallback_alert')
                 if e['node_id'] == node_b['id']]
    checks.append(Check("manual_sl_fallback_alert fired 'alerted' for node B",
                        [e['result'] for e in fallback_b] == ['alerted'],
                        f"events={[(e['result'], e['detail']) for e in fallback_b]}"))
    checks.append(Check("manual_sl_fallback_alert's detail carries the CORRECT algo SL price "
                        "(computed from the position's own persisted fixed_sl/signal_price, not "
                        "fabricated)",
                        bool(fallback_b) and f"{expected_stop:.2f}" in (fallback_b[0]['detail'] or ''),
                        f"detail={fallback_b[0]['detail'] if fallback_b else None} expected~={expected_stop:.2f}"))

    checks.append(Check("leg 2: a real UNPROTECTED alert posted with the correct SL price and the "
                        "resting stop-loss id",
                        any('UNPROTECTED' in p and f"{expected_stop:.2f}" in p and str(original_order_b) in p
                            for p in posted),
                        f"posted={posted}"))

    checks.append(Check("leg 2: the originally-seeded STOP is UNTOUCHED -- still WORKING (the raise "
                        "happened before schwab_client ever reached the broker, so nothing at the "
                        "broker moved)",
                        broker.orders[original_order_b]['status'] == 'WORKING',
                        f"status={broker.orders[original_order_b]['status']}"))
    resting_after_leg2 = _resting_sells(broker, MARGIN_ALIAS, TICKER)
    checks.append(Check("leg 2: exactly one resting SELL afterward (the original, untouched) -- no "
                        "orphan order created by the failed attempt",
                        len(resting_after_leg2) == 1 and resting_after_leg2[0]['orderId'] == original_order_b,
                        f"resting={[o['orderId'] for o in resting_after_leg2]} expected=[{original_order_b}]"))

    pos_b_after = db.get_open_position_by_wl_id(node_b['id'])
    checks.append(Check("leg 2: sl_order_id UNCHANGED -- still points at the original, still-live "
                        "stop, not overwritten by a failed attempt",
                        pos_b_after['sl_order_id'] == original_order_b,
                        f"sl_order_id={pos_b_after['sl_order_id']} expected={original_order_b}"))

    observations['node_a_wl_id'] = node_a['id']
    observations['node_b_wl_id'] = node_b['id']
    observations['price'] = price
    observations['shares'] = shares
    observations['expected_stop'] = expected_stop
    observations['posted'] = posted
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT wl.id AS wl_id, wl.account,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='automated_sell_execution'
         AND result='placed' AND node_id=wl.id) AS sell_placed,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='automated_sell_execution'
         AND result='failed_unexpectedly' AND node_id=wl.id) AS sell_failed,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='manual_sl_fallback_alert'
         AND result='alerted' AND node_id=wl.id) AS fallback_alerted
  FROM watch_list wl
 WHERE wl.ticker = ?
 ORDER BY wl.id
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly 2 nodes (A, B): node A with
    exactly one 'placed' automated_sell_execution and zero fallback alerts,
    node B with exactly one 'failed_unexpectedly' automated_sell_execution
    and exactly one 'alerted' manual_sl_fallback_alert -- directly from the
    harness DB, not from the in-process checks above."""
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
    ok = (node_a['sell_placed'] == 1 and node_a['sell_failed'] == 0 and node_a['fallback_alerted'] == 0
          and node_b['sell_placed'] == 0 and node_b['sell_failed'] == 1 and node_b['fallback_alerted'] == 1)
    return ok, rows
