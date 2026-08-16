"""Phase 2 scenario: `sl_async_fallback`, proving that when the SYNCHRONOUS
fast-confirm SL-placement path (`signals_notify._sync_confirm_and_protect`,
run immediately after an automated market-buy is placed) times out waiting
for the fill to confirm, the position still gets opened, tops up, and gets a
genuine broker-side protective stop once an ASYNC path later confirms the
same fill -- not left silently unprotected forever.

Grid row (scripts/coverage_registry.py, id='sl_async_fallback') found via a
mechanical (not prose-based) re-verification of the harness's Category A
stream-exposure queue, 2026-08-16: its own `code_path` field already named
this exact mechanism verbatim ("check_auto_fills/drain_fill_queue/
check_gap_resize fill poll"), yet no scenario had ever been queued for it.
Its `notes` field flagged the real open question directly: the timeout
BRANCH itself was confirmed reachable live (VOO, dry_run, 2026-07-24 --
`sl_placement_fast_confirm_timeout` fired for real), but nothing had ever
confirmed the fallback SL placement that follows actually succeeds. This
scenario closes that gap.

Real mechanism (signals_notify.py):
  `notify_buy_signal` -> `_attempt_automated_market_buy` places a real
  MARKET order (market-buy-eligible node, e.g. `TrailingExitZScoreBreakout`
  in `AUTOMATION_ENABLED_TICKERS`) -> `_sync_confirm_and_protect` (~line
  1404) polls `get_filled_order(order_id=...)` up to `_SL_FAST_CONFIRM_ATTEMPTS`
  times, `_SL_FAST_CONFIRM_INTERVAL_SECS` apart. On a hit, it calls
  `_reconcile_buy_fill` directly (opens the position, tops up, places the
  SL) -- the common case, a market order fills in seconds. On a MISS (rare
  -- an API/exchange reporting lag, not a broker-side non-fill: the order IS
  really filled, our own confirmation read just hasn't caught up yet), it
  logs `sl_placement_fast_confirm_timeout`='timed_out', posts an urgent
  "may be temporarily UNPROTECTED" alert, and returns WITHOUT opening the
  position or placing any stop -- explicitly deferring to "the async
  pipeline" per its own alert text.

  `_reconcile_buy_fill` (~line 4301) is shared by every path that can later
  pick this fill up, and its own docstring explains why the SL placement
  call was moved there (2026-07-21 fix): "if [the sync] path timed out, the
  async fallback paths below silently never placed a stop at all,
  contradicting the timeout alert's own claim that the fallback would
  eventually cover it." So the real fallback contract is: whichever path
  eventually calls `_reconcile_buy_fill` for this fill is what places the
  stop, not `_sync_confirm_and_protect` itself.

FOUND 2026-08-16, FIXED 2026-08-16 (same day, this scenario's own Leg 1.5) --
of the THREE fallback paths the Grid row's own `code_path` names, only ONE
(`drain_fill_queue`) originally recovered a timed-out market-buy's
sync-confirm:
  - `check_gap_resize` (~line 4439) is scoped to a still-RESTING trailing-buy
    order gapping through its trigger pre-market (`trail_buy_pct`/
    `running_low` concepts) -- a market-buy order isn't "resting" in that
    sense (FakeBroker/a real MARKET order both fill same-tick), so this path
    was never actually reachable for this case; the Grid row's code_path
    listing it is imprecise but not a bug on its own.
  - `check_auto_fills` (~line 4711)'s buy-side loop was gated on
    `pending['order_placed']` (`if not pending['order_placed']: continue`)
    unconditionally -- but for a market-buy-eligible node, NOTHING ever sets
    `pending_buys.order_placed=1`. `mark_pending_buy_placed_by_wl_id` (the
    only setter besides the DB migration default of 0) is called from
    exactly two places: `notify_buy_signal`'s TRAILING-buy branch and
    `signals_handlers.handle_trail_buy_order_placed` (the manual "Trailing
    Buy Order Placed" Slack button, also trailing-buy-only) -- neither ever
    fires for a market-buy node. This wasn't specific to the timeout case
    either: the buy-side branch was dead code for EVERY automated
    market-buy fill, timed-out sync-confirm or not.

    FIX (2026-08-16): the buy-side loop now branches on
    `db._is_trailing_buy(node)` -- a trailing-buy row still requires
    `order_placed` (the real "resting, awaiting fill" broker state a
    GOOD_TILL_CANCEL trailing order genuinely has); a market-buy row instead
    requires only a real `order_id` on file, since a market order has no
    analogous resting state -- it fills same-tick or the placement attempt
    itself failed, and `order_id` is already the correct, and only, signal
    that there's an unconfirmed fill outcome left to discover. Leg 1.5 below
    now proves this recovers the fill end-to-end (position opens, tops up,
    gets a real protective stop) via `check_auto_fills` alone, `required=True`.
  - `drain_fill_queue` (~line 4556, the real-time ACCT_ACTIVITY stream fast
    path) already worked for this case (matches a stream fill event to a
    pending_buys row by `order_id` alone, never consulting `order_placed`)
    and still does -- Leg 2 below drives it, independently, against a SECOND
    node/account (so it isn't just re-observing Leg 1.5's already-reconciled
    fill) and confirms it still recovers the position AND the stop-loss
    placement.

Shape (two fake nodes -- same ticker, two different fake accounts -- so
Leg 1.5's `check_auto_fills` recovery and Leg 2's `drain_fill_queue` recovery
each act on their OWN, independent timed-out fill rather than one leg
re-observing the other's already-reconciled row):

  Setup   two `TrailingExitZScoreBreakout` nodes (market-buy-eligible, not
          trailing-buy) are added, node_a on CASH_ALIAS and node_b on
          MARGIN_ALIAS; auto-fill-detection is enabled for the ticker and
          both node ids up front (both legs below need the opt-in gate open
          to reach the mechanism under test). Each leg's real BUY signal is
          driven through `notify_buy_signal` (not seeded) since the
          mechanism under test starts at automated market-buy PLACEMENT
          itself.

  Leg 1   (node_a) `schwab_client.get_filled_order` is monkeypatched to
          always return None for the duration of the call (an artificial
          confirmation-read lag, exactly `scenarios_drought_handoff.py`'s
          established delay pattern -- the order genuinely fills at the
          broker throughout, only OUR read of that fact is delayed) so
          `_sync_confirm_and_protect` genuinely exhausts its full
          `_SL_FAST_CONFIRM_ATTEMPTS` budget.
          => coverage_events['sl_placement_fast_confirm_timeout'] =
             'timed_out'                                            <-- checked
          => position NOT opened, NO stop-loss order at the broker -- the
             real, temporary UNPROTECTED window this scenario reproduces
                                                                      <-- checked

  Leg 1.5 (node_a) the delay is lifted (get_filled_order restored to the
          real function, so the fill IS now confirmable) and the real,
          production `check_auto_fills([])` -- the "slow poll" the Grid
          row's own code_path names -- is run exactly as active_signals.py's
          run_loop calls it. TARGET: proves the fix above -- the fill is
          recovered, the position opens, tops up, and gets a genuine
          broker-side protective stop, with NO involvement from
          `drain_fill_queue`/the stream path at all.
          => coverage_events['buy_fill_reconciled'] = 'opened'       <-- checked
          => coverage_events['sl_placement'] = 'placed'              <-- TARGET
          => a genuine broker STOP order exists, sized for node_a's real
             position, anchored to entry_price * (1 - fixed_sl%)     <-- checked
          => open_positions.sl_order_id set by the real production write
                                                                       <-- checked

  Leg 2   (node_b) an independent repeat of Leg 1's timeout (own delayed
          get_filled_order, own notify_buy_signal call), then recovered via
          the REAL async fallback that already worked before this fix: a
          real-shaped ACCT_ACTIVITY fill message for node_b's market-buy
          order's own order_id is emitted through the real stream parser,
          and `drain_fill_queue()` -- called exactly as active_signals.py's
          run_loop calls it -- picks it up, confirms it via its own
          `get_filled_order` poll, and calls `_reconcile_buy_fill`, which
          opens the position, tops it up, and places the real protective
          stop. Proves the pre-existing stream-path fallback still works
          unchanged alongside the newly-fixed poll-path fallback.
          => coverage_events['fast_path_fill_reconciliation'] =
             'confirmed_via_poll'                                    <-- checked
          => coverage_events['buy_fill_reconciled'] = 'opened'       <-- checked
          => coverage_events['sl_placement'] = 'placed'              <-- checked
          => a genuine broker STOP order exists, sized for node_b's real
             position, anchored to entry_price * (1 - fixed_sl%)     <-- checked
          => open_positions.sl_order_id set by the real production write
                                                                       <-- checked

Entry-side state is placed through the real automated BUY path (not seeded)
-- unlike most sibling Phase 2 scenarios, this scenario's target starts
there (the sync-confirm timeout only exists downstream of a real automated
placement), so seeding would skip the exact window this scenario exists to
reproduce.
"""
from dataclasses import dataclass
from datetime import datetime

