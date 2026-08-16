"""Phase 2 scenario: `market_buy_placement`, proving `_attempt_automated_
market_buy` + `_sync_confirm_and_protect` (signals_notify.py) succeed end to
end on the ORDINARY, no-delay path: a real MARKET order is placed, the
synchronous fast-confirm poll finds the fill on its very first attempt
(matching a real market order's actual fill latency -- seconds, not the full
~10s `_SL_FAST_CONFIRM_ATTEMPTS` x `_SL_FAST_CONFIRM_INTERVAL_SECS` budget),
and the position opens + gets a genuine protective stop entirely within the
synchronous call chain -- no async fallback (`check_auto_fills`/
`drain_fill_queue`) ever needs to run.

Grid row (scripts/coverage_registry.py, id='market_buy_placement') notes
field states this in plain terms: "No real (non-dry_run) order has ever been
placed by this system" for this mechanism. That's still literally true after
this scenario (a fake-venue run is not a real broker order) -- what this
closes is the SEPARATE, narrower gap: whether the synchronous confirm-and-
protect code path itself, driven through the real `notify_buy_signal`
entrypoint against a stateful simulated order book, behaves as designed on
the common case. Distinct from two closely-related siblings built the same
night, deliberately not duplicated here:
  - `sl_async_fallback` (fake_venue/scenarios_sl_async_fallback.py) drives
    the exact same entrypoint but ARTIFICIALLY DELAYS `get_filled_order` so
    the sync path times out, then proves the async stream fallback recovers
    it. That scenario's own module docstring is explicit that leg 1 there
    ends in "NO stop-loss order exists at the broker yet -- the real,
    temporary UNPROTECTED window" -- i.e. it deliberately never lets the
    sync path itself succeed. Nothing in this codebase had separately proven
    the sync path's OWN success case (get_filled_order finding the fill on
    the first attempt, no delay at all) until this scenario.
  - `sl_sync_placement` (fake_venue/scenarios_sl_sync_placement.py) is
    entirely about a DIFFERENT caller of the shared `_reconcile_buy_fill` ->
    `_place_stop_loss_for_position` chain -- the real websocket/
    ACCT_ACTIVITY stream path (`drain_fill_queue`), for a trailing-buy-style
    SEEDED entry, not `_attempt_automated_market_buy`/
    `_sync_confirm_and_protect` at all.
This scenario is the one that actually drives `_attempt_automated_market_buy`
(the real order-placement call) and lets `_sync_confirm_and_protect`
genuinely succeed on its own, synchronous poll -- the happy-path shape that
covers the large majority of real market-buy trades in production (a market
order fills in seconds; the async fallback exists for the rare API-lag
minority `sl_async_fallback` covers).

Also carries the `tests/test_fake_broker_entry_scenario.py`-style self-
declared marker (`registry id 'market_buy_placement'`, in the paired pytest
wrapper's docstring, not this module's) -- see that test file's own docstring
for why `scripts/coverage_registry.py`'s row uses `check_mechanism=
'scenario_expectations'` (tied to a real canary trade closing, not a
coverage_events scenario_key) and therefore needs the opt-in marker rather
than an automatic event-asserted match. That marker already existed before
this scenario (test_market_buy_entry_fills_and_protects_with_a_real_stop,
tests/fake_broker.py-based) -- this scenario adds SECOND, independent
evidence for the same Grid row via the harness's stronger isolation/proof-
by-fresh-connection discipline (production-path access tripwire, a plain
sqlite3 read against the harness DB rather than trusting in-process state),
not a replacement for the existing one.

Shape (one fake node, one fake account):

  Setup   a `TrailingExitZScoreBreakout` node (market-buy-eligible, not
          trailing-buy -- same node shape as `sl_async_fallback`) is added.
          The real BUY signal is driven through `notify_buy_signal` (not
          seeded), since the mechanism under test starts at automated
          market-buy PLACEMENT itself.

  Single  `notify.notify_buy_signal(node, sig)` runs with `get_filled_order`
  leg    completely UNMODIFIED (unlike `sl_async_fallback`'s leg 1) --
          FakeBroker fills a MARKET order same-tick, so the real, undelayed
          `_sync_confirm_and_protect` poll finds it on its first attempt.
          -> `_attempt_automated_market_buy` places a real MARKET order via
             `schwab_client.place_equity_buy`
          -> `_sync_confirm_and_protect` calls `get_filled_order` once,
             confirms immediately, and calls `_reconcile_buy_fill` directly
             (no timeout branch reached)
          -> `_reconcile_buy_fill` opens the position, then `_reconcile_fill`
             may top it up (market_pad_pct sizing can undersize the initial
             fill slightly -- same real, already-covered mechanism
             `post_fill_topup` exists for; not this scenario's target, so
             it's asserted-but-not-required-exact, matching
             `test_market_buy_entry_fills_and_protects_with_a_real_stop`'s
             own `>= 1` market-order-count assertion for the same reason)
          -> ticker is in AUTOMATION_ENABLED_TICKERS, so
             `_place_stop_loss_for_position(node, ticker)` runs, sizing the
             real broker STOP for the FULL (possibly topped-up) share count
          => coverage_events['sl_placement_fast_confirm_timeout'] never
             fires -- proves the sync path's OWN success case, not the
             timeout+fallback case `sl_async_fallback` already covers
                                                                  <-- checked
          => coverage_events['fast_path_fill_reconciliation'] never fires at
             all (that scenario_key is emitted ONLY inside
             `drain_fill_queue`, signals_notify.py ~4605-4702 -- grepped
             directly, not assumed) -- proves the position was opened
             entirely within the SYNCHRONOUS call chain, with no async
             stream/poll path involved                            <-- checked
          => coverage_events['buy_fill_reconciled'] = 'opened'     <-- checked
          => coverage_events['sl_placement'] = 'placed'            <-- TARGET
          => open_positions.sl_order_id set, a genuine broker STOP order
             exists sized for the real (possibly post-top-up) share count,
             anchored to entry_price * (1 - fixed_sl%)             <-- checked
"""
from dataclasses import dataclass
from datetime import datetime

