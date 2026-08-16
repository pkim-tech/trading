"""Phase 2 scenario: `dup_order_retry_after_failure` -- schwab_safety.
_broker_confirms_order (schwab_safety.py:1111), called from check_order's
local duplicate-order-window guard (schwab_safety.py:1970) to confirm a local
'recent_orders' fingerprint match against REAL broker state before blocking a
retry as a duplicate.

Weaker/thinner sibling of `order_retry_duplicate_prevented`
(fake_venue/scenarios_retry_duplicate_prevented.py): that scenario proves the
retry LOOP's own pre-attempt broker check (_check_broker_before_retry /
_find_recent_matching_order) never double-places an order across a single
call's own flapping-connection retries. This scenario proves a DIFFERENT,
narrower mechanism: check_order's local duplicate-order-window fingerprint
guard, which runs on EVERY new call (not just retry attempts within one call),
and must tell a genuinely REJECTED prior attempt (retry should be ALLOWED)
apart from a genuinely CONFIRMED prior attempt (retry should be BLOCKED) --
the two branches of schwab_safety.py:1970's `if limits.trading_enabled and
not _broker_confirms_order(...)`.

The only existing coverage for this mechanism
(tests/test_fake_broker_check_order_guards_phase2_scenario.py::
test_dup_order_retry_after_failure_allowed) hand-writes schwab_safety.
STATE_PATH's 'recent_orders' JSON directly and seeds a REJECTED order via
fake_broker.seed_resting_order(status='REJECTED') -- it never drives a REAL
place_equity_buy call through to a real rejection, and it only proves the
ALLOWED branch, not the paired BLOCKED contrast. This scenario closes both
gaps: leg 1's rejection comes from the real place_equity_buy/
_post_order_confirmation/OrderRejected chain (broker.force_reject_next_order,
the same real-observed-shape helper `order_retry_duplicate_prevented`'s test
suite established: HTTP-level success, async status poll finds REJECTED --
schwab_client.py's 2026-07-24 real incident), and leg 3 proves the mechanism
actually discriminates by attempting a second genuine duplicate once a real
order IS confirmed at the broker, not just that it never blocks anything.

One fake node/account.

  LEG 1  place_equity_buy with broker.force_reject_next_order('REJECTED') set
         -- approve_and_record's local 'recent_orders' record is written
         BEFORE the broker call (as in production), then the real order is
         created at the broker but resolves REJECTED via the real async
         status-poll path (_confirm_order_status), raising OrderRejected.
         => a real order exists at the broker, status=REJECTED       <-- checked
         => place_equity_buy raised (OrderRejected)                  <-- checked

  LEG 2  place_equity_buy again, SAME (account, ticker, side, quantity),
         immediately after (well within DUPLICATE_ORDER_WINDOW_SECS, left at
         its real default -- unlike order_retry_duplicate_prevented, this
         scenario's target IS the window guard, so it can't be zeroed out).
         check_order's local fingerprint check finds leg 1's recent_orders
         entry, but _broker_confirms_order(...) finds only a REJECTED order
         for this ticker (excluded by _DUPLICATE_NOT_CONFIRMED_STATUSES) --
         not a confirmed duplicate, so the retry is ALLOWED and a genuine
         second order is placed and fills.
         => coverage_events['dup_order_retry_after_failure']='allowed_retry'
                                                                       <-- TARGET
         => a second real order exists at the broker (distinct id from leg 1,
            status FILLED -- a plain MARKET buy, no reject queued this time)
                                                                       <-- checked

  LEG 3  place_equity_buy a THIRD time, same shape, immediately after leg 2.
         This time the fingerprint match is against leg 2's own order, which
         is genuinely FILLED (confirmed) at the broker -- _broker_confirms_
         order must now return True, and the retry is correctly BLOCKED as a
         real duplicate (SafetyViolation, 'dup_order_window_blocked'). The
         contrast this scenario's first run had to actually prove: the
         mechanism isn't just "always allow the second call" -- it tells the
         two real broker outcomes apart.
         => place_equity_buy raised SafetyViolation                  <-- checked
         => coverage_events['dup_order_window_blocked'] fired         <-- checked
         => no third real order reaches the broker (blocked before
            placement)                                                <-- checked

DUPLICATE_ORDER_WINDOW_SECS is deliberately left at its real default (60s,
not zeroed) -- the opposite of order_retry_duplicate_prevented's override,
since this scenario's whole point is exercising that window's real guard,
not sidestepping it.
"""
from dataclasses import dataclass
from datetime import datetime