from fake_venue import activity_stream, venue
from fake_venue.scenarios_meta import (CASH_ACCOUNT_NUMBER, CASH_ALIAS, MARGIN_ACCOUNT_NUMBER,
                                        MARGIN_ALIAS, PRICE_SOURCE_TICKER, TICKER)

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

    db.add_node(ticker=TICKER, strategy='TrailingExitZScoreBreakout',
                version=f'fake_venue_sl_async_{version_suffix}',
                window=20, take_profit=10, stop_loss=1, max_hold_hours=56,
                state='live', account=account, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=None, trail_pct=1.0, fixed_sl_override=FIXED_SL_PCT,
                label='fake-venue harness node (sl_async_fallback)')
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER and n['account'] == account][0]


def _sig(ticker, price):
    return {
        'ticker': ticker, 'current_price': price, 'z_score': -2.4,
        'last_bar': datetime.now(), 'lower_band': price - 1.0,
        'sma': price + 2.0, 'std': 1.0, 'hurst': None, 'adf_p': None, 'window': 20,
    }


def _run_timeout_leg(leg_label, node, price, broker, schwab_client, notify, db, time_mod, say, checks):
    """Shared Leg-1-shaped body: places a real automated market buy with
    get_filled_order delayed for the whole call, so _sync_confirm_and_protect
    genuinely exhausts its budget and returns without opening a position or
    placing a stop. Used for both node_a (Leg 1, recovered via check_auto_fills
    at Leg 1.5) and node_b (Leg 2's own precondition, recovered via
    drain_fill_queue) -- each node's timeout is independent, so neither leg's
    recovery can be mistaken for observing the other's already-reconciled row.
    Returns order_id, market_orders (broker order ids for this ticker)."""
    real_get_filled_order = schwab_client.get_filled_order
    real_sleep = time_mod.sleep
    delay_state = {'calls': 0}

    def _delayed_get_filled_order(account, ticker, side, order_id=None):
        delay_state['calls'] += 1
        return None

    schwab_client.get_filled_order = _delayed_get_filled_order
    notify.time.sleep = lambda *a, **kw: None  # skip the real ~10s poll-interval wait, not part of the mechanism under test
    say(f"[{leg_label}] calling the real signals_notify.notify_buy_signal(node, sig) with "
        f"get_filled_order delayed past _sync_confirm_and_protect's own budget")
    sig = _sig(TICKER, price)
    try:
        notify.notify_buy_signal(node, sig)
    finally:
        schwab_client.get_filled_order = real_get_filled_order
        notify.time.sleep = real_sleep

    checks.append(Check(f"{leg_label}: get_filled_order's delay wrapper genuinely exhausted "
                        f"_sync_confirm_and_protect's full poll budget",
                        delay_state['calls'] >= notify._SL_FAST_CONFIRM_ATTEMPTS,
                        f"calls={delay_state['calls']} budget={notify._SL_FAST_CONFIRM_ATTEMPTS}"))

    pendings = [p for p in db.get_pending_buys() if p['ticker'] == TICKER and p['wl_id'] == node['id']]
    checks.append(Check(f"{leg_label}: pending_buys row exists for the automated market-buy order",
                        len(pendings) == 1, f"pendings={pendings}"))
    order_id = pendings[0]['order_id'] if pendings else None
    checks.append(Check(f"{leg_label}: pending_buys row carries the real broker order_id",
                        order_id is not None, f"order_id={order_id}"))

    market_orders_before = [oid for oid, o in broker.orders.items()
                            if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                            and o['orderType'] == 'MARKET' and o['account'] == node['account']]
    checks.append(Check(f"{leg_label}: a real MARKET order was placed and IS FILLED at the broker "
                        f"(our confirmation READ was delayed -- the broker itself never lagged)",
                        len(market_orders_before) == 1 and broker.orders[market_orders_before[0]]['status'] == 'FILLED',
                        f"market_orders={[(oid, broker.orders[oid]['status']) for oid in market_orders_before]}"))

    timeout_events = db.get_coverage_events(scenario_key="sl_placement_fast_confirm_timeout")
    timed_out = [e for e in timeout_events if e['result'] == 'timed_out' and e['node_id'] == node['id']]
    checks.append(Check(f"{leg_label}: sl_placement_fast_confirm_timeout fired 'timed_out' -- the "
                        f"real, already-live-confirmed branch this scenario builds on",
                        len(timed_out) == 1,
                        f"events={[(e['result'], e['ticker']) for e in timeout_events]}"))

    checks.append(Check(f"{leg_label}: position is NOT open yet -- _sync_confirm_and_protect returned "
                        f"before ever calling _reconcile_buy_fill",
                        db.get_open_position_by_wl_id(node['id']) is None))

    stop_orders_before = [oid for oid, o in broker.orders.items()
                          if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                          and o['orderType'] == 'STOP' and o['account'] == node['account']]
    checks.append(Check(f"{leg_label}: NO stop-loss order exists at the broker yet -- the real, "
                        f"temporary UNPROTECTED window this scenario reproduces",
                        len(stop_orders_before) == 0, f"stop_orders={stop_orders_before}"))

    return order_id, market_orders_before


