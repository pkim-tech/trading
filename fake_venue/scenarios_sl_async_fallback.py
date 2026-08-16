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

FOUND, NOT FIXED (2026-08-16, this scenario's first run) -- of the THREE
fallback paths the Grid row's own `code_path` names, only ONE actually
recovers a timed-out market-buy's sync-confirm:
  - `check_gap_resize` (~line 4439) is scoped to a still-RESTING trailing-buy
    order gapping through its trigger pre-market (`trail_buy_pct`/
    `running_low` concepts) -- a market-buy order isn't "resting" in that
    sense (FakeBroker/a real MARKET order both fill same-tick), so this path
    was never actually reachable for this case; the Grid row's code_path
    listing it is imprecise but not a bug on its own.
  - `check_auto_fills` (~line 4711)'s buy-side loop is gated on
    `pending['order_placed']` (`if not pending['order_placed']: continue`,
    line 4721) -- but for a market-buy-eligible node, NOTHING ever sets
    `pending_buys.order_placed=1`. `mark_pending_buy_placed_by_wl_id` (the
    only setter besides the DB migration default of 0) is called from
    exactly two places: `notify_buy_signal`'s TRAILING-buy branch (line
    1944) and `signals_handlers.handle_trail_buy_order_placed` (the manual
    "Trailing Buy Order Placed" Slack button, also trailing-buy-only) --
    neither ever fires for a market-buy node. This isn't specific to the
    timeout case either: `check_auto_fills`'s buy-side branch is dead code
    for EVERY automated market-buy fill, timed-out sync-confirm or not.
    Leg 1.5 below demonstrates this directly against real, confirmable
    broker state (a `required=False` Check, per this session's standing
    instruction: a fix here would touch `signals_notify.py` and needs the
    paired independent-cold + contextual Opus review this diff is subject
    to, so it is documented, not patched, in this scenario).
  - `drain_fill_queue` (~line 4556, the real-time ACCT_ACTIVITY stream fast
    path) is the one path that genuinely works for this case: it matches a
    stream fill event to a pending_buys row by `order_id` alone, never
    consulting `order_placed`. Leg 2 below drives it for real and confirms
    it recovers the position AND the stop-loss placement.

Shape (one fake node, one fake account):

  Setup   a `TrailingExitZScoreBreakout` node (market-buy-eligible, not
          trailing-buy) is added; the real BUY signal is driven through
          `notify_buy_signal` (not seeded) since the mechanism under test
          starts at automated market-buy PLACEMENT itself.

  Leg 1   `schwab_client.get_filled_order` is monkeypatched to always return
          None for the duration of the call (an artificial confirmation-read
          lag, exactly `scenarios_drought_handoff.py`'s established delay
          pattern -- the order genuinely fills at the broker throughout,
          only OUR read of that fact is delayed) so `_sync_confirm_and_protect`
          genuinely exhausts its full `_SL_FAST_CONFIRM_ATTEMPTS` budget.
          => coverage_events['sl_placement_fast_confirm_timeout'] =
             'timed_out'                                            <-- checked
          => position NOT opened, NO stop-loss order at the broker -- the
             real, temporary UNPROTECTED window this scenario reproduces
                                                                      <-- checked

  Leg 1.5 the delay is lifted (get_filled_order restored to the real
          function, so the fill IS now confirmable) and the real,
          production `check_auto_fills([])` -- the "slow poll" the Grid
          row's own code_path names -- is run exactly as active_signals.py's
          run_loop calls it. Demonstrates the dead-code gap above: even
          though the fill is genuinely confirmable now, nothing reconciles
          it, because `order_placed` was never set for this pending row.
          `required=False` -- a real, documented, unfixed finding, not a
          failure of this scenario's own target mechanism.             <-- note

  Leg 2   the REAL async fallback that works: a real-shaped ACCT_ACTIVITY
          fill message for the market-buy order's own order_id is emitted
          through the real stream parser, and `drain_fill_queue()` -- called
          exactly as active_signals.py's run_loop calls it -- picks it up,
          confirms it via its own `get_filled_order` poll, and calls
          `_reconcile_buy_fill`, which opens the position, tops it up, and
          places the real protective stop.
          => coverage_events['fast_path_fill_reconciliation'] =
             'confirmed_via_poll'                                    <-- checked
          => coverage_events['buy_fill_reconciled'] = 'opened'       <-- checked
          => coverage_events['sl_placement'] = 'placed'              <-- TARGET
          => a genuine broker STOP order exists, sized for the real position,
             anchored to entry_price * (1 - fixed_sl%)               <-- checked
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
from fake_venue.scenarios_meta import CASH_ACCOUNT_NUMBER, CASH_ALIAS, PRICE_SOURCE_TICKER, TICKER

