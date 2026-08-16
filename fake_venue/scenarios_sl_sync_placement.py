"""Phase 2 scenario: `sl_sync_placement`, proving `_place_stop_loss_for_position`
is correctly reached via the REAL STREAM chain, not just the sync/market-buy
callers `scripts/coverage_registry.py`'s `sl_sync_placement` Grid row's own
`code_path` names.

Found 2026-08-16 via a mechanical (not prose-based) re-verification of the
harness's Category A stream-exposure queue: the Grid row's `code_path`
("_place_stop_loss_for_position via sync confirm") only documents the manual
"Filled" Slack-button callers (`signals_handlers.py:226`/`:367`). But
`_place_stop_loss_for_position` is ALSO called directly from
`signals_notify._reconcile_buy_fill` (signals_notify.py ~4430:
`if ticker in schwab_safety.AUTOMATION_ENABLED_TICKERS:
_place_stop_loss_for_position(node, ticker)`), and `_reconcile_buy_fill` is
the SHARED entry point for FOUR distinct callers: `check_auto_fills` (slow
poll), `check_gap_resize` (pre-market gap poll), `_sync_confirm_and_protect`
(the actual sync-confirm path the Grid row's `code_path` text describes), and
`drain_fill_queue` -- the real websocket/ACCT_ACTIVITY STREAM path. All four
emit the identical `scenario_key='sl_placement'` event the Grid row checks,
so nothing here was going unproven by the Grid's own bookkeeping -- but
`sibling scenario `post_fill_topup` (fake_venue/scenarios_post_fill_topup.py)
only ever reconciles its ORIGINAL entry fill via `check_auto_fills` (the
poll); its own leg 2 (the top-up order's own fill) DOES go through
`drain_fill_queue`, but lands in the orphan-fill alert branch (no pending_buys
row exists for a top-up order), which never reaches the `_place_stop_loss_
for_position` call at all. So the drain_fill_queue -> _reconcile_buy_fill ->
_place_stop_loss_for_position chain specifically had never actually been
driven end to end by any existing scenario or test before this one --
structurally the same code as the already-proven poll path (same function
body, `_reconcile_buy_fill` doesn't branch on caller), so this isn't
expected to surface a NEW bug the way `post_fill_topup`'s first run did --
but "structurally the same, never actually run" is exactly the gap class
this harness exists to close (automation_principles.md #1: reconfirm real
state, don't trust a structural argument alone).

`_place_stop_loss_for_position`'s own docstring (signals_notify.py:1212) is
directly relevant here: it "[reads] the final share count back off
open_positions (post any top-up _reconcile_fill already applied for
market-buy fills) so the stop covers the whole position, not just a
provisional quantity." `_reconcile_buy_fill` calls `_reconcile_fill` (which
may top up) BEFORE calling `_place_stop_loss_for_position` -- true regardless
of which of the 4 callers triggered `_reconcile_buy_fill`, since it's the
same function body. This scenario deliberately reuses `post_fill_topup`'s
short-sized-order shape (see that module's docstring for why a short original
order, not `activity_stream`'s `leaves_quantity`, is the faithful way to
trigger a real top-up) so the stop this scenario proves gets placed for the
FULL post-top-up share count -- specifically when the ENTIRE chain (original
entry fill -> top-up -> stop placement) is driven by the stream path, not the
poll path `post_fill_topup` already covers. This is the one combination nothing
else exercises: stream-detected entry + real top-up + stop sized off the
blended, post-top-up position.

Shape (one fake node, one fake account):

  Setup   node seeded with a resting BUY order deliberately SIZED SHORT of
          target_notional (same SHORT_SHARES mechanism as post_fill_topup).

  Leg 1   broker fills the (short-sized) order IN FULL. Unlike post_fill_topup,
          this is NEVER run through `check_auto_fills` -- the fill is only
          ever surfaced via a real-shaped ACCT_ACTIVITY message through
          `activity_stream.emit_fill` + `signals_notify.drain_fill_queue()`,
          the real fast-path/stream chain.
          -> drain_fill_queue's real order_id-exact resolution, opt-in gate,
             and get_filled_order re-confirmation (never trusting the
             stream's own price/quantity -- see that function's own
             docstring) all run for real.
          -> _reconcile_buy_fill opens the position at the short share count,
             then _reconcile_fill's `delta > fill_price` gate fires the
             TOP-UP branch: a real second broker MARKET order for the
             shortfall, filled immediately (FakeBroker's real market-fill
             behavior).
          -> back in _reconcile_buy_fill, ticker is in AUTOMATION_ENABLED_
             TICKERS, so _place_stop_loss_for_position(node, ticker) runs --
             THE TARGET of this scenario -- reading open_positions AFTER the
             synchronous top-up blend above, sizing the real broker STOP for
             the FULL (original + top-up) share count, anchored to the real
             blended entry_price.
          => coverage_events['fast_path_fill_reconciliation'] =
             'confirmed_via_poll'   <-- proves this ran via drain_fill_queue,
                                        not check_auto_fills (which never
                                        logs this scenario_key at all)
          => coverage_events['buy_fill_reconciled'] = 'opened'
          => coverage_events['top_up'] = 'placed'
          => coverage_events['sl_placement'] = 'placed'          <-- TARGET
          => open_positions.sl_order_id set, a genuine broker STOP order
             exists sized for (original + top-up) shares, anchored to the
             real blended entry_price * (1 - fixed_sl%)

  Leg 2   deliberately NOT exercised here -- the top-up order's own fill
          landing in the orphan-fill alert branch (no pending_buys row for a
          top-up order) is already proven end to end by post_fill_topup's
          leg 2, and adding it here would just re-run that same proof under a
          different scenario name (feedback_isolate_new_code_from_settled_
          paths.md's spirit: don't re-verify a settled path, extend past it).
"""
from dataclasses import dataclass
from datetime import datetime