def _assert_recovered(leg_label, node, broker, db, checks, extra_sl_check=None):
    """Shared post-recovery assertions (position open, SL placed and correctly
    anchored, sl_order_id persisted) -- used by both Leg 1.5 (check_auto_fills)
    and Leg 2 (drain_fill_queue) once each has driven its own node's fallback
    recovery. Returns (pos, stop_order)."""
    buy_events = db.get_coverage_events(scenario_key="buy_fill_reconciled")
    opened = [e for e in buy_events if e['result'] == 'opened' and e['node_id'] == node['id']]
    checks.append(Check(f"{leg_label}: buy_fill_reconciled fired 'opened' -- the position was "
                        f"finally opened via the fallback path, not the timed-out sync path",
                        len(opened) == 1,
                        f"events={[(e['result'], e['node_id']) for e in buy_events]}"))

    pos = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check(f"{leg_label}: position is now open", pos is not None))

    sl_events = db.get_coverage_events(scenario_key="sl_placement")
    placed = [e for e in sl_events if e['result'] == 'placed' and e['node_id'] == node['id']]
    checks.append(Check(f"{leg_label}: _place_stop_loss_for_position's real placement fired 'placed' "
                        f"-- the TARGET this scenario exists to prove: the fallback SL placement that "
                        f"follows a fallback-confirmed fill genuinely succeeds",
                        len(placed) == 1,
                        f"events={[(e['result'], e['detail']) for e in sl_events]}"))

    stop_orders = [oid for oid, o in broker.orders.items()
                   if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                   and o['orderType'] == 'STOP' and o['account'] == node['account']]
    stop_order = broker.orders[stop_orders[0]] if len(stop_orders) == 1 else None
    checks.append(Check(f"{leg_label}: a genuine broker STOP order now exists, sized for the real "
                        f"position's share count",
                        stop_order is not None and pos is not None
                        and stop_order['orderLegCollection'][0]['quantity'] == pos['shares'],
                        f"stop_orders={[(o['orderType'], o['stopPrice']) for o in [stop_order] if o]} "
                        f"pos_shares={pos['shares'] if pos else None}"))
    stop_price = stop_order['stopPrice'] if stop_order else None
    expected_stop_price = round(pos['entry_price'] * (1 - FIXED_SL_PCT / 100), 4) if pos else None
    checks.append(Check(f"{leg_label}: stop is anchored to entry_price * (1 - fixed_sl%), matching "
                        f"strategies.py's own SL check exactly",
                        stop_price is not None and expected_stop_price is not None
                        and abs(stop_price - expected_stop_price) < 0.01,
                        f"stop_price={stop_price} expected~={expected_stop_price}"))

    sl_order_id = pos.get('sl_order_id') if pos else None
    checks.append(Check(f"{leg_label}: open_positions.sl_order_id set by the real placement write",
                        sl_order_id is not None and stop_order is not None
                        and sl_order_id == stop_order['orderId'],
                        f"sl_order_id={sl_order_id} stop_order_id={stop_order['orderId'] if stop_order else None}"))
    return pos, stop_order