FAKE_ACCOUNTS = [
    dict(alias=CASH_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='cash', margin_capable=0),
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


def _add_node():
    import signals_db as db

    db.add_node(ticker=TICKER, strategy='TrailingExitZScoreBreakout', version='fake_venue_sl_async',
                window=20, take_profit=10, stop_loss=1, max_hold_hours=56,
                state='live', account=CASH_ALIAS, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=None, trail_pct=1.0, fixed_sl_override=FIXED_SL_PCT,
                label='fake-venue harness node (sl_async_fallback)')
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]


def _sig(ticker, price):
    return {
        'ticker': ticker, 'current_price': price, 'z_score': -2.4,
        'last_bar': datetime.now(), 'lower_band': price - 1.0,
        'sma': price + 2.0, 'std': 1.0, 'hurst': None, 'adf_p': None, 'window': 20,
    }


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
    broker = venue.install_fake_broker([CASH_ALIAS])
    venue.seed_account_number_env({CASH_ALIAS: CASH_ACCOUNT_NUMBER})
    price = venue.seed_quote(broker, TICKER, price, price_source_ticker=PRICE_SOURCE_TICKER)
    broker.set_cash_balance(CASH_ALIAS, 100_000.0)
    say(f"[setup] {TICKER} quote seeded at ${price:.4f}")

    node = _add_node()
    say(f"[setup] node wl_id={node['id']} ({CASH_ALIAS}), TrailingExitZScoreBreakout "
        f"(market-buy-eligible), fixed_sl={FIXED_SL_PCT}%")

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
    # get_filled_order is delayed for the ENTIRE call (not just N attempts) --
    # notify_buy_signal's own market-buy placement (_attempt_automated_market_buy)
    # doesn't call get_filled_order itself, only _sync_confirm_and_protect does,
    # so a blanket delay can't affect the placement step, only the confirmation
    # poll this leg exists to exhaust. Mirrors scenarios_drought_handoff.py's
    # established delay pattern exactly.
    real_get_filled_order = schwab_client.get_filled_order
    real_sleep = time_mod.sleep
    delay_state = {'calls': 0}

    def _delayed_get_filled_order(account, ticker, side, order_id=None):
        delay_state['calls'] += 1
        return None

    schwab_client.get_filled_order = _delayed_get_filled_order
    notify.time.sleep = lambda *a, **kw: None  # skip the real ~10s poll-interval wait, not part of the mechanism under test
    say("[leg 1] calling the real signals_notify.notify_buy_signal(node, sig) with "
        "get_filled_order delayed past _sync_confirm_and_protect's own budget")
    sig = _sig(TICKER, price)
    try:
        notify.notify_buy_signal(node, sig)
    finally:
        schwab_client.get_filled_order = real_get_filled_order
        notify.time.sleep = real_sleep

    checks.append(Check("leg 1: get_filled_order's delay wrapper genuinely exhausted "
                        "_sync_confirm_and_protect's full poll budget",
                        delay_state['calls'] >= notify._SL_FAST_CONFIRM_ATTEMPTS,
                        f"calls={delay_state['calls']} budget={notify._SL_FAST_CONFIRM_ATTEMPTS}"))

    pendings = [p for p in db.get_pending_buys() if p['ticker'] == TICKER]
    checks.append(Check("leg 1: pending_buys row exists for the automated market-buy order",
                        len(pendings) == 1, f"pendings={pendings}"))
    order_id = pendings[0]['order_id'] if pendings else None
    checks.append(Check("leg 1: pending_buys row carries the real broker order_id",
                        order_id is not None, f"order_id={order_id}"))

    market_orders = [oid for oid, o in broker.orders.items()
                     if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                     and o['orderType'] == 'MARKET']
    checks.append(Check("leg 1: a real MARKET order was placed and IS FILLED at the broker "
                        "(our confirmation READ was delayed -- the broker itself never lagged)",
                        len(market_orders) == 1 and broker.orders[market_orders[0]]['status'] == 'FILLED',
                        f"market_orders={[(oid, broker.orders[oid]['status']) for oid in market_orders]}"))

    timeout_events = db.get_coverage_events(scenario_key="sl_placement_fast_confirm_timeout")
    timed_out = [e for e in timeout_events if e['result'] == 'timed_out' and e['node_id'] == node['id']]
    checks.append(Check("leg 1: sl_placement_fast_confirm_timeout fired 'timed_out' -- the "
                        "real, already-live-confirmed branch this scenario builds on",
                        len(timed_out) == 1,
                        f"events={[(e['result'], e['ticker']) for e in timeout_events]}"))

    checks.append(Check("leg 1: position is NOT open yet -- _sync_confirm_and_protect returned "
                        "before ever calling _reconcile_buy_fill",
                        db.get_open_position_by_wl_id(node['id']) is None))

    stop_orders_leg1 = [oid for oid, o in broker.orders.items()
                        if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                        and o['orderType'] == 'STOP']
    checks.append(Check("leg 1: NO stop-loss order exists at the broker yet -- the real, "
                        "temporary UNPROTECTED window this scenario reproduces",
                        len(stop_orders_leg1) == 0, f"stop_orders={stop_orders_leg1}"))

    # -------------------------------------------------------------- leg 1.5
    # FOUND, NOT FIXED (see module docstring): check_auto_fills' buy-side loop
    # is gated on pending_buys.order_placed, which is never set for a
    # market-buy node (mark_pending_buy_placed_by_wl_id is trailing-buy-only).
    # The delay is fully lifted here (get_filled_order is REAL, would genuinely
    # find the fill) -- so a reconciliation NOT happening is attributable only
    # to the order_placed gate, not to any remaining artificial delay.
    say("[leg 1.5] delay lifted; calling the real signals_notify.check_auto_fills([]) -- the "
        "'slow poll' fallback the Grid row's own code_path names -- to demonstrate the "
        "order_placed gap documented in the module docstring")
    pending_before_1_5 = pendings[0] if pendings else {}
    checks.append(Check("leg 1.5 precondition: the pending row's order_placed is 0/False "
                        "(never set for a market-buy node -- this is the gap being demonstrated, "
                        "not a scenario setup mistake)",
                        not pending_before_1_5.get('order_placed'),
                        f"order_placed={pending_before_1_5.get('order_placed')}"))
    notify.check_auto_fills([])
    checks.append(Check(
        "FOUND, NOT FIXED: check_auto_fills' buy-side loop does not recover this fill -- "
        "position is still NOT open after a real check_auto_fills([]) call, even though "
        "get_filled_order is no longer delayed and would genuinely confirm the fill if asked "
        "(signals_notify.py ~line 4721, `if not pending['order_placed']: continue` -- "
        "order_placed is never set to 1 for an automated market-buy node's pending row, only "
        "for trailing-buy nodes via mark_pending_buy_placed_by_wl_id). Not fixed here per this "
        "session's standing instruction (would touch signals_notify.py, needs the paired "
        "independent-cold + contextual Opus review this diff is subject to). drain_fill_queue "
        "(leg 2) is the one fallback path that genuinely recovers this case today.",
        db.get_open_position_by_wl_id(node['id']) is None,
        "position remains unopened after check_auto_fills([]) -- confirms the dead-code gap",
        required=False))

    # ---------------------------------------------------------------- leg 2
    # The REAL fallback that works: the market-buy order's own fill, driven
    # through the real stream path -- matches by order_id alone, never
    # consults order_placed.
    schwab_safety.enable_auto_fill_detection(TICKER)
    schwab_safety.enable_node_auto_fill_detection(node['id'])
    fill_price = price
    fill_shares = market_orders and broker.orders[market_orders[0]]['orderLegCollection'][0]['quantity']
    say(f"[leg 2] emitting a real-shaped ACCT_ACTIVITY fill message for the market-buy order "
        f"{order_id} ({fill_shares} shares @ ${fill_price:.4f}); running drain_fill_queue()")
    activity_stream.emit_fill(CASH_ACCOUNT_NUMBER, order_id, TICKER, 'BUY', fill_price, fill_shares)

    queued = activity_stream.queued_events()
    expected_tuple = (CASH_ACCOUNT_NUMBER, TICKER, 'BUY', fill_price, float(fill_shares), str(order_id))
    checks.append(Check("leg 2: real parser decoded the market-buy order's own fill envelope correctly",
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
    confirmed = [e for e in fast_path_events if e['result'] == 'confirmed_via_poll' and e['node_id'] == node['id']]
    checks.append(Check("leg 2: fast_path_fill_reconciliation fired 'confirmed_via_poll' -- the "
                        "real async path picking up what the sync path missed",
                        len(confirmed) == 1,
                        f"events={[(e['result'], e['node_id']) for e in fast_path_events]}"))

    buy_events = db.get_coverage_events(scenario_key="buy_fill_reconciled")
    opened = [e for e in buy_events if e['result'] == 'opened' and e['node_id'] == node['id']]
    checks.append(Check("leg 2: buy_fill_reconciled fired 'opened' -- the position was finally "
                        "opened via the async path, not the timed-out sync path",
                        len(opened) == 1,
                        f"events={[(e['result'], e['node_id']) for e in buy_events]}"))

    pos = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("leg 2: position is now open", pos is not None))

    sl_events = db.get_coverage_events(scenario_key="sl_placement")
    placed = [e for e in sl_events if e['result'] == 'placed' and e['node_id'] == node['id']]
    checks.append(Check("leg 2: _place_stop_loss_for_position's real placement fired 'placed' -- "
                        "the TARGET this scenario exists to prove: the fallback SL placement that "
                        "follows an async-confirmed fill genuinely succeeds",
                        len(placed) == 1,
                        f"events={[(e['result'], e['detail']) for e in sl_events]}"))

    stop_orders = [oid for oid, o in broker.orders.items()
                   if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                   and o['orderType'] == 'STOP']
    stop_order = broker.orders[stop_orders[0]] if len(stop_orders) == 1 else None
    checks.append(Check("leg 2: a genuine broker STOP order now exists, sized for the real "
                        "position's share count",
                        stop_order is not None and pos is not None
                        and stop_order['orderLegCollection'][0]['quantity'] == pos['shares'],
                        f"stop_orders={[(o['orderType'], o['stopPrice']) for o in [stop_order] if o]} "
                        f"pos_shares={pos['shares'] if pos else None}"))
    stop_price = stop_order['stopPrice'] if stop_order else None
    expected_stop_price = round(pos['entry_price'] * (1 - FIXED_SL_PCT / 100), 4) if pos else None
    checks.append(Check("leg 2: stop is anchored to entry_price * (1 - fixed_sl%), matching "
                        "strategies.py's own SL check exactly",
                        stop_price is not None and expected_stop_price is not None
                        and abs(stop_price - expected_stop_price) < 0.01,
                        f"stop_price={stop_price} expected~={expected_stop_price}"))

    sl_order_id = pos.get('sl_order_id') if pos else None
    checks.append(Check("leg 2: open_positions.sl_order_id set by the real placement write",
                        sl_order_id is not None and stop_order is not None
                        and sl_order_id == stop_order['orderId'],
                        f"sl_order_id={sl_order_id} stop_order_id={stop_order['orderId'] if stop_order else None}"))

    observations['node_wl_id'] = node['id']
    observations['price'] = price
    observations['order_id'] = order_id
    observations['fill_shares'] = fill_shares
    observations['stop_price'] = stop_price
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
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly one open position with a real
    sl_order_id, exactly one sync-confirm timeout event (proving the fallback
    path -- not the sync path -- did the work), exactly one async-confirm
    event, and exactly one 'placed' sl_placement event, directly from the
    harness DB."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['sl_order_id'] is not None
          and rows[0]['sync_timeout_events'] == 1 and rows[0]['async_confirm_events'] == 1
          and rows[0]['sl_placed_events'] == 1 and rows[0]['state'] == 'live')
    return ok, rows