from fake_venue import activity_stream, venue
from fake_venue.scenarios_meta import CASH_ACCOUNT_NUMBER, CASH_ALIAS, PRICE_SOURCE_TICKER, TICKER

FAKE_ACCOUNTS = [
    dict(alias=CASH_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='cash', margin_capable=0),
]
# Same sizing rationale as post_fill_topup: a deliberate shortfall that still
# clears _reconcile_fill's `delta > fill_price` top-up gate with room to
# spare, at a realistic real-ETF price range.
NODE_NOTIONAL = 2_000
SHORT_SHARES = 3
# A round, non-hair-trigger SL -- this scenario isn't reproducing a specific
# incident's exact config (unlike sl_order_fills_independent_detection's
# LABD-matching fixed_sl=0.3%); it only needs a real, checkable stop price.
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

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version='fake_venue_sl_stream',
                window=20, take_profit=10, stop_loss=1, max_hold_hours=56,
                state='live', account=CASH_ALIAS, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=FIXED_SL_PCT,
                label='fake-venue harness node (sl_sync_placement)')
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
    say(f"[setup] node wl_id={node['id']} ({CASH_ALIAS}), target_notional=${NODE_NOTIONAL}, "
        f"fixed_sl={FIXED_SL_PCT}%")

    schwab_safety.enable_auto_fill_detection(TICKER)
    schwab_safety.enable_node_auto_fill_detection(node['id'])

    # Same rationale as post_fill_topup's module docstring/setup comment: the
    # calendar/trading-day gate is orthogonal to the mechanism under test
    # (this scenario's target is the stream fill-reconciliation -> stop-loss
    # placement chain, not schwab_safety's own weekday/holiday tests), and the
    # harness must be runnable deterministically any day.
    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking schwab_safety._is_trading_day "
            "True for this run (see module docstring: orthogonal to the mechanism under test)")
        schwab_safety._is_trading_day = lambda date_str: True

    # Entry-side state is SEEDED, not placed through the real BUY path -- same
    # accepted caveat as the sibling Phase 2 scenarios. Deliberately short of
    # full target_notional sizing -- see module docstring for why this
    # triggers a real top-up before the stop is sized.
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
    # The fill is NEVER run through check_auto_fills (the already-proven poll
    # path) -- reconciliation here happens ONLY via a real-shaped
    # ACCT_ACTIVITY message and the real drain_fill_queue(), so any pass/fail
    # below is genuine evidence about the stream chain specifically.
    fill_price = round(price * 0.99, 4)
    broker.force_fill(order_id, fill_price)
    say(f"[leg 1] broker filled node's order {order_id} @ ${fill_price:.4f} for {order_shares} shares "
        f"(deliberately short of target); emitting a real-shaped ACCT_ACTIVITY fill message")
    activity_stream.emit_fill(CASH_ACCOUNT_NUMBER, order_id, TICKER, 'BUY', fill_price, order_shares)

    queued = activity_stream.queued_events()
    expected_tuple = (CASH_ACCOUNT_NUMBER, TICKER, 'BUY', fill_price, float(order_shares), str(order_id))
    checks.append(Check("real parser decoded the original entry's fill envelope correctly",
                        queued == [expected_tuple],
                        f"queued={queued} expected={[expected_tuple]}"))

    orders_before_topup = set(broker.orders)
    say("[leg 1] running the real signals_notify.drain_fill_queue() -- the stream fast path")
    try:
        notify.drain_fill_queue()
        observations['drain_fill_queue'] = 'completed without raising'
    except Exception as e:
        observations['drain_fill_queue'] = f"{type(e).__name__}: {e}"
    checks.append(Check("drain_fill_queue() did not raise reconciling the stream-detected entry fill",
                        observations['drain_fill_queue'] == 'completed without raising'))

    fast_path_events = db.get_coverage_events(scenario_key="fast_path_fill_reconciliation")
    confirmed_via_stream = [e for e in fast_path_events if e['result'] == 'confirmed_via_poll'
                            and e['ticker'] == TICKER]
    checks.append(Check("fast_path_fill_reconciliation fired 'confirmed_via_poll' -- proves this "
                        "fill was reconciled via drain_fill_queue's stream path, not "
                        "check_auto_fills (which never logs this scenario_key)",
                        len(confirmed_via_stream) == 1,
                        f"events={[(e['result'], e['node_id']) for e in fast_path_events]}"))

    pos = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("position opened for the original (short) fill via the stream path",
                        pos is not None,
                        f"shares={pos['shares'] if pos else None} entry={pos['entry_price'] if pos else None}"))
    checks.append(Check("original order's pending row cleared",
                        [p for p in db.get_pending_buys() if p['ticker'] == TICKER] == []))

    buy_events = db.get_coverage_events(scenario_key="buy_fill_reconciled")
    checks.append(Check("buy_fill_reconciled fired 'opened' for the stream-detected fill",
                        any(e['result'] == 'opened' and e['node_id'] == node['id'] for e in buy_events),
                        f"results={[(e['result'], e['node_id']) for e in buy_events]}"))

    topup_events = db.get_coverage_events(scenario_key="top_up")
    placed_topup = [e for e in topup_events if e['result'] == 'placed' and e['node_id'] == node['id']]
    checks.append(Check("_reconcile_fill's top-up branch fired 'placed' (delta > fill_price gate "
                        "crossed) -- same mechanism as post_fill_topup, now reached via the stream",
                        len(placed_topup) == 1,
                        f"events={[(e['result'], e['detail']) for e in topup_events]}"))
    top_up_shares = None
    if placed_topup:
        # Parsed from the coverage event's own detail string, not hardcoded --
        # same technique as post_fill_topup, so this check stays honest if the
        # delta math ever shifts.
        try:
            top_up_shares = int(placed_topup[0]['detail'].split('shares=')[1].split(' ')[0])
        except (IndexError, ValueError):
            pass
    checks.append(Check("top-up shares parsed from the coverage event", top_up_shares is not None,
                        f"detail={placed_topup[0]['detail'] if placed_topup else None}"))

    pos_final = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("open_positions.shares reflects original + top-up BEFORE the stop-loss "
                        "call below -- this is what _place_stop_loss_for_position's docstring "
                        "means by reading the position back \"post any top-up already applied\"",
                        pos_final is not None and top_up_shares is not None
                        and pos_final['shares'] == order_shares + top_up_shares,
                        f"shares={pos_final['shares'] if pos_final else None} "
                        f"expected={(order_shares + top_up_shares) if top_up_shares else None}"))

    # THE TARGET: _reconcile_buy_fill's own
    # `if ticker in schwab_safety.AUTOMATION_ENABLED_TICKERS: _place_stop_loss_
    # for_position(node, ticker)` call, reached via drain_fill_queue this time,
    # not check_auto_fills (post_fill_topup) or the manual Filled button
    # (sl_order_fills_independent_detection calls _place_stop_loss_for_position
    # directly, bypassing the fill-reconciliation chain entirely).
    sl_events = db.get_coverage_events(scenario_key="sl_placement")
    placed_sl = [e for e in sl_events if e['result'] == 'placed' and e['node_id'] == node['id']]
    checks.append(Check("_place_stop_loss_for_position fired 'placed', reached via "
                        "drain_fill_queue -> _reconcile_buy_fill -- the previously-unrecognized "
                        "stream-exposure path for SL placement",
                        len(placed_sl) == 1,
                        f"events={[(e['result'], e['detail']) for e in sl_events]}"))

    sl_order_id = pos_final.get('sl_order_id') if pos_final else None
    checks.append(Check("open_positions.sl_order_id set by the real placement write "
                        "(db.set_sl_order_id_by_position, not test scaffolding)",
                        sl_order_id is not None,
                        f"sl_order_id={sl_order_id}"))

    new_orders = set(broker.orders) - orders_before_topup
    stop_orders = [oid for oid in new_orders if broker.orders[oid]['orderType'] == 'STOP']
    checks.append(Check("exactly one new broker STOP order placed alongside the top-up's MARKET order",
                        len(stop_orders) == 1,
                        f"new_orders={sorted(new_orders)} stop_orders={stop_orders}"))
    stop_order = broker.orders[stop_orders[0]] if stop_orders else None
    if stop_order is not None:
        leg = stop_order['orderLegCollection'][0]
        checks.append(Check("real broker STOP order id matches open_positions.sl_order_id",
                            stop_orders[0] == sl_order_id,
                            f"stop_order_id={stop_orders[0]} sl_order_id={sl_order_id}"))
        checks.append(Check("STOP order is sized for the FULL post-top-up share count "
                            "(original + top-up), not just the original short-sized fill -- "
                            "the exact claim in _place_stop_loss_for_position's docstring",
                            leg['instruction'] == 'SELL'
                            and top_up_shares is not None
                            and leg['quantity'] == order_shares + top_up_shares,
                            f"instruction={leg['instruction']} quantity={leg['quantity']} "
                            f"expected={(order_shares + top_up_shares) if top_up_shares else None}"))
        expected_stop_price = round(pos_final['entry_price'] * (1 - FIXED_SL_PCT / 100), 4) if pos_final else None
        checks.append(Check("STOP price is anchored to the real blended entry_price * "
                            "(1 - fixed_sl%), matching strategies.py's own SL check",
                            expected_stop_price is not None
                            and abs(stop_order['stopPrice'] - expected_stop_price) < 0.01,
                            f"stop_price={stop_order.get('stopPrice')} expected~={expected_stop_price}"))

    observations['node_wl_id'] = node['id']
    observations['price'] = price
    observations['order_shares'] = order_shares
    observations['top_up_shares'] = top_up_shares
    observations['final_shares'] = pos_final['shares'] if pos_final else None
    observations['sl_order_id'] = sl_order_id
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT op.wl_id, op.shares AS final_shares, op.entry_price, op.sl_order_id, wl.account, wl.state,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='sl_placement' AND result='placed'
         AND node_id=op.wl_id) AS sl_placed_events,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='fast_path_fill_reconciliation'
         AND result='confirmed_via_poll' AND ticker=wl.ticker) AS stream_confirmed_events
  FROM open_positions op
  JOIN watch_list wl ON wl.id = op.wl_id
 WHERE wl.ticker = ?
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly one open position, with a real
    sl_order_id, exactly one 'placed' sl_placement event, and exactly one
    'confirmed_via_poll' fast_path_fill_reconciliation event (proving the
    entry fill was itself reconciled via the stream path) -- directly from
    the harness DB."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['sl_order_id'] is not None
          and rows[0]['sl_placed_events'] == 1 and rows[0]['stream_confirmed_events'] == 1
          and rows[0]['state'] == 'live')
    return ok, rows