from fake_venue import venue
from fake_venue.scenarios_meta import CASH_ALIAS, PRICE_SOURCE_TICKER, TICKER

FAKE_ACCOUNTS = [
    dict(alias=CASH_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='cash', margin_capable=0),
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


def _add_node():
    import signals_db as db

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version='fake_venue_dupretry',
                window=20, take_profit=10, stop_loss=1, max_hold_hours=56,
                state='live', account=CASH_ALIAS, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                label='fake-venue harness node (dup_order_retry_after_failure)')
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]


def _real_orders(broker, ticker):
    return [o for o in broker.orders.values()
            if o['orderLegCollection'][0]['instrument']['symbol'] == ticker]


def run(price=None, verbose=True):
    """Runs the scenario against the already-isolated, already-imported
    environment. Returns (checks, observations)."""
    import schwab_client
    import schwab_safety
    import signals_db as db

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
    price = venue.seed_quote(broker, TICKER, price, price_source_ticker=PRICE_SOURCE_TICKER)
    broker.set_cash_balance(CASH_ALIAS, 100_000.0)
    say(f"[setup] {TICKER} quote seeded at ${price:.4f}")

    node = _add_node()
    say(f"[setup] node wl_id={node['id']} ({CASH_ALIAS}), target_notional=${NODE_NOTIONAL}")

    # Same environmental overrides every other Phase 2 scenario placing a real
    # BUY uses -- both orthogonal to the mechanism under test.
    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking schwab_safety._is_trading_day "
            "True for this run (orthogonal to the mechanism under test)")
        schwab_safety._is_trading_day = lambda date_str: True

    orig_now = schwab_safety._now
    schwab_safety._now = lambda: datetime(2026, 7, 29, 10, 30)

    orig_retry_interval = schwab_client._ORDER_SUBMIT_RETRY_INTERVAL_SECS
    schwab_client._ORDER_SUBMIT_RETRY_INTERVAL_SECS = 0

    shares = max(int(NODE_NOTIONAL // price), 1)

    try:
        # ==================================================== leg 1
        say("[leg 1] place_equity_buy with a queued real REJECTED status (HTTP-level success, "
            "async status poll confirms REJECTED) -- the local 'recent_orders' record is still "
            "written first, exactly as production's approve_and_record does")
        broker.force_reject_next_order('REJECTED')
        leg1_exc = None
        try:
            schwab_client.place_equity_buy(CASH_ALIAS, TICKER, shares, price, node_id=node['id'])
        except Exception as e:
            leg1_exc = e
        checks.append(Check("leg 1: place_equity_buy raised (OrderRejected -- a confirmed real "
                            "rejection, not a successful placement)",
                            leg1_exc is not None and type(leg1_exc).__name__ == 'OrderRejected',
                            f"raised={type(leg1_exc).__name__ if leg1_exc else None}: {leg1_exc}"))

        placed_1 = _real_orders(broker, TICKER)
        checks.append(Check("leg 1: exactly 1 real order at the broker, status REJECTED",
                            len(placed_1) == 1 and placed_1[0]['status'] == 'REJECTED',
                            f"orders={[(o['orderId'], o['status']) for o in placed_1]}"))

        # ==================================================== leg 2
        say("[leg 2] place_equity_buy again, same shape, immediately -- the local fingerprint "
            "check finds leg 1's record, but the real broker only shows a REJECTED order for "
            "this ticker, so _broker_confirms_order must say 'not confirmed' and allow the retry")
        leg2_exc = None
        try:
            r, order_id_2 = schwab_client.place_equity_buy(CASH_ALIAS, TICKER, shares, price,
                                                             node_id=node['id'])
        except Exception as e:
            leg2_exc = e
            order_id_2 = None
        checks.append(Check("leg 2: retry after a real rejection was ALLOWED (no exception, a real "
                            "order_id returned)",
                            leg2_exc is None and order_id_2 is not None,
                            f"raised={leg2_exc} order_id={order_id_2}"))

        events_2 = [e for e in db.get_coverage_events(scenario_key='dup_order_retry_after_failure')
                   if e['ticker'] == TICKER and e['node_id'] == node['id']]
        checks.append(Check("leg 2: coverage_events logged 'allowed_retry' -- the real "
                            "REJECTED-doesn't-count-as-confirmed branch",
                            any(e['result'] == 'allowed_retry' for e in events_2),
                            f"events={[(e['result'], e['detail']) for e in events_2]}"))

        placed_2 = _real_orders(broker, TICKER)
        checks.append(Check("leg 2: a second, distinct real order now exists at the broker "
                            "(the allowed retry actually reached place_order)",
                            len(placed_2) == 2 and order_id_2 in [o['orderId'] for o in placed_2],
                            f"orders={[(o['orderId'], o['status']) for o in placed_2]} "
                            f"expected new id={order_id_2}"))
        leg2_order = next((o for o in placed_2 if o['orderId'] == order_id_2), None)
        checks.append(Check("leg 2: the allowed-retry order is genuinely FILLED at the broker "
                            "(a real, confirmed placement -- not another rejection)",
                            leg2_order is not None and leg2_order['status'] == 'FILLED',
                            f"status={leg2_order['status'] if leg2_order else None}"))

        # ==================================================== leg 3
        say("[leg 3] place_equity_buy a THIRD time, same shape, immediately -- this time the "
            "fingerprint match is against leg 2's genuinely FILLED order, so the retry must be "
            "correctly BLOCKED as a real duplicate, not allowed through again")
        leg3_exc = None
        try:
            schwab_client.place_equity_buy(CASH_ALIAS, TICKER, shares, price, node_id=node['id'])
        except Exception as e:
            leg3_exc = e
        checks.append(Check("leg 3: place_equity_buy raised SafetyViolation -- the mechanism "
                            "discriminates a confirmed prior order from a rejected one, rather "
                            "than always allowing the next call through",
                            leg3_exc is not None and type(leg3_exc).__name__ == 'SafetyViolation',
                            f"raised={type(leg3_exc).__name__ if leg3_exc else None}: {leg3_exc}"))

        blocked_3 = [e for e in db.get_coverage_events(scenario_key='dup_order_window_blocked')
                    if e['ticker'] == TICKER and e['node_id'] == node['id']]
        checks.append(Check("leg 3: coverage_events logged 'dup_order_window_blocked' -- the "
                            "confirmed-duplicate branch, distinct from leg 2's allowed_retry",
                            len(blocked_3) == 1,
                            f"events={[(e['result'], e['detail']) for e in blocked_3]}"))

        placed_3 = _real_orders(broker, TICKER)
        checks.append(Check("leg 3: still only 2 real orders at the broker -- the block happened "
                            "before a 3rd place_order call, no unwanted 3rd real order placed",
                            len(placed_3) == 2,
                            f"orders={[(o['orderId'], o['status']) for o in placed_3]}"))

        observations['node_wl_id'] = node['id']
        observations['price'] = price
        observations['shares'] = shares
        observations['leg1_order_id'] = placed_1[0]['orderId'] if placed_1 else None
        observations['leg2_order_id'] = order_id_2
    finally:
        schwab_client._ORDER_SUBMIT_RETRY_INTERVAL_SECS = orig_retry_interval
        schwab_safety._now = orig_now

    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT wl.id AS wl_id, wl.account,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='dup_order_retry_after_failure'
         AND result='allowed_retry' AND node_id=wl.id) AS allowed_retry_events,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='dup_order_window_blocked'
         AND node_id=wl.id) AS blocked_events
  FROM watch_list wl
 WHERE wl.ticker = ?
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly one node, with exactly 1
    'allowed_retry' event (leg 2, after a real rejection) and exactly 1
    'dup_order_window_blocked' event (leg 3, the paired contrast against a
    genuinely confirmed order), directly from the harness DB."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['allowed_retry_events'] == 1 and rows[0]['blocked_events'] == 1)
    return ok, rows
