"""Phase 2 scenario: `canary_early_sl` (real canary letter B, QQQ/QID in
production -- scripts/coverage_registry.py's `canary_early_sl` row, CLAUDE.md's
"Canary A-F design restored" section), the shorter sibling of
`canary_full_lifecycle`: entry -> bounce-fill -> immediate same-day SL, no
arm stage at all (arm=10% is practically unreachable on the real node, exactly
mirrored here via ARM_PCT=10 -- the real config's whole point is that the
first threshold ever crossed is fixed_sl, hair-trigger at 0.1%).

Same boundary as canary_full_lifecycle and every other fake_venue scenario:
the SL trigger CONDITION is seeded (real bar-close signal computation needs
real hourly OHLC history this synthetic ticker doesn't have), the EXECUTION
MECHANISM that fires once that condition is true is driven for real.

  Stage 1 (entry fill) -- identical shape to canary_full_lifecycle's stage 1:
    node seeded with a resting BUY order -> broker fills it ->
    notify.check_auto_fills() -> _reconcile_buy_fill opens the position and
    (ticker in AUTOMATION_ENABLED_TICKERS) places a real protective STOP.
    => coverage_events['buy_fill_reconciled'] = 'opened'
    => coverage_events['sl_placement'] = 'placed'

  Stage 2 (immediate SL, no arm) -- the real short-circuit this canary is
    designed to exercise: `notify.notify_sell_signal(pos, 'SL', cp, target)`
    is called DIRECTLY off the entry-stage position, with no
    notify_trailing_activated call in between (mirrors active_signals.py:
    `if just_activated_trailing: notify_trailing_activated(...); if reason:
    notify_sell_signal(...)` -- for a genuine SL, just_activated_trailing is
    always False, so only the second branch fires). `_attempt_automated_exit_
    sell` resolves this to `pos['sl_order_id']` (the entry-time STOP from
    stage 1 -- there is no trailing-sell to prefer, since arm never
    happened) and atomically REPLACES it with a real MARKET SELL
    (`schwab_client.replace_equity_order_with_market`) -- FakeBroker fills a
    MARKET order immediately, same real-market-fill behavior every sibling
    scenario relies on. `notify_sell_signal`'s own short bounded poll then
    confirms that fill by exact order_id and closes the position for real
    (`db.close_position(..., exit_reason='SL')`) -- no manual Slack tap.
    => coverage_events['automated_exit_execution'] = 'placed'
    => coverage_events['automated_exit_confirmed'] = 'closed'
    => trade_log row written with exit_reason='SL', a real closed trade

Distinct real-world hazard this specifically proves that canary_full_lifecycle
does NOT: `_attempt_automated_exit_sell`'s `resting_order_id = pos.get('sl_order_id')`
branch (the plain STOP-was-never-replaced path) and the atomic
replace-STOP-with-MARKET call, neither of which canary_full_lifecycle's
arm-first flow ever reaches (that scenario's exit always resolves to the
arm-time TRAILING_STOP order via a different branch of the same function).
"""
from dataclasses import dataclass
from datetime import datetime

from fake_venue import venue
from fake_venue.scenarios_meta import CASH_ACCOUNT_NUMBER, CASH_ALIAS, PRICE_SOURCE_TICKER, TICKER

# Real elapsed time is never actually close: real production always has
# >=POLL_SECS(300s) between an entry-time SL placement and any later
# SELL-side replace (see venue.age_recent_order_records's docstring). This
# scenario drives both stages back-to-back in one process for determinism/
# speed, which would otherwise trip schwab_safety's 60s dup-order window as
# a pure harness-compression artifact, not a real reachable production block.
_DUP_WINDOW_AGE_SECS = 70

