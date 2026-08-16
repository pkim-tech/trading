"""Phase 2 scenario: `automated_exit_execution`, proving the general TP/SL/TIME
exit-replace chain of `_attempt_automated_exit_sell` (signals_notify.py:217)
against a real broker -- distinct from `time_exit_trigger_armed`'s scenario
(the hold-time-forced-while-ARMED sub-case, which force-replaces an arm-time
TRAILING_STOP) and from `automated_sell_execution`'s scenario (the earlier
ARM-time replace of a protective STOP with a TRAILING_STOP). This is the
later, more common case: a plain TP/SL/TIME condition fires against a
position that was never armed at all, and the resting protective STOP itself
gets atomically replaced with a market sell.

Grid row (scripts/coverage_registry.py, id='automated_exit_execution'):
added 2026-08-14 after an Opus audit found 14 real live events (incl. 6
'blocked', 2 'failed_unexpectedly') with no Grid row at all -- this scenario
gives the first repeatable, deterministic proof of the mechanism.

Three legs, one node (the reuse-check is inherently sequential -- a second
bar's re-fire on the SAME still-unresolved condition, not a parallel case):

  LEG 1  fresh SL exit, no exit_pending yet. `_attempt_automated_exit_sell`
         is called directly (reason='SL', hold_time_forced=False) --
         resolves resting_order_id to sl_order_id (never touches
         trail_state.exit_order_id, unlike the hold-time-forced branch),
         labels it "stop-loss", and atomically replaces it with a market
         sell.
         => coverage_events['automated_exit_execution']='placed', detail
            containing reason=SL                                    <-- checked
         => sl_order_id repointed to the new MARKET SELL order; the old
            STOP is REPLACED                                        <-- checked
         => broker_stop_price cleared                                <-- checked

  LEG 2  THE REUSE-PENDING-ORDER CHECK. Simulates a second bar's re-fire on
         the exact same still-true SL condition BEFORE the leg 1 order's
         own fill has been confirmed -- exit_pending seeded directly with
         leg 1's order_id (mirroring what notify_sell_signal itself writes
         when its own confirm-poll doesn't find an immediate fill; seeded
         here rather than driven through notify_sell_signal to avoid that
         function's interactive-input fallback branch entirely, which would
         block on stdin with SIM_MODE/INTERACTIVE both False in this
         harness -- see docstring note on isolation.py's SOCKET_MODE=False).
         `_attempt_automated_exit_sell` called again with the SAME reason --
         must return the EXISTING order_id via the early
         `pending_order_id is not None` branch, WITHOUT placing any new
         broker order (this is what "sell_alerted's dedup only covers the
         bar the order was placed on" -- see the function's own docstring --
         exists to prevent: a second real market sell for the same shares).
         => the SAME order_id is returned, not a new one                 <-- checked
         => zero new broker orders created by leg 2                      <-- checked
         => automated_exit_execution NOT re-logged (short-circuited before
            reaching that code entirely)                                 <-- checked

  LEG 3  CONFIRM, via the real production entrypoint. A fresh position/exit
         (reason='TP', a DIFFERENT ACCOUNT -- fv_margin, not fv_cash -- so
         schwab_safety's same-ticker duplicate-order guard, which legitimately
         sees leg 1's just-placed 40-share SELL as a same-account/same-ticker/
         same-size match within its dedup window, doesn't block this leg's
         own placement; found live by this scenario's first run) driven
         through the actual `notify_sell_signal` -- the market sell FakeBroker
         fills same-tick,
         notify_sell_signal's own short bounded poll confirms it on the
         first attempt, and the position auto-closes with no manual Slack
         tap, closing the loop from "replace" all the way to "confirm".
         => coverage_events['automated_exit_execution']='placed' for reason=TP
                                                                           <-- checked
         => position closed, trade_log exit_reason='TP'                  <-- checked
         => no manual_sl_fallback_alert (nothing failed)                 <-- checked

Entry-side state is SEEDED throughout (sl_order_id / resting STOP inserted
directly), matching every other Phase 2 scenario's accepted caveat.
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


def _add_node(version_suffix, account=CASH_ALIAS):
    import signals_db as db

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout',
                version=f'fake_venue_exitexec_{version_suffix}',
                window=20, take_profit=10, stop_loss=FIXED_SL_PCT, max_hold_hours=56,
                state='live', account=account, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=FIXED_SL_PCT,
                label='fake-venue harness node (automated_exit_execution)')
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER and n['version'] == f'fake_venue_exitexec_{version_suffix}'][0]


def _open_pos(node, price, shares):
    import signals_db as db

    now = datetime.now()
    db.open_position(node, signal_price=price, signal_time=now, entry_price=price,
                      entry_time=now, shares=shares)
    return db.get_open_position_by_wl_id(node['id'])


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
    say("[leg 1] node A: fresh SL exit, no exit_pending yet")
    node_a = _add_node('a')
    pos_a = _open_pos(node_a, price, shares)
    original_order = broker.seed_resting_order(CASH_ALIAS, TICKER, 'STOP', 'SELL', shares,
                                                stop_price=expected_stop)
    db.set_sl_order_id_by_position(pos_a['id'], original_order)
    db.set_broker_stop_price_by_position(pos_a['id'], expected_stop)
    pos_a = db.get_open_position_by_wl_id(node_a['id'])

    orders_before_leg1 = set(broker.orders)
    order_id_1 = notify._attempt_automated_exit_sell(pos_a, reason='SL', current_price=price)
    checks.append(Check("leg 1: a real order_id was returned", order_id_1 is not None,
                        f"order_id_1={order_id_1}"))

    exec_events = [e for e in db.get_coverage_events(scenario_key='automated_exit_execution')
                  if e['node_id'] == node_a['id']]
    placed = [e for e in exec_events if e['result'] == 'placed' and 'reason=SL' in (e['detail'] or '')]
    checks.append(Check("automated_exit_execution fired 'placed' for reason=SL",
                        len(placed) == 1, f"events={[(e['result'], e['detail']) for e in exec_events]}"))

    checks.append(Check("leg 1: the originally-seeded STOP is now REPLACED",
                        broker.orders[original_order]['status'] == 'REPLACED',
                        f"status={broker.orders[original_order]['status']}"))
    new_orders_leg1 = set(broker.orders) - orders_before_leg1
    checks.append(Check("leg 1: exactly one new order placed, a MARKET SELL, matching order_id_1",
                        len(new_orders_leg1) == 1 and order_id_1 in new_orders_leg1
                        and broker.orders[order_id_1]['orderType'] == 'MARKET',
                        f"new_orders={new_orders_leg1} order_id_1={order_id_1} "
                        f"orderType={broker.orders.get(order_id_1, {}).get('orderType')}"))

    pos_a_after_leg1 = db.get_open_position_by_wl_id(node_a['id'])
    checks.append(Check("leg 1: sl_order_id repointed to the new MARKET SELL order",
                        pos_a_after_leg1['sl_order_id'] == order_id_1,
                        f"sl_order_id={pos_a_after_leg1['sl_order_id']} expected={order_id_1}"))
    checks.append(Check("leg 1: broker_stop_price cleared",
                        pos_a_after_leg1.get('broker_stop_price') is None,
                        f"broker_stop_price={pos_a_after_leg1.get('broker_stop_price')}"))

    # ============================================================== LEG 2
    say("[leg 2] node A: a second bar re-fires the SAME still-true SL condition before leg 1's own "
        "fill is confirmed -- exit_pending seeded to mirror what notify_sell_signal itself would "
        "have written on an unconfirmed poll")
    state = dict(pos_a_after_leg1.get('trail_state') or {})
    state['exit_pending'] = {
        'reason': 'SL', 'current_price': price, 'target_price': expected_stop,
        'reminder_channel': None, 'reminder_ts': None, 'reminder_count': 0,
        'last_reminder_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'order_id': order_id_1,
    }
    db.update_position_trail_state(pos_a_after_leg1['id'], state)
    pos_a_leg2 = db.get_open_position_by_wl_id(node_a['id'])
    checks.append(Check("leg 2 setup: exit_pending carries leg 1's order_id",
                        pos_a_leg2['trail_state'].get('exit_pending', {}).get('order_id') == order_id_1,
                        f"exit_pending={pos_a_leg2['trail_state'].get('exit_pending')}"))

    orders_before_leg2 = set(broker.orders)
    events_before_leg2 = len(db.get_coverage_events(scenario_key='automated_exit_execution'))
    order_id_2 = notify._attempt_automated_exit_sell(pos_a_leg2, reason='SL', current_price=price)

    checks.append(Check("leg 2: the SAME order_id was returned -- no second order placed for the "
                        "same still-true condition (the exact duplicate-market-sell gap "
                        "_attempt_automated_exit_sell's own docstring names, found by Sonnet review "
                        "2026-07-27)",
                        order_id_2 == order_id_1, f"order_id_2={order_id_2} expected={order_id_1}"))
    checks.append(Check("leg 2: zero new broker orders created",
                        set(broker.orders) == orders_before_leg2,
                        f"new_orders={set(broker.orders) - orders_before_leg2}"))
    checks.append(Check("leg 2: automated_exit_execution NOT re-logged -- the reuse branch "
                        "short-circuits before reaching that code at all",
                        len(db.get_coverage_events(scenario_key='automated_exit_execution')) == events_before_leg2))

    # ============================================================== LEG 3
    say("[leg 3] node B (fv_margin): CONFIRM, via the real production entrypoint "
        "notify_sell_signal(reason='TP')")
    node_b = _add_node('b', account=MARGIN_ALIAS)
    pos_b = _open_pos(node_b, price, shares)
    original_order_b = broker.seed_resting_order(MARGIN_ALIAS, TICKER, 'STOP', 'SELL', shares,
                                                  stop_price=expected_stop)
    db.set_sl_order_id_by_position(pos_b['id'], original_order_b)
    pos_b = db.get_open_position_by_wl_id(node_b['id'])

    notify.notify_sell_signal(pos_b, 'TP', current_price=price * 1.05, target_price=price * 1.05)

    exec_events_b = [e for e in db.get_coverage_events(scenario_key='automated_exit_execution')
                     if e['node_id'] == node_b['id']]
    placed_b = [e for e in exec_events_b if e['result'] == 'placed' and 'reason=TP' in (e['detail'] or '')]
    checks.append(Check("leg 3: automated_exit_execution fired 'placed' for reason=TP",
                        len(placed_b) == 1, f"events={[(e['result'], e['detail']) for e in exec_events_b]}"))

    pos_b_after = db.get_open_position_by_wl_id(node_b['id'])
    checks.append(Check("leg 3: position auto-closed -- confirmed via the real fill poll, no manual "
                        "Slack tap needed",
                        pos_b_after is None, f"pos_b_after={pos_b_after}"))

    trades_b = db.get_trade_log_for_wl_id(node_b['id'])
    closed_b = [t for t in trades_b if t.get('exit_reason') == 'TP']
    checks.append(Check("leg 3: trade_log's closed row has exit_reason='TP'",
                        len(closed_b) == 1, f"trades={[(t.get('id'), t.get('exit_reason')) for t in trades_b]}"))

    fallback_b = [e for e in db.get_coverage_events(scenario_key='manual_sl_fallback_alert')
                 if e['node_id'] == node_b['id']]
    checks.append(Check("leg 3: no manual_sl_fallback_alert fired -- nothing failed",
                        fallback_b == []))

    observations['node_a_wl_id'] = node_a['id']
    observations['node_b_wl_id'] = node_b['id']
    observations['price'] = price
    observations['shares'] = shares
    observations['order_id_1'] = order_id_1
    observations['order_id_2'] = order_id_2
    observations['posted'] = posted
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT wl.id AS wl_id, wl.version,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='automated_exit_execution'
         AND result='placed' AND node_id=wl.id) AS exit_placed,
       -- trade_log gets a row at OPEN time too (exit_reason NULL until close) --
       -- scoped to exit_reason IS NOT NULL so this only counts genuinely CLOSED
       -- trades, not every node's routine open-time row.
       (SELECT COUNT(*) FROM trade_log WHERE wl_id=wl.id AND exit_reason IS NOT NULL) AS closed_trades,
       (SELECT COUNT(*) FROM open_positions WHERE wl_id=wl.id) AS still_open
  FROM watch_list wl
 WHERE wl.ticker = ?
 ORDER BY wl.id
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly 2 nodes (A, B): node A with
    exactly one 'placed' automated_exit_execution (leg 1's SL placement; leg
    2's reuse deliberately does NOT add a second one), zero CLOSED trades,
    and still open (leg 1/2 never confirm a fill), node B with exactly one
    'placed' automated_exit_execution, one closed trade, and zero
    still-open positions (leg 3's real confirm+close) -- directly from the
    harness DB."""
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
    ok = (node_a['exit_placed'] == 1 and node_a['closed_trades'] == 0 and node_a['still_open'] == 1
          and node_b['exit_placed'] == 1 and node_b['closed_trades'] == 1 and node_b['still_open'] == 0)
    return ok, rows
