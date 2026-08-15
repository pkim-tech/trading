"""Phase 2 scenario: `post_fill_topup`, end to end through TWO real broker orders.

`_reconcile_fill` (signals_notify.py:4116) is the real code that fires whenever
a real fill comes in under target_notional (the conservative worst-case pad in
buy_order_sizing/check_gap_resize means this is the common case, not the rare
one -- real live proof exists, RETL/ERY 2026-08-10, see CLAUDE.md). It places a
genuine SECOND broker order inline (schwab_client.place_equity_buy for the
top-up shares) right after the original entry fill reconciles. What's never
been tested: whether THAT second order's own fill correctly resolves through
the real stream/poll path -- the exact AccountNumber/SchwabOrderID bug class
Phase 1's scenario found once already (see fake_venue/scenarios.py's FIXED
note), now on a SECOND order for the same ticker+account, placed moments after
the first.

Shape (one fake node, one fake account -- this scenario isn't about ticker/
account disambiguation, Phase 1 already covers that; it's about a second
order's own fill reconciling correctly):

  node A  (fv_cash)  resting BUY order, deliberately SIZED SHORT of what
                      target_notional (== starting_notional, no trade_log
                      history yet -- signals_helpers._last_sale_recovery falls
                      back to it directly) would buy in full.

  Leg 1  broker fills A's (short-sized) order IN FULL
         -> notify.check_auto_fills()                  [real slow-poll path,
                                                          matches Phase 1 leg 1]
         -> _reconcile_buy_fill -> position opens at the short share count
         -> _reconcile_fill computes delta = target_notional - fill_notional
            > fill_price -> TOP-UP BRANCH fires
         -> schwab_client.place_equity_buy(..., is_protective=True) places a
            REAL second MARKET order (FakeBroker fills it immediately, same as
            a real market order would in production) for top_up_shares
         => coverage_events['top_up'] = 'placed'                <-- checked
         => open_positions.shares already reflects original+top_up (top_up_
            position blends synchronously -- _reconcile_fill does NOT wait for
            the top-up order's own fill to land via the stream/poll path
            before recording it; see FOUND/FIXED note below for what it DOES
            now do before recording it)

  Leg 2  the TOP-UP ORDER'S OWN fill, driven through the REAL STREAM path --
         a fake ACCT_ACTIVITY message (real-shaped: raw AccountNumber, string
         SchwabOrderID) carrying the top-up order's own id.
         -> schwab_stream._handle_activity_message()   [real parser]
         -> signals_notify.drain_fill_queue()           [real fast path]

         THE ACTUAL QUESTION THIS SCENARIO ANSWERS: _reconcile_fill never
         created a pending_buys row for the top-up order (it isn't a
         "pending buy" being waited on -- it's placed and, as of the fix
         below, price-confirmed inline, not tracked as a resumable pending
         entry). So when the top-up's own fill arrives via the stream,
         drain_fill_queue finds NO matching pending_buys row for it
         (db.get_pending_buys() has nothing left for this ticker at all --
         leg 1 already cleared node A's only row) and _node_id is None. That
         routes it into the ORPHANED-FILL alert branch, not a second
         reconciliation -- BY DESIGN, since the position was already updated
         by leg 1. What this scenario had to actually prove, not assume: that
         the orphan-fill branch's own get_filled_order(order_id=...) call
         resolves the CORRECT order (this account+ticker now has TWO FILLED
         orders on file, original entry + top-up -- an unparseable/omitted
         order_id would fall back to get_filled_order's documented
         "most-recent-FILLED" heuristic and could silently match either one)
         and reports the top-up's own real price/quantity, not the
         original's. Confirmed working (AccountNumber/order-id resolution
         holds for a SECOND order on the same ticker+account, not just two
         different nodes' first orders as Phase 1 proved).

         FOUND AND FIXED, 2026-08-16 (signals_notify._reconcile_fill): this
         scenario's first run caught a real, separate gap on the way to the
         above -- _reconcile_fill recorded the top-up leg's blended
         entry_price using `fill_price` (the ORIGINAL entry fill's price,
         passed straight through), not the top-up order's own actual
         execution price. place_equity_buy's `price` argument is documented
         as "used only for the safety-cap notional check ... not sent to the
         API" -- a real MARKET order fills at whatever the live quote
         actually is at that instant, which can differ from the original
         fill (the two orders are placed seconds apart; in production this
         is usually a small drift, not the 1% this scenario deliberately
         used to make it observable). Since no pending_buys row is ever
         created for the top-up, nothing downstream could ever correct this
         -- the orphan-fill alert above is a Slack notification, not a DB
         write. Fixed: _reconcile_fill now does a short synchronous poll
         (get_filled_order, same _GAP_FILL_POLL_ATTEMPTS/_INTERVAL_SECS
         pattern drain_fill_queue already uses) for the top-up order's own
         CONFIRMED fill price before calling db.top_up_position, falling
         back to the fill_price approximation (previous, sole behavior) if
         the poll doesn't confirm in time or raises for any reason -- fails
         open exactly as before, no new blocking risk on the real order path.
         entry_price feeds real SL/trailing-stop trigger percentages
         downstream, so this was a real (if usually small) live-money
         correctness gap, not cosmetic bookkeeping.

         => coverage_events['orphaned_fill_detected'] = 'alerted', with
            quantity/price matching the TOP-UP order specifically (not the
            original fill)                                       <-- checked
         => coverage_events['fast_path_fill_reconciliation'] =
            'auto_fill_detection_disabled' (node_auto_fill_detection_enabled
            (None) is a hard False -- schwab_safety.py:636 -- so no second,
            competing reconciliation is even attempted)          <-- checked
         => open_positions.shares UNCHANGED by leg 2 (already correct from
            leg 1's synchronous top_up_position call) -- no double-count
                                                                   <-- checked

Design note on the sizing mechanism (docs/design.md's 2026-08-15 entry left
this as an implementer's call): sized the ORIGINAL order's own share count
short of target_notional, rather than using activity_stream's leaves_quantity
partial-execution shape. leaves_quantity models Schwab reporting one partial
execution of a STILL-OPEN order (drain_fill_queue's docstring: "never trust
the stream's own price/quantity, it might be one leg of a still-filling
order") -- a different real hazard (premature top-up while the original order
is still resting) from this scenario's target (a FULLY, TERMINALLY filled
order that simply bought fewer shares than target_notional called for, the
overwhelmingly common real case per buy_order_sizing's conservative-padding
design and the real RETL/ERY proof). Using leaves_quantity here would
conflate the two.
"""
from dataclasses import dataclass
from datetime import datetime