from fake_venue import venue
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

    db.add_node(ticker=TICKER, strategy='TrailingExitZScoreBreakout', version='fake_venue_market_buy',
                window=20, take_profit=10, stop_loss=1, max_hold_hours=56,
                state='live', account=CASH_ALIAS, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=None, trail_pct=1.0, fixed_sl_override=FIXED_SL_PCT,
                label='fake-venue harness node (market_buy_placement)')
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

    # Same rationale as sl_async_fallback's module docstring/setup comment:
    # check_order's BUY trading-day AND signal-window gates (schwab_safety.py
    # ~1650/~1663) are both unconditional for a fresh, signal-driven entry --
    # real and correct in production, but orthogonal to the mechanism under
    # test, and the harness must be runnable deterministically at any
    # wall-clock time/day. Both gates read schwab_safety._now() exclusively.
    real_now = schwab_safety._now()
    observations['real_now'] = real_now.isoformat()
    IN_WINDOW_TRADING_DAY = datetime(2026, 7, 29, 10, 30)  # a known real NYSE trading day, in-window
    say(f"[setup] faking schwab_safety._now() to {IN_WINDOW_TRADING_DAY} (a known in-window trading "
        f"day) -- orthogonal to the mechanism under test")
    schwab_safety._now = lambda: IN_WINDOW_TRADING_DAY

    schwab_safety.enable_auto_fill_detection(TICKER)
    schwab_safety.enable_node_auto_fill_detection(node['id'])

    # ------------------------------------------------------------- the leg
    # get_filled_order is deliberately left UNMODIFIED -- unlike
    # sl_async_fallback's leg 1, nothing here delays or blocks it. FakeBroker
    # fills a MARKET order same-tick (mirroring a real market order's actual
    # broker-side behavior), so _sync_confirm_and_protect's own poll finds
    # the fill on its first attempt -- this is the scenario's entire point:
    # proving the SYNCHRONOUS confirm-and-protect chain succeeds on its own,
    # ordinary case, with zero reliance on the async fallback paths
    # sl_async_fallback already covers.
    orders_before = set(broker.orders)
    say("[leg] calling the real signals_notify.notify_buy_signal(node, sig) -- "
        "_attempt_automated_market_buy places a real MARKET order, then "
        "_sync_confirm_and_protect's own undelayed poll must confirm the fill "
        "and protect the position entirely synchronously")
    sig = _sig(TICKER, price)
    notify.notify_buy_signal(node, sig)

    market_orders = [oid for oid in set(broker.orders) - orders_before
                     if broker.orders[oid]['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                     and broker.orders[oid]['orderType'] == 'MARKET']
    checks.append(Check("a real MARKET BUY order was placed at the broker "
                        "(_attempt_automated_market_buy -> schwab_client.place_equity_buy)",
                        len(market_orders) >= 1,
                        f"market_orders={[(oid, broker.orders[oid]['orderLegCollection'][0]['instruction']) for oid in market_orders]}"))
    checks.append(Check("the market order(s) placed are already FILLED at the broker "
                        "(FakeBroker's real same-tick market-fill behavior)",
                        market_orders and all(broker.orders[oid]['status'] == 'FILLED' for oid in market_orders),
                        f"statuses={[broker.orders[oid]['status'] for oid in market_orders]}"))

    pendings = [p for p in db.get_pending_buys() if p['ticker'] == TICKER]
    checks.append(Check("the pending_buys row created for the automated order was cleared -- "
                        "_reconcile_buy_fill ran synchronously within notify_buy_signal's own "
                        "call, not left waiting on a later poll/stream detection",
                        pendings == [], f"pendings={pendings}"))

    timeout_events = db.get_coverage_events(scenario_key="sl_placement_fast_confirm_timeout")
    checks.append(Check("sl_placement_fast_confirm_timeout NEVER fired -- proves the sync "
                        "path's own undelayed success case, distinct from sl_async_fallback's "
                        "deliberately-delayed timeout+fallback scenario",
                        [e for e in timeout_events if e['node_id'] == node['id']] == [],
                        f"events={[(e['result'], e['node_id']) for e in timeout_events]}"))

    # fast_path_fill_reconciliation is emitted ONLY inside drain_fill_queue
    # (signals_notify.py ~4605-4702, grepped directly to confirm -- see
    # module docstring) -- its total absence here is direct proof this fill
    # was reconciled entirely within the SYNCHRONOUS _sync_confirm_and_protect
    # chain, never touching the async stream fast path.
    fast_path_events = db.get_coverage_events(scenario_key="fast_path_fill_reconciliation")
    checks.append(Check("fast_path_fill_reconciliation never fired at all -- the fill was "
                        "reconciled entirely synchronously, no async stream/poll path involved",
                        [e for e in fast_path_events if e['ticker'] == TICKER] == [],
                        f"events={[(e['result'], e['ticker']) for e in fast_path_events]}"))

    buy_events = db.get_coverage_events(scenario_key="buy_fill_reconciled")
    checks.append(Check("buy_fill_reconciled fired 'opened' for the synchronously-confirmed fill",
                        any(e['result'] == 'opened' and e['node_id'] == node['id'] for e in buy_events),
                        f"results={[(e['result'], e['node_id']) for e in buy_events]}"))

    pos = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("position is open, entirely within the synchronous notify_buy_signal call",
                        pos is not None,
                        f"shares={pos['shares'] if pos else None} entry={pos['entry_price'] if pos else None}"))

    sl_events = db.get_coverage_events(scenario_key="sl_placement")
    placed_sl = [e for e in sl_events if e['result'] == 'placed' and e['node_id'] == node['id']]
    checks.append(Check("_place_stop_loss_for_position fired 'placed' -- the TARGET this "
                        "scenario exists to prove: the sync-confirm chain's own placement of a "
                        "genuine protective stop, on the ordinary (no-timeout) case",
                        len(placed_sl) == 1,
                        f"events={[(e['result'], e['detail']) for e in sl_events]}"))

    sl_order_id = pos.get('sl_order_id') if pos else None
    checks.append(Check("open_positions.sl_order_id set by the real placement write",
                        sl_order_id is not None, f"sl_order_id={sl_order_id}"))

    stop_orders = [oid for oid in set(broker.orders) - orders_before
                   if broker.orders[oid]['orderType'] == 'STOP']
    checks.append(Check("exactly one new broker STOP order placed alongside the market BUY",
                        len(stop_orders) == 1,
                        f"stop_orders={stop_orders}"))
    stop_order = broker.orders[stop_orders[0]] if len(stop_orders) == 1 else None
    if stop_order is not None:
        leg = stop_order['orderLegCollection'][0]
        checks.append(Check("real broker STOP order id matches open_positions.sl_order_id",
                            stop_orders[0] == sl_order_id,
                            f"stop_order_id={stop_orders[0]} sl_order_id={sl_order_id}"))
        checks.append(Check("STOP order is a SELL sized for the position's real (possibly "
                            "post-top-up) share count",
                            leg['instruction'] == 'SELL' and pos is not None
                            and leg['quantity'] == pos['shares'],
                            f"instruction={leg['instruction']} quantity={leg['quantity']} "
                            f"pos_shares={pos['shares'] if pos else None}"))
        expected_stop_price = round(pos['entry_price'] * (1 - FIXED_SL_PCT / 100), 4) if pos else None
        checks.append(Check("STOP price is anchored to the real entry_price * (1 - fixed_sl%), "
                            "matching strategies.py's own SL check",
                            expected_stop_price is not None
                            and abs(stop_order['stopPrice'] - expected_stop_price) < 0.01,
                            f"stop_price={stop_order.get('stopPrice')} expected~={expected_stop_price}"))

    observations['node_wl_id'] = node['id']
    observations['price'] = price
    observations['final_shares'] = pos['shares'] if pos else None
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
         AND ticker=wl.ticker) AS async_fast_path_events,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='sl_placement_fast_confirm_timeout'
         AND node_id=op.wl_id) AS sync_timeout_events
  FROM open_positions op
  JOIN watch_list wl ON wl.id = op.wl_id
 WHERE wl.ticker = ?
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly one open position, with a real
    sl_order_id, exactly one 'placed' sl_placement event, and ZERO
    fast_path_fill_reconciliation/sl_placement_fast_confirm_timeout events --
    proof the whole chain resolved synchronously, directly from the harness
    DB."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['sl_order_id'] is not None
          and rows[0]['sl_placed_events'] == 1 and rows[0]['async_fast_path_events'] == 0
          and rows[0]['sync_timeout_events'] == 0 and rows[0]['state'] == 'live')
    return ok, rows