def run(price=None, verbose=True):
    """Runs the scenario against the already-isolated, already-imported
    environment. Returns (checks, observations)."""
    import time as time_mod

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
    venue.seed_account_number_env({CASH_ALIAS: CASH_ACCOUNT_NUMBER, MARGIN_ALIAS: MARGIN_ACCOUNT_NUMBER})
    price = venue.seed_quote(broker, TICKER, price, price_source_ticker=PRICE_SOURCE_TICKER)
    broker.set_cash_balance(CASH_ALIAS, 100_000.0)
    broker.set_cash_balance(MARGIN_ALIAS, 100_000.0)
    broker.set_buying_power(MARGIN_ALIAS, 100_000.0)
    say(f"[setup] {TICKER} quote seeded at ${price:.4f}")

    node_a = _add_node(CASH_ALIAS, 'a')
    node_b = _add_node(MARGIN_ALIAS, 'b')
    say(f"[setup] node_a wl_id={node_a['id']} ({CASH_ALIAS}), node_b wl_id={node_b['id']} "
        f"({MARGIN_ALIAS}), both TrailingExitZScoreBreakout (market-buy-eligible), "
        f"fixed_sl={FIXED_SL_PCT}%")

    # Opt-in gate for BOTH fallback paths under test (check_auto_fills AND
    # drain_fill_queue both require this) -- enabled up front for both nodes,
    # unlike the pre-fix version of this scenario which deliberately delayed
    # enabling it until after Leg 1.5 (back when Leg 1.5 was only demonstrating
    # the order_placed gap, not exercising real recovery).
    schwab_safety.enable_auto_fill_detection(TICKER)
    schwab_safety.enable_node_auto_fill_detection(node_a['id'])
    schwab_safety.enable_node_auto_fill_detection(node_b['id'])

    # check_order's BUY trading-day gate AND its BUY signal-window gate
    # (schwab_safety.py ~1650/~1663) are both unconditional for a fresh,
    # signal-driven entry (this scenario's leg 1 is exactly that -- not
    # is_gap_correction/is_protective/is_addon_leg, none of which this
    # placement qualifies for) -- real and correct in production, but this
    # scenario must be runnable deterministically at any wall-clock time,
    # any day, without degrading into "prove nothing because it's outside
    # 10:25-10:40". Both gates read schwab_safety._now() exclusively (its own
    # docstring: "Seam for tests to monkeypatch"), the same seam
    # tests/test_fake_broker_entry_scenario.py's `env` fixture uses -- a
    # single override covers both gates at once, rather than needing a
    # separate _is_trading_day override the way sibling scenarios that only
    # exercise gap-correction/protective placements (already exempt from the
    # window gate) do.
    real_now = schwab_safety._now()
    observations['real_now'] = real_now.isoformat()
    IN_WINDOW_TRADING_DAY = datetime(2026, 7, 29, 10, 30)  # a known real NYSE trading day, in-window
    say(f"[setup] faking schwab_safety._now() to {IN_WINDOW_TRADING_DAY} (a known in-window trading "
        f"day) -- orthogonal to the mechanism under test, covers both the trading-day and "
        f"signal-window BUY gates via their shared seam")
    schwab_safety._now = lambda: IN_WINDOW_TRADING_DAY

    # ---------------------------------------------------------------- leg 1
    # node_a's real automated market-buy times out at the sync-confirm step
    # (get_filled_order delayed for the whole call -- exactly
    # scenarios_drought_handoff.py's established delay pattern: the order
    # genuinely fills at the broker throughout, only OUR read of that fact is
    # delayed).
    order_id_a, market_orders_a = _run_timeout_leg(
        "leg 1", node_a, price, broker, schwab_client, notify, db, time_mod, say, checks)

    # -------------------------------------------------------------- leg 1.5
    # TARGET: the delay is fully lifted (get_filled_order is REAL again) and
    # the real, production check_auto_fills([]) -- the "slow poll" the Grid
    # row's own code_path names -- is run exactly as active_signals.py's
    # run_loop calls it. Before the 2026-08-16 fix this was dead code for a
    # market-buy node (gated on pending_buys.order_placed, never set for a
    # market order); the fix branches the buy-side loop on
    # db._is_trailing_buy(node) so a market-buy row is instead gated on
    # having a real order_id -- proven here end-to-end, required=True.
    pendings_a = [p for p in db.get_pending_buys() if p['ticker'] == TICKER and p['wl_id'] == node_a['id']]
    pending_before_1_5 = pendings_a[0] if pendings_a else {}
    checks.append(Check("leg 1.5 precondition: the pending row's order_placed is 0/False "
                        "(never set for a market-buy node -- order_id, not order_placed, is what "
                        "the fixed check_auto_fills now keys the recovery on)",
                        not pending_before_1_5.get('order_placed'),
                        f"order_placed={pending_before_1_5.get('order_placed')}"))
    say("[leg 1.5] delay lifted; calling the real signals_notify.check_auto_fills([]) -- the "
        "'slow poll' fallback the Grid row's own code_path names -- to prove the 2026-08-16 fix "
        "recovers a market-buy node's timed-out fill via order_id alone")
    notify.check_auto_fills([])
    checks.append(Check(
        "leg 1.5 TARGET: check_auto_fills' buy-side loop recovers node_a's timed-out fill purely "
        "via check_auto_fills([]) -- no drain_fill_queue/stream involvement at all for this node",
        db.get_open_position_by_wl_id(node_a['id']) is not None,
        "position opened by check_auto_fills([]) alone -- confirms the fix"))
    pos_a, stop_order_a = _assert_recovered("leg 1.5", node_a, broker, db, checks)

    # ---------------------------------------------------------------- leg 2
    # An INDEPENDENT second node/fill (node_b, MARGIN_ALIAS) proves the
    # pre-existing drain_fill_queue stream-path fallback still works
    # unchanged alongside the newly-fixed check_auto_fills poll-path fallback
    # -- not just re-observing node_a's already-reconciled row.
    order_id_b, market_orders_b = _run_timeout_leg(
        "leg 2 setup", node_b, price, broker, schwab_client, notify, db, time_mod, say, checks)

    fill_price = price
    fill_shares_b = market_orders_b and broker.orders[market_orders_b[0]]['orderLegCollection'][0]['quantity']
    say(f"[leg 2] emitting a real-shaped ACCT_ACTIVITY fill message for node_b's market-buy order "
        f"{order_id_b} ({fill_shares_b} shares @ ${fill_price:.4f}); running drain_fill_queue()")
    activity_stream.emit_fill(MARGIN_ACCOUNT_NUMBER, order_id_b, TICKER, 'BUY', fill_price, fill_shares_b)

    queued = activity_stream.queued_events()
    expected_tuple = (MARGIN_ACCOUNT_NUMBER, TICKER, 'BUY', fill_price, float(fill_shares_b), str(order_id_b))
    checks.append(Check("leg 2: real parser decoded node_b's market-buy order's own fill envelope correctly",
                        queued == [expected_tuple],
                        f"queued={queued} expected={[expected_tuple]}"))

    try:
        notify.drain_fill_queue()
        observations['drain_fill_queue_result'] = 'completed without raising'
    except Exception as e:
        observations['drain_fill_queue_result'] = f"{type(e).__name__}: {e}"
    checks.append(Check("leg 2: drain_fill_queue() did not raise reconciling the delayed fill",
                        observations['drain_fill_queue_result'] == 'completed without raising'))

    fast_path_events = db.get_coverage_events(scenario_key="fast_path_fill_reconciliation")
    confirmed = [e for e in fast_path_events if e['result'] == 'confirmed_via_poll' and e['node_id'] == node_b['id']]
    checks.append(Check("leg 2: fast_path_fill_reconciliation fired 'confirmed_via_poll' for node_b -- "
                        "the pre-existing async stream path picking up what its own sync path missed",
                        len(confirmed) == 1,
                        f"events={[(e['result'], e['node_id']) for e in fast_path_events]}"))

    pos_b, stop_order_b = _assert_recovered("leg 2", node_b, broker, db, checks)

    observations['node_a_wl_id'] = node_a['id']
    observations['node_b_wl_id'] = node_b['id']
    observations['price'] = price
    observations['order_id_a'] = order_id_a
    observations['order_id_b'] = order_id_b
    observations['fill_shares_b'] = fill_shares_b
    observations['stop_price_a'] = stop_order_a['stopPrice'] if stop_order_a else None
    observations['stop_price_b'] = stop_order_b['stopPrice'] if stop_order_b else None
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT op.wl_id, op.sl_order_id, op.entry_price, wl.account, wl.state,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='sl_placement_fast_confirm_timeout'
         AND result='timed_out' AND node_id=op.wl_id) AS sync_timeout_events,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='fast_path_fill_reconciliation'
         AND result='confirmed_via_poll' AND node_id=op.wl_id) AS async_confirm_events,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='sl_placement'
         AND result='placed' AND node_id=op.wl_id) AS sl_placed_events
  FROM open_positions op
  JOIN watch_list wl ON wl.id = op.wl_id
 WHERE wl.ticker = ?
 ORDER BY wl.account
