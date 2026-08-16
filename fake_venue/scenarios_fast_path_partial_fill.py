"""Phase 2 scenario: `fast_path_fill_reconciliation`, multi-execution partial
fill (the `leaves_quantity` progression Phase 1's emitter built but never
drove -- see fake_venue/activity_stream.py::build_fill_message_data's own
docstring: "driving a partial-fill leg through the real reconciliation path
is the next scenario to write, not built in Phase 1").

`drain_fill_queue` (signals_notify.py:4556) is explicit, in its own
docstring, about exactly the hazard this scenario proves closed: a real
Schwab ExecutionActivity message can represent ONE PARTIAL EXECUTION of a
still-filling order -- multiple partial fills can arrive within the same
second for a liquid ticker -- and locking in whichever partial quantity
happens to be parsed first would both under-record real share count AND let
_reconcile_fill's top-up logic place a real SECOND buy order to "correct" a
fill that was never actually final. The function's stated defense is to
never trust the stream message's own price/quantity at all, using it only as
a wake-up signal and re-confirming via a get_filled_order poll that only
returns non-None once the order's broker-side status is the terminal
'FILLED'. This scenario is the first real exercise of that specific claim
end-to-end -- Phase 1's scenario only ever emitted a single, already-terminal
fill message (leaves_quantity=0 the whole time).

Shape (one fake node, one fake account -- like post_fill_topup, this isn't
about ticker/account disambiguation, Phase 1 already covers that; it's about
one order's fill progressing through 2 executions before going terminal):

  node A  (fv_cash)  resting BUY order for the FULL target share count,
                      still WORKING at the fake broker.

  Leg 1  a PARTIAL execution arrives over the stream -- a real-shaped
         ACCT_ACTIVITY message with leaves_quantity > 0 (ExecutionQuantity
         well short of the order's own Quantity; LegSubStatus
         'LegSubStatusPartiallyFilled', LegStatus 'LegOpen' -- see
         activity_stream.build_fill_message_data). The fake broker's own
         order status is DELIBERATELY left WORKING (force_fill is not called
         yet) -- this models the real situation the docstring describes: the
         stream told us *something* filled, but the order itself is not
         actually done.
         -> schwab_stream._handle_activity_message()   [real parser]
         -> signals_notify.drain_fill_queue()           [real fast path]
         -> get_filled_order(order_id=...) polls the broker, finds status
            still WORKING -> returns None every attempt
         => coverage_events['fast_path_fill_reconciliation'] =
            'stream_event_not_yet_confirmed_filled'                <-- checked
         => NO position opened, NO top-up placed, pending_buys row for node A
            still intact -- the exact double-buy hazard the docstring warns
            about, proven NOT to fire on a partial                 <-- checked

  Leg 2  the order's REMAINING quantity actually fills at the broker (a
         second, terminal execution) -- force_fill() drives the fake
         broker's own order to status='FILLED' with the order's FULL
         quantity (matching a real aggregated fill), at a price distinct
         from leg 1's partial execution price (so a check that used leg 1's
         stream price/quantity by mistake would be caught). A second
         real-shaped ACCT_ACTIVITY message (leaves_quantity=0, this leg's own
         ExecutionQuantity -- the remainder, not the full order) drives the
         real parser and fast path again.
         -> drain_fill_queue() polls again; the order is now FILLED, so
            get_filled_order returns the broker's own aggregated fill
            {price, quantity} -- NOT leg 1's or leg 2's own stream-carried
            price/quantity, which is the entire point being proven.
         => coverage_events['fast_path_fill_reconciliation'] =
            'confirmed_via_poll'                                    <-- checked
         => position opens for the FULL order quantity at the broker's own
            aggregated fill price (matches neither leg's partial numbers)
                                                                      <-- checked
         => coverage_events['buy_fill_reconciled'] = 'opened', with
            shares/price matching the broker's aggregated fill exactly
                                                                      <-- checked
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
# The leg-1 partial execution covers well under half the order -- deliberately
# far from the eventual full quantity so a bug that locked in this leg's
# numbers would be obviously wrong, not a rounding-adjacent near-miss.
PARTIAL_FRACTION = 0.4


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

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version='fake_venue_partial',
                window=20, take_profit=10, stop_loss=1, max_hold_hours=56,
                state='live', account=CASH_ALIAS, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                label='fake-venue harness node (fast_path_partial_fill)')
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

    # Same environmental fake as post_fill_topup -- orthogonal to the
    # mechanism under test (the fast-path/poll reconciliation, not the
    # calendar gate), needed so the harness is runnable deterministically any
    # day. This scenario's only real BUY-path traffic (the entry order) is
    # seeded directly, not placed through check_order, so the gate is not
    # actually exercised either way -- kept for parity/documentation with the
    # other Phase 2 scenario.
    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking schwab_safety._is_trading_day "
            "True for this run (see module docstring: orthogonal to the mechanism under test)")
        schwab_safety._is_trading_day = lambda date_str: True

    full_shares = max(int(NODE_NOTIONAL // price), 3)
    partial_shares = max(int(full_shares * PARTIAL_FRACTION), 1)
    remainder_shares = full_shares - partial_shares
    checks.append(Check("partial leg is a genuine fraction of the full order, not the whole thing",
                        0 < partial_shares < full_shares,
                        f"partial_shares={partial_shares} full_shares={full_shares}"))

    sig = {'current_price': price, 'last_bar': datetime.now()}
    order_id = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'BUY',
                                         full_shares, trail_offset=1.0)
    db.add_pending_buy(node, sig, channel=None, ts=None, order_id=order_id)
    db.mark_pending_buy_placed_by_wl_id(node['id'])
    say(f"[setup] resting order {order_id} for {full_shares} shares, status=WORKING at the fake broker")

    # ---------------------------------------------------------------- leg 1
    # A partial execution over the stream -- the order itself is deliberately
    # NOT force_fill()'d yet, so the fake broker's own status stays WORKING.
    # partial_price is well off the eventual aggregated fill price, so a bug
    # that locked in this leg's stream-carried price would be caught by the
    # final entry_price assertion below.
    partial_price = round(price * 0.995, 4)
    say(f"[leg 1] emitting a PARTIAL fill stream message for order {order_id}: "
        f"{partial_shares}/{full_shares} shares @ ${partial_price:.4f} (order still WORKING at broker)")
    activity_stream.emit_fill(CASH_ACCOUNT_NUMBER, order_id, TICKER, 'BUY', partial_price, partial_shares,
                              leaves_quantity=float(remainder_shares))

    queued = activity_stream.queued_events()
    expected_partial_tuple = (CASH_ACCOUNT_NUMBER, TICKER, 'BUY', partial_price, float(partial_shares),
                              str(order_id))
    checks.append(Check("real parser decoded the partial-execution envelope correctly",
                        queued == [expected_partial_tuple],
                        f"queued={queued} expected={[expected_partial_tuple]}"))

    checks.append(Check("order is still WORKING at the fake broker before leg 1's drain "
                        "(the real hazard: broker hasn't actually finished filling)",
                        broker.orders[order_id]['status'] == 'WORKING',
                        f"status={broker.orders[order_id]['status']}"))

    try:
        notify.drain_fill_queue()
        observations['drain_partial_fill'] = 'completed without raising'
    except Exception as e:
        observations['drain_partial_fill'] = f"{type(e).__name__}: {e}"
    say(f"[leg 1] drain_fill_queue() for the partial fill -> {observations['drain_partial_fill']}")
    checks.append(Check("drain_fill_queue() did not raise on a partial-execution stream event",
                        observations['drain_partial_fill'] == 'completed without raising'))

    fast_after_leg1 = db.get_coverage_events(scenario_key="fast_path_fill_reconciliation")
    unconfirmed = [e for e in fast_after_leg1 if e['result'] == 'stream_event_not_yet_confirmed_filled'
                  and e['node_id'] == node['id']]
    checks.append(Check("fast path did NOT confirm the fill from the partial's stream event alone "
                        "(get_filled_order polled and found the order still WORKING, not FILLED)",
                        len(unconfirmed) == 1,
                        f"events={[(e['result'], e['node_id']) for e in fast_after_leg1]}"))

    pos_after_leg1 = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("NO position opened off the partial execution alone -- the exact "
                        "under-record/premature-top-up hazard drain_fill_queue's docstring warns about",
                        pos_after_leg1 is None,
                        f"pos={pos_after_leg1}"))
    checks.append(Check("node's pending_buys row is still intact after the partial "
                        "(not consumed by an unconfirmed stream event)",
                        any(p['node']['id'] == node['id'] for p in db.get_pending_buys()
                            if p['ticker'] == TICKER)))

    topup_after_leg1 = db.get_coverage_events(scenario_key="top_up")
    checks.append(Check("no top-up branch fired off the partial (nothing to top up -- no position "
                        "was ever opened for it to under-size)",
                        not any(e['node_id'] == node['id'] for e in topup_after_leg1)))

    # ---------------------------------------------------------------- leg 2
    # The order's remainder actually fills at the broker -- force_fill drives
    # status to FILLED with the order's FULL quantity, at a price distinct
    # from leg 1's partial (models real aggregation across executions: Schwab
    # reports one blended/VWAP price for the whole order once terminal, not
    # each leg's own execution price).
    final_price = round(price * 0.99, 4)
    broker.force_fill(order_id, final_price)
    checks.append(Check("order is FILLED at the broker for its full quantity after the remainder executes",
                        broker.orders[order_id]['status'] == 'FILLED',
                        f"status={broker.orders[order_id]['status']}"))

    say(f"[leg 2] emitting the TERMINAL fill stream message for order {order_id}: "
        f"{remainder_shares} more shares @ ${final_price:.4f} (leaves_quantity=0, order now FILLED)")
    activity_stream.emit_fill(CASH_ACCOUNT_NUMBER, order_id, TICKER, 'BUY', final_price, remainder_shares,
                              leaves_quantity=0.0, order_quantity=full_shares)

    queued2 = activity_stream.queued_events()
    expected_terminal_tuple = (CASH_ACCOUNT_NUMBER, TICKER, 'BUY', final_price, float(remainder_shares),
                               str(order_id))
    checks.append(Check("real parser decoded the terminal-execution envelope correctly",
                        queued2 == [expected_terminal_tuple],
                        f"queued={queued2} expected={[expected_terminal_tuple]}"))

    try:
        notify.drain_fill_queue()
        observations['drain_terminal_fill'] = 'completed without raising'
    except Exception as e:
        observations['drain_terminal_fill'] = f"{type(e).__name__}: {e}"
    say(f"[leg 2] drain_fill_queue() for the terminal fill -> {observations['drain_terminal_fill']}")
    checks.append(Check("drain_fill_queue() did not raise on the terminal fill event",
                        observations['drain_terminal_fill'] == 'completed without raising'))

    fast_after_leg2 = db.get_coverage_events(scenario_key="fast_path_fill_reconciliation")
    confirmed = [e for e in fast_after_leg2 if e['result'] == 'confirmed_via_poll' and e['node_id'] == node['id']]
    checks.append(Check("fast path confirmed via poll once the broker's own order status went FILLED",
                        len(confirmed) == 1,
                        f"events={[(e['result'], e['node_id']) for e in fast_after_leg2]}"))
    if confirmed:
        checks.append(Check("confirmed event reports the broker's AGGREGATED price/quantity "
                            "(full order), not either leg's own stream-carried numbers",
                            f"price={final_price:.4f}" in (confirmed[0]['detail'] or '')
                            and f"qty={full_shares:g}" in (confirmed[0]['detail'] or ''),
                            f"detail={confirmed[0]['detail']}"))

    pos_after_leg2 = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("position opened for the FULL order quantity, not leg 1's partial "
                        f"({partial_shares}) or leg 2's own execution-only remainder ({remainder_shares})",
                        pos_after_leg2 is not None and pos_after_leg2['shares'] == full_shares,
                        f"shares={pos_after_leg2['shares'] if pos_after_leg2 else None} expected={full_shares}"))
    checks.append(Check("entry_price matches the broker's aggregated FILLED price, not leg 1's "
                        f"partial price (${partial_price:.4f})",
                        pos_after_leg2 is not None
                        and abs(pos_after_leg2['entry_price'] - final_price) < 0.0005,
                        f"entry_price={pos_after_leg2['entry_price'] if pos_after_leg2 else None} "
                        f"expected={final_price:.4f} (partial was {partial_price:.4f})"))

    checks.append(Check("node's pending_buys row cleared after the confirmed terminal fill",
                        not any(p['node']['id'] == node['id'] for p in db.get_pending_buys()
                                if p['ticker'] == TICKER)))

    buy_events = db.get_coverage_events(scenario_key="buy_fill_reconciled")
    opened = [e for e in buy_events if e['result'] == 'opened' and e['node_id'] == node['id']]
    checks.append(Check("buy_fill_reconciled fired 'opened' for the aggregated terminal fill",
                        len(opened) == 1,
                        f"events={[(e['result'], e['detail']) for e in buy_events]}"))
    if opened:
        checks.append(Check("buy_fill_reconciled's own detail reports the aggregated shares/price "
                            "exactly (fill-math correctness, not just node identity)",
                            f"shares={full_shares:g}" in opened[0]['detail']
                            and f"price={final_price:.4f}" in opened[0]['detail'],
                            f"detail={opened[0]['detail']}"))

    observations['node_wl_id'] = node['id']
    observations['price'] = price
    observations['full_shares'] = full_shares
    observations['partial_shares'] = partial_shares
    observations['remainder_shares'] = remainder_shares
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT ce.scenario_key, ce.mode, ce.ticker, ce.node_id, ce.result, ce.detail, ce.strategy_type,
       ce.id, wl.account, wl.state
  FROM coverage_events ce
  JOIN watch_list wl ON wl.id = ce.node_id
 WHERE ce.scenario_key = 'fast_path_fill_reconciliation'
   AND ce.result IN ('stream_event_not_yet_confirmed_filled', 'confirmed_via_poll')
 ORDER BY ce.id ASC
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires BOTH the unconfirmed-partial event and
    the confirmed-via-poll event, in that order, attached to the same real
    watch_list node, in the harness DB."""
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 2
          and rows[0]['result'] == 'stream_event_not_yet_confirmed_filled'
          and rows[1]['result'] == 'confirmed_via_poll'
          and rows[0]['node_id'] == rows[1]['node_id']
          and rows[0]['mode'] == 'live' and rows[1]['mode'] == 'live'
          and rows[0]['account'] == CASH_ALIAS and rows[1]['account'] == CASH_ALIAS
          and rows[0]['state'] == 'live' and rows[1]['state'] == 'live')
    return ok, rows