from fake_venue import activity_stream, venue
from fake_venue.scenarios_meta import CASH_ACCOUNT_NUMBER, CASH_ALIAS, PRICE_SOURCE_TICKER, TICKER

FAKE_ACCOUNTS = [
    dict(alias=CASH_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='cash', margin_capable=0),
]
# Chosen so a deliberate 3-share shortfall (SHORT_SHARES below) still clears
# _reconcile_fill's `delta > fill_price` top-up gate with room to spare, at a
# realistic real-ETF price range.
NODE_NOTIONAL = 2_000
SHORT_SHARES = 3


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

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version='fake_venue_topup',
                window=20, take_profit=10, stop_loss=1, max_hold_hours=56,
                state='live', account=CASH_ALIAS, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                label='fake-venue harness node (post_fill_topup)')
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
    say(f"[setup] node wl_id={node['id']} ({CASH_ALIAS}), target_notional=${NODE_NOTIONAL}")

    schwab_safety.enable_auto_fill_detection(TICKER)
    schwab_safety.enable_node_auto_fill_detection(node['id'])

    # check_order's trading-day gate (schwab_safety.py ~1652) is BUY-only and
    # UNCONDITIONAL -- deliberately not exempted even for is_protective
    # top-ups (see that gate's own comment: "no legitimate reason to exempt
    # it the way the [signal-]window gate below does"). Real and correct in
    # production. But this scenario's actual target is the SECOND real BUY
    # order's own reconciliation, not the calendar gate (already covered by
    # schwab_safety's own weekday/holiday tests) -- and the harness must be
    # runnable deterministically any day, including weekends, without
    # silently degrading into "prove nothing happened because it's Saturday."
    # Faked here the same way price/accounts are faked: an environmental fact
    # orthogonal to the mechanism under test, not a bypass of the mechanism
    # itself. real_trading_day recorded in observations so a run against a
    # genuine trading day is visibly not relying on this override.
    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking schwab_safety._is_trading_day "
            "True for this run (see module docstring: orthogonal to the mechanism under test)")
        schwab_safety._is_trading_day = lambda date_str: True

    # Entry-side state is SEEDED, not placed through the real BUY path -- same
    # accepted caveat as Phase 1 (this scenario's target is the reconciliation
    # chain, not entry placement). Deliberately short of full target_notional
    # sizing -- see module docstring for why this (not activity_stream's
    # leaves_quantity) is the faithful way to trigger a real top-up.
    full_shares = max(int(NODE_NOTIONAL // price), 1)
    order_shares = max(full_shares - SHORT_SHARES, 1)
    sig = {'current_price': price, 'last_bar': datetime.now()}
    order_id = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'BUY',
                                         order_shares, trail_offset=1.0)
    db.add_pending_buy(node, sig, channel=None, ts=None, order_id=order_id)
    db.mark_pending_buy_placed_by_wl_id(node['id'])
    checks.append(Check("original order sized short of target_notional",
                        order_shares < full_shares,
                        f"order_shares={order_shares} full_shares={full_shares} "
                        f"(target=${NODE_NOTIONAL} @ ${price:.4f})"))

    # ---------------------------------------------------------------- leg 1
    fill_price = round(price * 0.99, 4)
    broker.force_fill(order_id, fill_price)
    say(f"[leg 1] broker filled node's order {order_id} @ ${fill_price:.4f} for {order_shares} shares "
        f"(full order, deliberately short of target); running check_auto_fills()")
    orders_before_topup = set(broker.orders)
    notify.check_auto_fills([])

    pos = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("position opened for the original (short) fill", pos is not None,
                        f"shares={pos['shares'] if pos else None} entry={pos['entry_price'] if pos else None}"))
    checks.append(Check("original order's pending row cleared",
                        [p for p in db.get_pending_buys() if p['ticker'] == TICKER] == []))

    buy_events = db.get_coverage_events(scenario_key="buy_fill_reconciled")
    checks.append(Check("buy_fill_reconciled fired 'opened' for the original fill",
                        any(e['result'] == 'opened' and e['node_id'] == node['id'] for e in buy_events),
                        f"results={[(e['result'], e['node_id']) for e in buy_events]}"))

    topup_events = db.get_coverage_events(scenario_key="top_up")
    placed = [e for e in topup_events if e['result'] == 'placed' and e['node_id'] == node['id']]
    checks.append(Check("_reconcile_fill's top-up branch fired 'placed' "
                        "(delta > fill_price gate crossed)",
                        len(placed) == 1,
                        f"events={[(e['result'], e['detail']) for e in topup_events]}"))
    top_up_shares = None
    if placed:
        # detail is "shares=<N> price=<P>" -- parsed rather than hardcoded so
        # this check stays honest if the delta math above ever shifts.
        try:
            top_up_shares = int(placed[0]['detail'].split('shares=')[1].split(' ')[0])
        except (IndexError, ValueError):
            pass
    checks.append(Check("top-up shares parsed from the coverage event", top_up_shares is not None,
                        f"detail={placed[0]['detail'] if placed else None}"))

    # check_auto_fills' single call also triggers _place_stop_loss_for_position
    # (ticker is in AUTOMATION_ENABLED_TICKERS) -- so orders_before_topup's
    # complement can legitimately contain TWO new orders: the protective STOP
    # and the top-up MARKET BUY. Disambiguated by orderType (STOP vs MARKET),
    # not order count, so this check stays correct regardless of that
    # placement ordering.
    new_orders = set(broker.orders) - orders_before_topup
    market_buy_orders = [oid for oid in new_orders
                         if broker.orders[oid]['orderType'] == 'MARKET'
                         and broker.orders[oid]['orderLegCollection'][0]['instruction'] == 'BUY']
    topup_order_id = market_buy_orders[0] if len(market_buy_orders) == 1 else None
    topup_order = broker.orders[topup_order_id] if topup_order_id is not None else None
    checks.append(Check("real second broker order placed for the top-up (schwab_client.place_equity_buy)",
                        len(market_buy_orders) == 1,
                        f"new_orders={sorted(new_orders)} market_buy_orders={market_buy_orders} "
                        f"(expected exactly 1 new MARKET BUY order)"))
    if topup_order is not None:
        leg = topup_order['orderLegCollection'][0]
        checks.append(Check("top-up order is a MARKET BUY for the expected share count",
                            topup_order['orderType'] == 'MARKET' and leg['instruction'] == 'BUY'
                            and leg['quantity'] == top_up_shares,
                            f"orderType={topup_order['orderType']} instruction={leg['instruction']} "
                            f"quantity={leg['quantity']} expected={top_up_shares}"))
        checks.append(Check("top-up order is already FILLED at the broker "
                            "(a real MARKET order fills immediately -- its OWN fill event is what "
                            "leg 2 drives through the stream, mirroring real same-tick fill + "
                            "async ACCT_ACTIVITY delivery)",
                            topup_order['status'] == 'FILLED',
                            f"status={topup_order['status']}"))

    pos_after_leg1 = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("open_positions.shares already reflects original + top-up "
                        "(synchronous db.top_up_position, no wait for the top-up's own fill confirmation)",
                        pos_after_leg1 is not None and top_up_shares is not None
                        and pos_after_leg1['shares'] == order_shares + top_up_shares,
                        f"shares={pos_after_leg1['shares'] if pos_after_leg1 else None} "
                        f"expected={(order_shares + top_up_shares) if top_up_shares else None}"))
    shares_after_leg1 = pos_after_leg1['shares'] if pos_after_leg1 else None

    # ---------------------------------------------------------------- leg 2
    # The top-up's OWN fill, driven through the real stream path. No
    # pending_buys row exists for this order (leg 1 already cleared the
    # node's only row) -- the real question is whether the orphan-fill
    # branch resolves THIS order (not the original) via order_id-exact
    # get_filled_order, using the real-shaped AccountNumber/SchwabOrderID
    # fixes Phase 1 put in place, now exercised against an account that has
    # TWO filled orders on file for this ticker.
    # FakeBroker fills a MARKET order at the CURRENT quote (self.quotes[...]['lastPrice']),
    # not at the `price` argument passed to place_equity_buy (that's documented as
    # "used only for the safety-cap notional check ... not sent to the API") --
    # so the top-up's real fill is at `price` (the seeded quote), not `fill_price`
    # (leg 1's discounted force_fill price). Confirmed by the real production fix
    # this scenario's first run motivated (see signals_notify._reconcile_fill's
    # 2026-08-16 docstring addendum): _reconcile_fill now polls the top-up's own
    # order for its CONFIRMED price before recording it, rather than assuming
    # fill_price -- this scenario's own leg 2 fill price must match that same
    # real broker execution price for the "correct price/quantity" check below to
    # mean anything.
    topup_fill_price = price
    say(f"[leg 2] emitting a real-shaped ACCT_ACTIVITY fill message for the top-up's own order "
        f"{topup_order_id} ({top_up_shares} shares @ ${topup_fill_price:.4f})")
    activity_stream.emit_fill(CASH_ACCOUNT_NUMBER, topup_order_id, TICKER, 'BUY',
                              topup_fill_price, top_up_shares)

    queued = activity_stream.queued_events()
    expected_tuple = (CASH_ACCOUNT_NUMBER, TICKER, 'BUY', topup_fill_price, float(top_up_shares),
                      str(topup_order_id))
    checks.append(Check("real parser decoded the top-up's own fill envelope correctly",
                        queued == [expected_tuple],
                        f"queued={queued} expected={[expected_tuple]}"))

    try:
        notify.drain_fill_queue()
        observations['drain_topup_fill'] = 'completed without raising'
    except Exception as e:
        observations['drain_topup_fill'] = f"{type(e).__name__}: {e}"
    say(f"[leg 2] drain_fill_queue() for the top-up's own fill -> {observations['drain_topup_fill']}")
    checks.append(Check("drain_fill_queue() did not raise reconciling a second order for the same ticker",
                        observations['drain_topup_fill'] == 'completed without raising'))

    orphan = db.get_coverage_events(scenario_key="orphaned_fill_detected")
    topup_orphan = [e for e in orphan if e['result'] == 'alerted' and e['ticker'] == TICKER
                    and f"order_id={topup_order_id}" in (e['detail'] or '')]
    checks.append(Check("the top-up's own fill correctly landed in the orphan-fill alert branch "
                        "(no pending_buys row -- by design, _reconcile_fill already recorded it "
                        "synchronously), tagged to the TOP-UP order's id specifically",
                        len(topup_orphan) == 1,
                        f"events={[(e['result'], e['detail']) for e in orphan]}"))
    if topup_orphan:
        checks.append(Check("orphan-fill alert reports the TOP-UP order's own price/quantity, "
                            "not the original fill's (order_id-exact get_filled_order resolved the "
                            "right one of two FILLED orders on file for this ticker)",
                            f"price={topup_fill_price:.4f}" in topup_orphan[0]['detail']
                            and f"shares={top_up_shares:g}" in topup_orphan[0]['detail'],
                            f"detail={topup_orphan[0]['detail']}"))

    fast = db.get_coverage_events(scenario_key="fast_path_fill_reconciliation")
    disabled_for_topup = [e for e in fast if e['result'] == 'auto_fill_detection_disabled'
                          and e['node_id'] is None and e['ticker'] == TICKER]
    checks.append(Check("no competing reconciliation attempted for the top-up's own fill "
                        "(node_auto_fill_detection_enabled(None) is a hard False -- no pending row "
                        "means no node identity to opt in)",
                        len(disabled_for_topup) >= 1,
                        f"fast_path events={[(e['result'], e['node_id']) for e in fast]}"))

    pos_after_leg2 = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("open_positions.shares unchanged by leg 2 -- no double-count from the "
                        "top-up's own fill being processed a second time",
                        pos_after_leg2 is not None and pos_after_leg2['shares'] == shares_after_leg1,
                        f"before_leg2={shares_after_leg1} after_leg2={pos_after_leg2['shares'] if pos_after_leg2 else None}"))

    observations['node_wl_id'] = node['id']
    observations['price'] = price
    observations['order_shares'] = order_shares
    observations['top_up_shares'] = top_up_shares
    observations['final_shares'] = pos_after_leg2['shares'] if pos_after_leg2 else None
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT op.wl_id, op.shares AS final_shares, wl.account, wl.state, wl.starting_notional,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='top_up' AND result='placed'
         AND node_id=op.wl_id) AS top_up_placed_events,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='orphaned_fill_detected'
         AND result='alerted' AND ticker=wl.ticker) AS orphan_fill_alerts
  FROM open_positions op
  JOIN watch_list wl ON wl.id = op.wl_id
 WHERE wl.ticker = ?
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly one open position, with exactly
    one 'placed' top_up event and exactly one 'alerted' orphaned-fill event
    (the top-up's own fill), directly from the harness DB."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['top_up_placed_events'] == 1
          and rows[0]['orphan_fill_alerts'] == 1 and rows[0]['state'] == 'live')
    return ok, rows
