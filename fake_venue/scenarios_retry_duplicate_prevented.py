"""Phase 2 scenario: `order_retry_duplicate_prevented` -- a REPEATED flapping
broker connection (up-down-up mid-order-placement, not just a single clean
drop) must never stack a real duplicate order. Ports the existing fake_broker-
tier proof (tests/test_fake_broker_retry_flapping_scenario.py, direct calls
against schwab_client._submit_order_with_retry/_submit_replace_with_retry)
onto this harness's real wire-format/subprocess-isolated boundary --
place_equity_buy/replace_order_with_stop_loss driven exactly as production
calls them, through venue.install_fake_broker's real schwab-py-shaped
FakeBroker, in the harness's own OS process with its own DB and its own
schwab_safety state dir (fake_broker's pytest-tier proof runs in-process
against the same test suite's DB/state, never isolated this way).

Real code under test: schwab_client._submit_order_with_retry /
_submit_replace_with_retry (_check_broker_before_retry /
_find_recent_matching_order / _get_retry_lock, all added/hardened
2026-08-15/16 -- see scripts/coverage_registry.py's 'order_retry_duplicate_
prevented' row and docs/backlog_cache.md's now-resolved "retry blind, no
broker-state re-check between attempts" item).

Three legs, one fake node/account (this scenario isn't about ticker/account
disambiguation -- fake_venue's Phase 1/`post_fill_topup` already cover that;
it's about the retry loop's own broker-state re-check). Order matters: both
BUY legs (1 and 2) run BEFORE any local position is opened, because
schwab_safety.check_order's 2026-08-02 existing-position guard (BUY blocked
once a local position is on file for this node/ticker/account, unless
is_protective=True) would otherwise reject leg 2's plain BUY outright once
leg 3's position exists -- an unrelated guard, not the mechanism this
scenario targets, so the ordering sidesteps it rather than disabling it.

  LEG 1  place_equity_buy (fresh placement path, _submit_order_with_retry).
         Attempt 1: the request never reaches the broker at all (clean drop).
         Attempt 2: the broker genuinely creates the order, but the local
         response is lost to a second flap. Without the fix, attempt 3 would
         resubmit blind and a SECOND real order would land. With the fix, the
         pre-attempt-3 broker check (_check_broker_before_retry) finds
         attempt 2's real order and returns it instead.
         => exactly 2 real place_order calls, exactly 1 real order at the
            broker                                                  <-- checked
         => coverage_events['order_retry_duplicate_prevented']='prevented'
                                                                      <-- checked

  LEG 2  place_equity_buy again, but this time attempt 1's own broker-landed-
         but-lost-response window ALSO contains a genuinely independent,
         separately-shaped-identically order (e.g. a fast concurrent manual
         resubmission) -- real ambiguity, not something the retry loop's own
         actions could have created (the baseline-order-id snapshot, taken
         once before attempt 1, already rules out anything the loop places
         itself). Must fail safe (raise _AmbiguousBrokerState, halt, no
         further resubmission) rather than guess which of the two real
         orders is "ours".
         => exactly 1 real place_order call (the loop stops at the
            ambiguity check, never fires a 2nd)                     <-- checked
         => coverage_events['order_retry_duplicate_prevented']='ambiguous'
                                                                      <-- checked

  LEG 3  replace_order_with_stop_loss (the atomic-replace path,
         _submit_replace_with_retry) -- a real open position (seeded) + a
         real resting STOP, then the SAME landed-but-lost-response shape on
         the replace call. Proves the fix closes the identical gap on the
         OTHER chokepoint (this loop's own docstring calls out that the
         2026-07-27 single-clean-drop acceptance only ever covered ONE
         attempt, not every attempt -- this leg exercises the now-fixed "any
         attempt, including via a genuinely different code path" case).
         => exactly 1 real replace_order call, exactly 1 live STOP resting
            afterward (the new one; the old one REPLACED)           <-- checked
         => coverage_events['order_retry_duplicate_prevented']='prevented'
                                                                      <-- checked

Entry-side state for leg 3's position is SEEDED (db.open_position), not
placed through the real BUY path -- same accepted caveat as every other
Phase 2 scenario; this scenario's target is the retry/dedup sequence, not
entry placement.
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

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version='fake_venue_retryflap',
                window=20, take_profit=10, stop_loss=FIXED_SL_PCT, max_hold_hours=56,
                state='live', account=CASH_ALIAS, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=FIXED_SL_PCT,
                label='fake-venue harness node (order_retry_duplicate_prevented)')
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

    # Same environmental override every other Phase 2 scenario uses -- the
    # trading-day gate is orthogonal to the retry/dedup mechanism under test,
    # and the harness must be runnable deterministically any day.
    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking schwab_safety._is_trading_day "
            "True for this run (orthogonal to the mechanism under test)")
        schwab_safety._is_trading_day = lambda date_str: True

    # Unlike every other Phase 2 scenario, this one calls the real BUY
    # placement path (place_equity_buy) directly rather than seeding entry-
    # side state -- so it also needs schwab_safety's BUY-only signal-window
    # gate pinned inside a real window, same override
    # test_fake_broker_retry_flapping_scenario.py's own env fixture uses
    # (monkeypatch.setattr(schwab_safety, '_now', lambda: _IN_WINDOW_TIME)).
    # Orthogonal to the retry/dedup mechanism under test -- a scenario run at
    # 7pm shouldn't silently prove nothing just because it's outside the two
    # 15-minute real signal windows.
    orig_now = schwab_safety._now
    schwab_safety._now = lambda: datetime(2026, 7, 29, 10, 30)

    # Real retry-interval delays (2s/attempt) would make this scenario take
    # tens of seconds to prove nothing new -- same override the fake_broker-
    # tier pytest suite uses (monkeypatch.setattr(schwab_client,
    # '_ORDER_SUBMIT_RETRY_INTERVAL_SECS', 0)), just applied and restored by
    # hand since this harness isn't running under pytest.
    orig_retry_interval = schwab_client._ORDER_SUBMIT_RETRY_INTERVAL_SECS
    schwab_client._ORDER_SUBMIT_RETRY_INTERVAL_SECS = 0

    # schwab_safety's OWN recent-orders duplicate-fingerprint guard (ticker+
    # side+quantity match within DUPLICATE_ORDER_WINDOW_SECS, unconditional
    # for both BUY and SELL) is a completely different mechanism from the one
    # this scenario targets -- but this scenario deliberately places several
    # same-ticker/same-side orders in quick succession across its 3 legs
    # (leg 1's real filled BUY, leg 2's own BUY attempt; leg 3's own STOP
    # placement then its own replace), which would otherwise trip on itself.
    # Same override tests/test_fake_broker_retry_flapping_scenario.py's own
    # replace-path test uses, and the same rationale: "keeps this test
    # focused on the retry/dedup logic being proven, not on reproducing that
    # separate guard's own real-world timing."
    orig_dup_window = schwab_safety.DUPLICATE_ORDER_WINDOW_SECS
    schwab_safety.DUPLICATE_ORDER_WINDOW_SECS = 0

    shares = max(int(NODE_NOTIONAL // price), 1)
    real_place_order = broker.place_order
    real_replace_order = broker.replace_order

    try:
        # ============================================================ LEG 1
        say("[leg 1] place_equity_buy: attempt 1 drops clean, attempt 2 lands but the response is "
            "lost, attempt 3 must be prevented by the pre-attempt broker check")
        call_log_1 = []

        def flaky_place_order_leg1(account_hash, order):
            call_log_1.append(1)
            n = len(call_log_1)
            if n == 1:
                raise ConnectionError("simulated flap: request never reached the broker")
            if n == 2:
                real_place_order(account_hash, order)  # broker genuinely creates the order...
                raise ConnectionError("simulated flap: broker received it, response lost")
            return real_place_order(account_hash, order)  # a 3rd call means the fix failed

        broker.place_order = flaky_place_order_leg1
        r, order_id_1 = schwab_client.place_equity_buy(CASH_ALIAS, TICKER, shares, price,
                                                        node_id=node['id'])
        broker.place_order = real_place_order

        checks.append(Check("leg 1: retry loop stopped at 2 real place_order calls "
                            "(drop, then landed-but-lost-response) -- no blind 3rd resubmission",
                            order_id_1 is not None and len(call_log_1) == 2,
                            f"order_id={order_id_1} calls={len(call_log_1)}"))
        placed_1 = _real_orders(broker, TICKER)
        checks.append(Check("leg 1: exactly 1 real order at the broker",
                            len(placed_1) == 1 and placed_1[0]['orderId'] == order_id_1,
                            f"placed={[o['orderId'] for o in placed_1]} expected=[{order_id_1}]"))

        events_1 = [e for e in db.get_coverage_events(scenario_key='order_retry_duplicate_prevented')
                   if e['ticker'] == TICKER and e['node_id'] == node['id']]
        checks.append(Check("leg 1: coverage_events logged 'prevented' for the avoided duplicate",
                            any(e['result'] == 'prevented' for e in events_1),
                            f"events={[(e['result'], e['detail']) for e in events_1]}"))

        # ============================================================ LEG 2
        # Runs BEFORE any local position is opened (see module docstring's
        # ordering note) -- both BUY legs must clear check_order's existing-
        # position guard on their own merits, not because a position happens
        # not to exist yet by accident of file layout.
        say("[leg 2] place_equity_buy again: attempt 1's own window ALSO contains a genuinely "
            "independent concurrent order -- real ambiguity, must fail safe")
        call_log_2 = []

        def flaky_place_order_leg2(account_hash, order):
            call_log_2.append(1)
            real_place_order(account_hash, order)  # our own attempt's real order...
            # ...plus a genuinely separate, concurrent real order landing in the exact same
            # window (e.g. a human's fast manual resubmit) -- not something our own retry
            # loop created, so the baseline-id snapshot (taken before attempt 1) can't have
            # excluded it either.
            broker.seed_resting_order(CASH_ALIAS, TICKER, 'MARKET', 'BUY', shares, status='FILLED')
            raise ConnectionError("simulated flap: broker received it, response lost")

        broker.place_order = flaky_place_order_leg2
        raised = None
        try:
            schwab_client.place_equity_buy(CASH_ALIAS, TICKER, shares, price, node_id=node['id'])
        except schwab_client._AmbiguousBrokerState as e:
            raised = e
        finally:
            broker.place_order = real_place_order

        checks.append(Check("leg 2: retry loop raised _AmbiguousBrokerState rather than guessing "
                            "between the two real candidate orders",
                            raised is not None, f"raised={raised}"))
        checks.append(Check("leg 2: only 1 real placement call happened -- the ambiguity check "
                            "fires before attempt 2 and halts, no further resubmission",
                            len(call_log_2) == 1, f"calls={len(call_log_2)}"))

        events_2 = [e for e in db.get_coverage_events(scenario_key='order_retry_duplicate_prevented')
                   if e['ticker'] == TICKER and e['node_id'] == node['id'] and e['result'] == 'ambiguous']
        checks.append(Check("leg 2: coverage_events logged 'ambiguous' -- the fail-safe halt, "
                            "distinct from a 'prevented' duplicate save",
                            len(events_2) == 1, f"ambiguous_events={len(events_2)}"))

        # ============================================================ LEG 3
        say("[leg 3] replace_order_with_stop_loss: a real open position + resting STOP, then the "
            "same landed-but-lost-response flap on the atomic-replace path")
        db.open_position(node, signal_price=price, signal_time=datetime.now(), entry_price=price,
                         entry_time=datetime.now(), shares=shares)
        stop_price = round(price * (1 - FIXED_SL_PCT / 100), 4)
        r, old_stop_id = schwab_client.place_stop_loss(CASH_ALIAS, TICKER, shares, stop_price,
                                                        node_id=node['id'])
        checks.append(Check("leg 3 setup: original resting STOP placed cleanly (no flap yet)",
                            old_stop_id is not None, f"old_stop_id={old_stop_id}"))

        call_log_3 = []

        def flaky_replace_order_leg3(account_hash, order_id_arg, order):
            call_log_3.append(1)
            if len(call_log_3) == 1:
                real_replace_order(account_hash, order_id_arg, order)  # broker genuinely replaces...
                raise ConnectionError("simulated flap: broker received it, response lost")
            return real_replace_order(account_hash, order_id_arg, order)  # a 2nd call means the fix failed

        broker.replace_order = flaky_replace_order_leg3
        new_stop_price = round(price * (1 - FIXED_SL_PCT * 2 / 100), 4)
        r, new_stop_id = schwab_client.replace_order_with_stop_loss(
            CASH_ALIAS, TICKER, old_stop_id, shares, new_stop_price, node_id=node['id'])
        broker.replace_order = real_replace_order

        checks.append(Check("leg 3: retry loop stopped at 1 real replace_order call -- the "
                            "pre-attempt-2 check found the landed replacement and skipped a 2nd",
                            new_stop_id is not None and len(call_log_3) == 1,
                            f"new_stop_id={new_stop_id} calls={len(call_log_3)}"))
        resting_3 = [o for o in _real_orders(broker, TICKER) if o['status'] == 'WORKING']
        checks.append(Check("leg 3: exactly 1 live STOP resting afterward (the new one; the "
                            "original REPLACED, not left dangling or duplicated)",
                            len(resting_3) == 1 and resting_3[0]['orderId'] == new_stop_id,
                            f"resting={[o['orderId'] for o in resting_3]} expected=[{new_stop_id}]"))
        checks.append(Check("leg 3: original stop order is REPLACED, not still resting",
                            broker.orders[old_stop_id]['status'] == 'REPLACED',
                            f"status={broker.orders[old_stop_id]['status']}"))

        events_3b = [e for e in db.get_coverage_events(scenario_key='order_retry_duplicate_prevented')
                    if e['ticker'] == TICKER and e['node_id'] == node['id'] and e['result'] == 'prevented']
        checks.append(Check("leg 3: coverage_events logged 'prevented' via the replace path "
                            "(a distinct chokepoint from leg 1's fresh-placement path)",
                            len(events_3b) == 2,  # leg 1's + leg 3's own 'prevented' row
                            f"prevented_events={len(events_3b)} (expected 2: leg 1 + leg 3)"))

        observations['node_wl_id'] = node['id']
        observations['price'] = price
        observations['shares'] = shares
        observations['leg1_order_id'] = order_id_1
        observations['leg3_old_stop_id'] = old_stop_id
        observations['leg3_new_stop_id'] = new_stop_id
    finally:
        broker.place_order = real_place_order
        broker.replace_order = real_replace_order
        schwab_client._ORDER_SUBMIT_RETRY_INTERVAL_SECS = orig_retry_interval
        schwab_safety.DUPLICATE_ORDER_WINDOW_SECS = orig_dup_window
        schwab_safety._now = orig_now

    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT wl.id AS wl_id, wl.account,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='order_retry_duplicate_prevented'
         AND result='prevented' AND node_id=wl.id) AS prevented_events,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='order_retry_duplicate_prevented'
         AND result='ambiguous' AND node_id=wl.id) AS ambiguous_events
  FROM watch_list wl
 WHERE wl.ticker = ?
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly one node, with exactly 2
    'prevented' events (leg 1's fresh-placement save + leg 2's replace-path
    save) and exactly 1 'ambiguous' event (leg 3's fail-safe halt), directly
    from the harness DB."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['prevented_events'] == 2 and rows[0]['ambiguous_events'] == 1)
    return ok, rows
