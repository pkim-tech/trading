"""Phase 1 scenario: same-ticker/two-account fill attribution, end to end.

Reproduces `buy_fill_reconciles_correct_node` -- the Accountability Grid row
that has never had live proof because no two live nodes organically share a
ticker -- through the REAL code path, and exercises the real ACCT_ACTIVITY
parser + drain_fill_queue fast path on the way.

Shape (one fake ticker, two fake accounts, three nodes, one shared FakeBroker):

  node A  (fv_cash,   cash-settlement account)  resting BUY order  ---.
  node B  (fv_margin, margin account)           resting BUY order  ---+  all pending
  node C  (fv_cash,   SAME account as A)        resting BUY order  ---'

Node C exists so the ambiguity is the REAL one. The JNUG collision (2026-08-10)
and its deliberate reproduction (YINN wl_id 199 + 228, both in `soxl_ira`)
were both same-ticker/SAME-account; a cross-account pair alone resolves
unambiguously everywhere and would prove a strictly weaker thing. With A and C
sharing ticker+account, signals_db.get_watch_list_node(ticker, account) returns
None -- the documented silent-ambiguity gap -- and the fill still has to land
on A, by wl_id, not on its same-account sibling.

Scoped honestly (rebuttal finding, 2026-08-16): the ambiguity is RECORDED here
(observations['ticker_account_lookup_is_ambiguous']) but no decision path in
this scenario consumes it. `place_stop_loss` threads node_id through
(schwab_client.py), so check_order takes its `node_id is not None` branch and
never reaches the ambiguous-lookup fallback whose own comment documents the
silent `node_automation_enabled(None) -> True` default. Driving THAT hazard
needs a real order placed with no node_id -- a later leg, not this one.

  Leg 1  broker fills A's order
         -> signals_notify.check_auto_fills()          [real slow-poll path]
         -> schwab_client.get_filled_order()           [real, against FakeBroker]
         -> _reconcile_buy_fill(..., wl_id=A)          [2 pendings for one ticker]
         => coverage_events['buy_fill_reconciles_correct_node'] = 'resolved'   <-- TARGET
         => position opens for A only; B's pending row untouched

  Leg 2  broker fills B's order
         -> fake ACCT_ACTIVITY message with a REAL-shaped AccountNumber AND a
            REAL-shaped (string) SchwabOrderID -- i.e. faithful to an actual
            Schwab message in both respects, not a partially-faithful probe
         -> schwab_stream._handle_activity_message()   [real parser]
         -> signals_notify.drain_fill_queue()          [real fast path]
         => fast_path_fill_reconciliation = 'confirmed_via_poll', B's position
            opens, B's pending row clears -- END-TO-END PROOF the two fixes
            below actually close the gap, through the exact real message shape

  Leg 3  idempotent-redelivery check: the SAME real-shaped fill message is
         emitted a second time (stream redelivery / at-least-once delivery is
         a real possibility, not hypothetical). B is already reconciled by
         leg 2, so this exercises drain_fill_queue's orphan-fill branch (no
         matching pending_buys row, ticker still in automation scope) --
         proves a duplicate delivery alerts safely instead of raising or
         opening a second position.

FIXED 2026-08-16 (docs/backlog_cache.md, both items now closed): this
scenario originally found and pinned down two real, pre-existing production
defects at this exact line, both now fixed:

  1. `AccountNumber` defect -- Schwab's ACCT_ACTIVITY messages carry
     `AccountNumber` as the raw account number ("45111931" etc. -- confirmed
     against 347 real messages in logs/active_signals.log), but
     drain_fill_queue passed that value straight into
     schwab_client.get_filled_order(account=...), which resolves it via
     _resolve_account_hashes()[account] -- a dict keyed by account ALIAS. A
     real account number was never a key there, so the fast path raised
     KeyError before reconciling anything. Fixed via
     schwab_client.resolve_account_alias_from_number() (mirrors
     _resolve_account_hashes's own SCHWAB_ACCOUNT_<ALIAS> suffix match, no
     new lookup table), called at the top of drain_fill_queue
     (signals_notify.py) before `account` is used for anything.

  2. `SchwabOrderID` defect -- real messages quote it
     ("SchwabOrderID":"1007506544737"), but get_filled_order compares
     `o.get("orderId") == order_id` with no coercion, and Schwab's REST order
     JSON returns `orderId` as a number, so the exact-order lookup always
     missed. Fixed by passing drain_fill_queue's already-int-coerced
     `_order_id_int` (previously computed only for the local pending-row
     match) into both get_filled_order calls instead of the raw string.

Neither was ever observed live because the parser itself produced zero events
until the 2026-08-15 fix, so no real message had ever reached this line
before this harness build.
"""
from dataclasses import dataclass
from datetime import datetime