"""


def verify_proof(db_path):
    """Returns (ok, rows). Two independent nodes/fills this scenario now
    drives (node_a/CASH_ALIAS via the fixed check_auto_fills poll-path,
    node_b/MARGIN_ALIAS via the pre-existing drain_fill_queue stream-path) --
    ok requires exactly two open positions, each with a real sl_order_id and
    exactly one sync-confirm timeout + one 'placed' sl_placement event; only
    node_b (the stream-path leg) should carry an async_confirm_events hit --
    node_a's recovery deliberately never touches drain_fill_queue at all, so
    a nonzero async_confirm_events on node_a's row would mean Leg 1.5 didn't
    actually recover the fill on its own, directly from the harness DB."""
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
    row_a = next((r for r in rows if r['account'] == CASH_ALIAS), None)
    row_b = next((r for r in rows if r['account'] == MARGIN_ALIAS), None)
    ok = (
        row_a is not None and row_b is not None
        and row_a['sl_order_id'] is not None and row_b['sl_order_id'] is not None
        and row_a['sync_timeout_events'] == 1 and row_b['sync_timeout_events'] == 1
        and row_a['sl_placed_events'] == 1 and row_b['sl_placed_events'] == 1
        and row_a['state'] == 'live' and row_b['state'] == 'live'
        # node_a recovered purely via check_auto_fills -- zero drain_fill_queue
        # involvement; node_b recovered purely via drain_fill_queue's stream path.
        and row_a['async_confirm_events'] == 0
        and row_b['async_confirm_events'] == 1
    )
    return ok, rows