FAKE_ACCOUNTS = [
    dict(alias=CASH_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='cash', margin_capable=0),
]
NODE_NOTIONAL = 2_000
# Matches the real QQQ/QID canary B config exactly (scripts/add_canary_nodes.py):
# arm(take_profit)=10% (practically unreachable), fixed_sl=0.1% hair-trigger.
FIXED_SL_PCT = 0.1
ARM_PCT = 10.0


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

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version='fake_venue_canary_b',
                window=5, take_profit=ARM_PCT, stop_loss=0, max_hold_hours=48,
                state='live', account=CASH_ALIAS, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=0.1, trail_pct=0.1, fixed_sl_override=FIXED_SL_PCT,
                entry_timing='close', label='fake-venue harness node (canary_early_sl)')
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
    broker = venue.install_fake_broker([CASH_ALIAS])
    venue.seed_account_number_env({CASH_ALIAS: CASH_ACCOUNT_NUMBER})
    price = venue.seed_quote(broker, TICKER, price, price_source_ticker=PRICE_SOURCE_TICKER)
    broker.set_cash_balance(CASH_ALIAS, 100_000.0)
    say(f"[setup] {TICKER} quote seeded at ${price:.4f}")

    node = _add_node()
    say(f"[setup] node wl_id={node['id']} ({CASH_ALIAS}), fixed_sl={FIXED_SL_PCT}% arm={ARM_PCT}% "
        f"(matches real QQQ/QID canary B config)")

    schwab_safety.enable_auto_fill_detection(TICKER)
    schwab_safety.enable_node_auto_fill_detection(node['id'])

    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking schwab_safety._is_trading_day "
            "True for this run (see module docstring: orthogonal to the mechanism under test)")
        schwab_safety._is_trading_day = lambda date_str: True

    # 2026-08-19: signals_notify._attempt_automated_exit_sell now gates on
    # _market_session_open_now() (incident #13's market-hours-close guard),
    # clocked off schwab_safety._now() -- orthogonal to what this scenario
    # tests, but a real wall-clock run outside 9:30-16:00 ET would silently
    # short-circuit the exit/replace path this scenario exercises. Pinned to
    # a fixed in-session moment, same orthogonal-override pattern as
    # _is_trading_day above.
    schwab_safety._now = lambda: datetime(2026, 8, 19, 15, 30, 0)

    # ---------------------------------------------------------------- stage 1
    shares = max(int(NODE_NOTIONAL // price), 1)
    sig = {'current_price': price, 'last_bar': datetime.now()}
    order_id = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'BUY',
                                         shares, trail_offset=0.1)
    db.add_pending_buy(node, sig, channel=None, ts=None, order_id=order_id)
    db.mark_pending_buy_placed_by_wl_id(node['id'])

    fill_price = round(price * 0.99, 4)
    broker.force_fill(order_id, fill_price)
    say(f"[stage 1] broker filled entry order {order_id} @ ${fill_price:.4f}; running check_auto_fills()")
    notify.check_auto_fills([])

    pos_after_entry = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("position opened for the entry fill", pos_after_entry is not None,
                        f"shares={pos_after_entry['shares'] if pos_after_entry else None}"))
    checks.append(Check("entry order's pending row cleared",
                        [p for p in db.get_pending_buys() if p['ticker'] == TICKER] == []))
    buy_events = db.get_coverage_events(scenario_key="buy_fill_reconciled")
    checks.append(Check("buy_fill_reconciled fired 'opened'",
                        any(e['result'] == 'opened' and e['node_id'] == node['id'] for e in buy_events),
                        f"results={[(e['result'], e['node_id']) for e in buy_events]}"))

    sl_events = db.get_coverage_events(scenario_key="sl_placement")
    placed_sl = [e for e in sl_events if e['result'] == 'placed' and e['node_id'] == node['id']]
    checks.append(Check("_place_stop_loss_for_position fired 'placed' at entry",
                        len(placed_sl) == 1,
                        f"events={[(e['result'], e['detail']) for e in sl_events]}"))
    entry_sl_order_id = pos_after_entry.get('sl_order_id') if pos_after_entry else None
    checks.append(Check("open_positions.sl_order_id set by the real entry-time SL placement",
                        entry_sl_order_id is not None, f"sl_order_id={entry_sl_order_id}"))

    # ---------------------------------------------------------------- stage 2
    # No arm stage at all -- unlike canary_full_lifecycle, notify_sell_signal
    # is called directly off the entry-stage position, exactly mirroring
    # active_signals.py's real branching (just_activated_trailing is False
    # for a genuine SL, so notify_trailing_activated is never called).
    entry_price = pos_after_entry['entry_price']
    sl_trigger_price = round(entry_price * (1 - FIXED_SL_PCT / 100 - 0.0005), 4)  # crosses the 0.1% SL
    target = round(entry_price * (1 - FIXED_SL_PCT / 100), 4)
    venue.age_recent_order_records(_DUP_WINDOW_AGE_SECS)
    say(f"[stage 2] running the real notify.notify_sell_signal(pos, 'SL', cp=${sl_trigger_price:.4f}, "
        f"target=${target:.4f}) directly off the entry-stage position -- no arm in between")
    orders_before_exit = set(broker.orders)
    notify.notify_sell_signal(pos_after_entry, 'SL', sl_trigger_price, target)

    exit_exec_events = db.get_coverage_events(scenario_key="automated_exit_execution")
    placed_exit = [e for e in exit_exec_events if e['result'] == 'placed' and e['node_id'] == node['id']]
    checks.append(Check("_attempt_automated_exit_sell fired automated_exit_execution='placed' -- the "
                        "real atomic replace of the entry-time STOP with a MARKET SELL (no trailing-sell "
                        "involved -- there was never an arm stage to place one)",
                        len(placed_exit) == 1,
                        f"events={[(e['result'], e['detail']) for e in exit_exec_events]}"))

    new_orders = set(broker.orders) - orders_before_exit
    market_sell_orders = [oid for oid in new_orders
                          if broker.orders[oid]['orderType'] == 'MARKET'
                          and broker.orders[oid]['orderLegCollection'][0]['instruction'] == 'SELL']
    checks.append(Check("exactly one new broker MARKET SELL order placed for the SL exit",
                        len(market_sell_orders) == 1,
                        f"new_orders={sorted(new_orders)} market_sell_orders={market_sell_orders}"))
    exit_order = broker.orders[market_sell_orders[0]] if market_sell_orders else None
    if exit_order is not None:
        checks.append(Check("MARKET SELL order was already FILLED at the broker (real market-order "
                            "fill behavior -- what notify_sell_signal's own inline poll confirms)",
                            exit_order['status'] == 'FILLED', f"status={exit_order['status']}"))
        checks.append(Check("MARKET SELL order is for the full entry share count",
                            exit_order['orderLegCollection'][0]['quantity'] == shares,
                            f"quantity={exit_order['orderLegCollection'][0]['quantity']} expected={shares}"))

    original_stop_order = broker.orders.get(entry_sl_order_id) if entry_sl_order_id else None
    checks.append(Check("the original protective STOP order is no longer WORKING at the broker "
                        "(atomic replace canceled it, not a separate cancel_order call)",
                        original_stop_order is not None and original_stop_order['status'] != 'WORKING',
                        f"status={original_stop_order['status'] if original_stop_order else None}"))

    exit_confirmed_events = db.get_coverage_events(scenario_key="automated_exit_confirmed")
    closed_events = [e for e in exit_confirmed_events if e['result'] == 'closed' and e['node_id'] == node['id']]
    checks.append(Check("automated_exit_confirmed fired 'closed' -- the real fill-confirmed auto-close, "
                        "no manual Slack tap anywhere in this chain",
                        len(closed_events) == 1,
                        f"events={[(e['result'], e['detail']) for e in exit_confirmed_events]}"))
    checks.append(Check("position no longer open", db.get_open_position_by_wl_id(node['id']) is None))

    trades = db.get_closed_trades_for_ticker_on_date(TICKER, datetime.now().strftime('%Y-%m-%d'),
                                                       wl_id=node['id'])
    checks.append(Check("trade_log has exactly one closed trade for this node today, exit_reason='SL' "
                        "-- exactly what the real daily canary's coverage_check.py trade_lifecycle "
                        "expectation checks for (canary_early_sl, expect_exit_reason=['SL'])",
                        len(trades) == 1 and trades[0]['exit_reason'] == 'SL',
                        f"trades={[(t.get('exit_reason'), t.get('exit_price')) for t in trades]}"))

    observations['node_wl_id'] = node['id']
    observations['price'] = price
    observations['shares'] = shares
    observations['entry_sl_order_id'] = entry_sl_order_id
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT tl.wl_id, tl.exit_reason, tl.entry_price, tl.exit_price, wl.account, wl.state,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='sl_placement' AND result='placed'
         AND node_id=tl.wl_id) AS sl_placed_events,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='automated_exit_execution'
         AND result='placed' AND node_id=tl.wl_id) AS exit_placed_events,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='automated_exit_confirmed'
         AND result='closed' AND node_id=tl.wl_id) AS exit_confirmed_events
  FROM trade_log tl
  JOIN watch_list wl ON wl.id = tl.wl_id
 WHERE tl.ticker = ?
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly one closed trade with
    exit_reason='SL', a real entry-time SL placement event, a real exit-place
    event, and a real exit-confirm event, all attached to the same node --
    directly from the harness DB."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['exit_reason'] == 'SL' and rows[0]['sl_placed_events'] == 1
          and rows[0]['exit_placed_events'] == 1 and rows[0]['exit_confirmed_events'] == 1
          and rows[0]['state'] == 'live')
    return ok, rows