from fake_venue import activity_stream, venue
from fake_venue.scenarios_meta import (  # noqa: F401  (re-exported for callers/tests)
    CASH_ACCOUNT_NUMBER, CASH_ALIAS, MARGIN_ACCOUNT_NUMBER, MARGIN_ALIAS,
    PRICE_SOURCE_TICKER, TICKER,
)

FAKE_ACCOUNTS = [
    dict(alias=CASH_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='cash', margin_capable=0),
    dict(alias=MARGIN_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='margin', margin_capable=1),
]
NODE_NOTIONAL = 2_000


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ''
    required: bool = True

    def __post_init__(self):
        self.ok = bool(self.ok)


def _add_node(alias, version):
    import signals_db as db

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version=version,
                window=20, take_profit=10, stop_loss=1, max_hold_hours=56,
                state='live', account=alias, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                label=f'fake-venue harness node ({alias})')
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER and n['version'] == version][0]


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
        # Re-running against a --keep'd DB would re-add nodes/pendings on top
        # of the previous run's still-open positions, and verify_proof's
        # single-row requirement would then fail for a confusing reason.
        raise RuntimeError("this harness DB has already been used -- point --db-path at a fresh "
                           "file (the default temp dir is fresh every run)")
    venue.seed_fake_accounts(FAKE_ACCOUNTS)
    broker = venue.install_fake_broker([a['alias'] for a in FAKE_ACCOUNTS])
    venue.seed_account_number_env({CASH_ALIAS: CASH_ACCOUNT_NUMBER, MARGIN_ALIAS: MARGIN_ACCOUNT_NUMBER})
    price = venue.seed_quote(broker, TICKER, price, price_source_ticker=PRICE_SOURCE_TICKER)
    for acct in (CASH_ALIAS, MARGIN_ALIAS):
        broker.set_cash_balance(acct, 100_000.0)
    say(f"[setup] {TICKER} quote seeded at ${price:.4f}")

    node_a = _add_node(CASH_ALIAS, 'fake_venue_a')
    node_b = _add_node(MARGIN_ALIAS, 'fake_venue_b')
    node_c = _add_node(CASH_ALIAS, 'fake_venue_c')   # same ticker AND same account as A
    say(f"[setup] node A wl_id={node_a['id']} ({CASH_ALIAS}), node B wl_id={node_b['id']} "
        f"({MARGIN_ALIAS}), node C wl_id={node_c['id']} ({CASH_ALIAS}, A's sibling)")
    # The real, already-documented silent-ambiguity gap this shape exists to
    # hold in place: a ticker+account lookup cannot tell A and C apart.
    observations['ticker_account_lookup_is_ambiguous'] = (
        db.get_watch_list_node(TICKER, account=CASH_ALIAS) is None)

    # Real per-ticker + per-node auto-fill-detection opt-in (writes into the
    # harness's own isolated state dir, never the real json files).
    schwab_safety.enable_auto_fill_detection(TICKER)
    for node in (node_a, node_b, node_c):
        schwab_safety.enable_node_auto_fill_detection(node['id'])

    # Entry-side state is SEEDED, not placed through the real BUY path -- design
    # decision #6's accepted caveat: valid for isolating a downstream state
    # transition, NOT a substitute for entry-path coverage. Consequence worth
    # knowing before extending this: the only real check_order traffic in this
    # run is the protective STOP (SELL side), and check_order's NYSE
    # trading-day gate is BUY-only (schwab_safety.py ~1652), which is why the
    # scenario runs identically on a weekend. Add a real BUY leg and it becomes
    # calendar-dependent.
    shares = max(int(NODE_NOTIONAL // price), 1)
    orders = {}
    sig = {'current_price': price, 'last_bar': datetime.now()}
    for node, alias in ((node_a, CASH_ALIAS), (node_b, MARGIN_ALIAS), (node_c, CASH_ALIAS)):
        orders[node['id']] = broker.seed_resting_order(alias, TICKER, 'TRAILING_STOP', 'BUY',
                                                        shares, trail_offset=1.0)
        db.add_pending_buy(node, sig, channel=None, ts=None, order_id=orders[node['id']])
        db.mark_pending_buy_placed_by_wl_id(node['id'])
    order_a, order_b = orders[node_a['id']], orders[node_b['id']]
    checks.append(Check("three pending buys for one ticker (two of them same-account)",
                        len([p for p in db.get_pending_buys() if p['ticker'] == TICKER]) == 3,
                        "the ambiguity the target Grid row is about"))
    checks.append(Check("ticker+account lookup cannot disambiguate A from C",
                        observations['ticker_account_lookup_is_ambiguous'],
                        "get_watch_list_node(ticker, account) returns None on the 2-node match -- "
                        "the real gap the JNUG/YINN pairs reproduce live"))

    # ---------------------------------------------------------------- leg 1
    fill_price_a = round(price * 0.99, 4)
    broker.force_fill(order_a, fill_price_a)
    say(f"[leg 1] broker filled node A's order {order_a} @ ${fill_price_a:.4f}; running check_auto_fills()")
    notify.check_auto_fills([])

    events = db.get_coverage_events(scenario_key="buy_fill_reconciles_correct_node")
    resolved = [e for e in events if e['result'] == 'resolved' and e['node_id'] == node_a['id']]
    checks.append(Check("buy_fill_reconciles_correct_node fired 'resolved' for node A",
                        len(resolved) == 1,
                        f"{len(events)} event(s): {[(e['result'], e['node_id'], e['detail']) for e in events]}"))
    pos_a = db.get_open_position_by_wl_id(node_a['id'])
    pos_b = db.get_open_position_by_wl_id(node_b['id'])
    checks.append(Check("position opened for node A", pos_a is not None,
                        f"shares={pos_a['shares'] if pos_a else None} entry={pos_a['entry_price'] if pos_a else None}"))
    # buy_fill_reconciled -- fill MATH correctness (Grid row 'buy_fill_reconciled'),
    # distinct from buy_fill_reconciles_correct_node's node-identity check above.
    # Piggybacked here per docs/backlog_cache.md rather than a standalone
    # scenario: check_auto_fills' slow-poll path (leg 1's own mechanism)
    # already produces a real fill event with a known, precise expected
    # price/quantity (fill_price_a/shares, both set explicitly by this
    # scenario) -- nothing about the math needs its own broker/stream setup.
    checks.append(Check("position shares/entry_price exactly match the real fill (not just node identity)",
                        pos_a is not None and pos_a['shares'] == shares
                        and abs(pos_a['entry_price'] - fill_price_a) < 0.0005,
                        f"shares={pos_a['shares'] if pos_a else None} expected={shares} "
                        f"entry={pos_a['entry_price'] if pos_a else None} expected={fill_price_a:.4f}"))
    buy_fill_events_a = db.get_coverage_events(scenario_key="buy_fill_reconciled")
    opened_a = [e for e in buy_fill_events_a if e['result'] == 'opened' and e['node_id'] == node_a['id']]
    checks.append(Check("buy_fill_reconciled fired 'opened' for node A with exact shares/price in detail",
                        len(opened_a) == 1 and f"shares={shares:g}" in opened_a[0]['detail']
                        and f"price={fill_price_a:.4f}" in opened_a[0]['detail'],
                        f"events={[(e['result'], e['detail']) for e in buy_fill_events_a]}"))
    checks.append(Check("node B did NOT get node A's fill", pos_b is None))
    checks.append(Check("node C (A's SAME-ACCOUNT sibling) did NOT get node A's fill",
                        db.get_open_position_by_wl_id(node_c['id']) is None))
    checks.append(Check("node A's pending row cleared, B's and C's intact",
                        sorted(p['node']['id'] for p in db.get_pending_buys() if p['ticker'] == TICKER)
                        == sorted([node_b['id'], node_c['id']])))
    stop_orders = [o for o in broker.orders.values()
                   if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                   and o['orderType'] == 'STOP']
    checks.append(Check("real protective STOP placed for node A's position",
                        len(stop_orders) == 1 and stop_orders[0]['account'] == CASH_ALIAS,
                        f"{[(o['account'], o['orderLegCollection'][0]['quantity'], o['stopPrice']) for o in stop_orders]}"))

    # ---------------------------------------------------------------- leg 2
    # Real-shaped in BOTH respects now: raw AccountNumber (not an alias) AND
    # a string SchwabOrderID (order_id_as_string defaults True) -- exactly
    # what a real Schwab ACCT_ACTIVITY message looks like. Pre-fix, this used
    # to raise KeyError inside get_filled_order (AccountNumber defect) and,
    # even if that were bypassed, never confirm (SchwabOrderID defect). Both
    # are now fixed in schwab_client.resolve_account_alias_from_number /
    # signals_notify.drain_fill_queue's _order_id_int reuse -- this leg is
    # the direct, faithful, end-to-end proof.
    fill_price_b = round(price * 0.98, 4)
    broker.force_fill(order_b, fill_price_b)
    say(f"[leg 2] broker filled node B's order {order_b} @ ${fill_price_b:.4f}; "
        f"emitting a real-shaped ACCT_ACTIVITY message (raw AccountNumber, string SchwabOrderID)")
    activity_stream.emit_fill(MARGIN_ACCOUNT_NUMBER, order_b, TICKER, 'BUY', fill_price_b, shares)

    queued = activity_stream.queued_events()
    expected_tuple = (MARGIN_ACCOUNT_NUMBER, TICKER, 'BUY', fill_price_b, float(shares), str(order_b))
    checks.append(Check("real parser decoded the raw envelope into the exact fill tuple",
                        queued == [expected_tuple],
                        f"queued={queued} expected={[expected_tuple]}"))
    health = db.get_coverage_events(scenario_key="stream_message_parsed")
    checks.append(Check("stream parse-health logged 'received' for every content entry",
                        len([e for e in health if e['result'] == 'received']) == 2,
                        f"results={[e['result'] for e in health]}"))
    checks.append(Check("stream parse-health logged 'parsed' for the fill entry",
                        len([e for e in health if e['result'] == 'parsed' and e['ticker'] == TICKER]) == 1))

    try:
        notify.drain_fill_queue()
        observations['drain_with_real_account_number'] = 'completed without raising'
    except Exception as e:
        observations['drain_with_real_account_number'] = f"{type(e).__name__}: {e}"
    say(f"[leg 2] drain_fill_queue() with AccountNumber={MARGIN_ACCOUNT_NUMBER} -> "
        f"{observations['drain_with_real_account_number']}")
    checks.append(Check("drain_fill_queue() did not raise on a real-shaped AccountNumber",
                        observations['drain_with_real_account_number'] == 'completed without raising'))

    fast = db.get_coverage_events(scenario_key="fast_path_fill_reconciliation")
    checks.append(Check("fast path confirmed the fill via a real get_filled_order poll "
                        "(both fixes: account-alias resolution + int order-id coercion)",
                        any(e['result'] == 'confirmed_via_poll' and e['node_id'] == node_b['id'] for e in fast),
                        f"results={[(e['result'], e['node_id']) for e in fast]}"))
    pos_b = db.get_open_position_by_wl_id(node_b['id'])
    checks.append(Check("position opened for node B via the real-shaped stream fast path",
                        pos_b is not None,
                        f"shares={pos_b['shares'] if pos_b else None}"))
    # buy_fill_reconciled -- fill math correctness for node B too, via the
    # fast (stream) path rather than leg 1's slow-poll path, so both real
    # reconciliation entry points are checked, not just one.
    checks.append(Check("node B's shares/entry_price exactly match the real fill via the fast path",
                        pos_b is not None and pos_b['shares'] == shares
                        and abs(pos_b['entry_price'] - fill_price_b) < 0.0005,
                        f"shares={pos_b['shares'] if pos_b else None} expected={shares} "
                        f"entry={pos_b['entry_price'] if pos_b else None} expected={fill_price_b:.4f}"))
    buy_fill_events_b = db.get_coverage_events(scenario_key="buy_fill_reconciled")
    opened_b = [e for e in buy_fill_events_b if e['result'] == 'opened' and e['node_id'] == node_b['id']]
    checks.append(Check("buy_fill_reconciled fired 'opened' for node B with exact shares/price in detail",
                        len(opened_b) == 1 and f"shares={shares:g}" in opened_b[0]['detail']
                        and f"price={fill_price_b:.4f}" in opened_b[0]['detail'],
                        f"events={[(e['result'], e['detail']) for e in buy_fill_events_b]}"))
    checks.append(Check("only node C's pending buy is left (A and B both reconciled)",
                        [p['node']['id'] for p in db.get_pending_buys() if p['ticker'] == TICKER]
                        == [node_c['id']]))
    checks.append(Check("each node's position is attributed to its own account",
                        pos_a is not None and pos_b is not None
                        and pos_a['account'] == CASH_ALIAS and pos_b['account'] == MARGIN_ALIAS,
                        f"A={pos_a['account'] if pos_a else None} B={pos_b['account'] if pos_b else None}"))

    # ---------------------------------------------------------------- leg 3
    # Idempotent-redelivery check: at-least-once stream delivery is a real
    # possibility, not hypothetical. B is already fully reconciled by leg 2
    # (pending row cleared, position open) -- re-emitting the identical
    # real-shaped fill must not raise, must not open a second position, and
    # must not silently vanish. It should land in drain_fill_queue's
    # orphan-fill branch (no matching pending_buys row, ticker still in
    # automation scope) and alert instead.
    say(f"[leg 3] re-emitting the identical real-shaped fill for node B's already-reconciled order "
        f"{order_b} (simulated stream redelivery)")
    activity_stream.emit_fill(MARGIN_ACCOUNT_NUMBER, order_b, TICKER, 'BUY', fill_price_b, shares)
    try:
        notify.drain_fill_queue()
        observations['drain_redelivery'] = 'completed without raising'
    except Exception as e:
        observations['drain_redelivery'] = f"{type(e).__name__}: {e}"
    checks.append(Check("redelivered fill for an already-reconciled order did not raise",
                        observations['drain_redelivery'] == 'completed without raising'))
    orphan = db.get_coverage_events(scenario_key="orphaned_fill_detected")
    checks.append(Check("redelivery landed in the orphan-fill alert branch (no duplicate pending row)",
                        any(e['result'] == 'alerted' and e['ticker'] == TICKER for e in orphan),
                        f"results={[(e['result'], e['ticker']) for e in orphan]}"))
    pos_b_after_redelivery = db.get_open_position_by_wl_id(node_b['id'])
    checks.append(Check("node B still has exactly one position after the redelivered fill",
                        pos_b_after_redelivery is not None
                        and pos_b_after_redelivery['shares'] == pos_b['shares'],
                        f"before={pos_b['shares'] if pos_b else None} "
                        f"after={pos_b_after_redelivery['shares'] if pos_b_after_redelivery else None}"))

    observations['node_a_wl_id'] = node_a['id']
    observations['node_b_wl_id'] = node_b['id']
    observations['node_c_wl_id'] = node_c['id']
    observations['price'] = price
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT ce.scenario_key, ce.mode, ce.ticker, ce.node_id, ce.result, ce.detail, ce.strategy_type,
       wl.account, wl.state
  FROM coverage_events ce
  JOIN watch_list wl ON wl.id = ce.node_id
 WHERE ce.scenario_key = 'buy_fill_reconciles_correct_node'
   AND ce.result = 'resolved'
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly one 'resolved' event, attached
    to a real watch_list node, in a real coverage_events row of the harness DB."""
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['mode'] == 'live' and rows[0]['ticker'] == TICKER
          and rows[0]['account'] == CASH_ALIAS and rows[0]['state'] == 'live'
          and rows[0]['result'] == 'resolved')
    return ok, rows
