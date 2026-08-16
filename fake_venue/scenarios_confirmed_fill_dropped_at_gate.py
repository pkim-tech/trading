"""Phase 2 scenario: `confirmed_fill_dropped_at_gate`, through BOTH real
fill-detection paths (slow poll + fast stream) and the reminder loop that
catches what they both miss.

Real incident this proves against: SOXS/ira/wl_id=206, 2026-08-14. A trailing
BUY filled 09:30:05 ET and sat unreconciled for hours. TICKER was in
AUTOMATION_ENABLED_TICKERS (so the fill-detection code paths run at all) but
NEITHER the ticker-level nor the node-level auto_fill_detection opt-in flag
was set -- both `check_auto_fills` (signals_notify.py, the slow poll) and
`drain_fill_queue` (the fast ACCT_ACTIVITY-stream path) sit behind the
identical `auto_fill_detection_enabled(ticker) AND
node_auto_fill_detection_enabled(wl_id)` gate, so both dropped the same real
fill silently and identically. `drain_fill_queue` already had a loud
`orphaned_fill_detected` alert, but only for the case where NO pending_buys
row exists at all -- the strictly more dangerous case (a real fill for an
order we placed and are STILL actively reminding about) was completely
silent. The only thing still talking about the order was
`check_buy_reminders`, and it kept asserting "still pending" off purely
local, purely price-based state with zero broker re-verification.

FIXED (signals_notify.check_buy_reminders, 2026-08-15): before nagging with
the stale "still pending" text, the reminder loop now re-verifies the order's
real status at the broker (schwab_client.get_filled_order) whenever a real
order_id is on file. A CONFIRMED fill fires a distinct, loud
'confirmed_fill_dropped_at_gate' alert + coverage event instead of the old
reminder. This scenario proves the FULL 3-leg chain against real code paths
(not just direct function calls, per tests/test_fake_broker_
confirmed_fill_dropped_at_gate_scenario.py, which already covers the same
mechanism via fake_broker's shorter-path style) -- most importantly, that the
fast stream path really does still drop it (real ACCT_ACTIVITY parser, real
raw AccountNumber, real string SchwabOrderID) rather than assuming
`drain_fill_queue`'s gate behaves the same as `check_auto_fills`' from
reading the code alone.

Deliberately exempt from has_capital_at_stake (same precedent as
check_addon_buying_power_drift/confirmed_fill_dropped_at_gate's own Grid
notes): a confirmed-but-unreconciled real fill is an infrastructure-
precondition failure, not routine per-node noise. This scenario's node is
sized well under CAPITAL_AT_STAKE_THRESHOLD on purpose (mirrors SOXS's real
$800-scale notional) -- if the re-verification call were gated on that
threshold (as it originally, incorrectly, would have been -- see
signals_notify.py's own comment on why it isn't), this scenario would never
alert.

Shape (one fake node, one fake account -- single order, no cross-node/
cross-account disambiguation question here; that's Phase 1's job):

  node A  (fv_cash)  resting BUY order, deliberately NOT opted into
                      auto_fill_detection at either the ticker or node level
                      (schwab_safety.enable_auto_fill_detection /
                      enable_node_auto_fill_detection are never called --
                      the SOXS precondition, reproduced by omission).

  Leg 1  broker fills A's order IN FULL
         -> notify.check_auto_fills()                  [real slow-poll path]
         -> gate declines (opt-in flags unset) -> silently skipped, no
            position, no alert, no coverage event (the gate itself is mute
            by design -- only the reminder loop is supposed to shout)

  Leg 2  the SAME fill, driven through the REAL STREAM path -- a fake
         ACCT_ACTIVITY message (real-shaped: raw AccountNumber, string
         SchwabOrderID, same fixtures Phase 1 proved the parser against).
         -> schwab_stream._handle_activity_message()   [real parser]
         -> signals_notify.drain_fill_queue()           [real fast path]
         -> a matching pending_buys row DOES exist (order_id-exact match),
            so this is NOT the orphaned-fill branch -- it reaches the SAME
            opt-in gate check_auto_fills uses and is dropped identically
         => coverage_events['fast_path_fill_reconciliation'] =
            'auto_fill_detection_disabled', node_id=A                <-- checked
         => still no position, pending_buys row for A still open      <-- checked

  Leg 3  check_buy_reminders() runs (the only thing left that still talks
         about this order). Re-verifies against the broker via
         schwab_client.get_filled_order and finds the real, confirmed fill
         both prior legs dropped.
         => a distinct 🚨 CONFIRMED FILLED alert, NOT the stale "still
            pending" wording                                          <-- checked
         => coverage_events['confirmed_fill_dropped_at_gate'] = 'alerted',
            node_id=A, detail carrying the real fill price/qty/order_id
                                                                        <-- checked
         => pending_buys row for A is STILL open afterward -- the alert is a
            human-in-the-loop escalation, not an auto-reconciliation; the
            position only opens once a human taps Filled (out of scope for
            this scenario, matching the real incident's own resolution path)
                                                                        <-- checked
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from fake_venue import activity_stream, venue
from fake_venue.scenarios_meta import CASH_ACCOUNT_NUMBER, CASH_ALIAS, PRICE_SOURCE_TICKER, TICKER

FAKE_ACCOUNTS = [
    dict(alias=CASH_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='cash', margin_capable=0),
]
# Deliberately small, mirroring SOXS's real ~$800-scale live notional -- the
# whole point of the scenario is that the re-verification alert fires
# BELOW CAPITAL_AT_STAKE_THRESHOLD, not because of it.
NODE_NOTIONAL = 800
BUY_REMINDER_MINUTES = 15  # matches signals_notify.BUY_REMINDER_MINUTES; kept local so this
                           # scenario's cadence-backdate math doesn't silently drift if that
                           # constant is ever tuned without this file being touched.


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

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version='fake_venue_gatedrop',
                window=20, take_profit=10, stop_loss=1, max_hold_hours=56,
                state='live', account=CASH_ALIAS, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                label='fake-venue harness node (confirmed_fill_dropped_at_gate)')
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
    say(f"[setup] node wl_id={node['id']} ({CASH_ALIAS}), target_notional=${NODE_NOTIONAL} "
        f"(deliberately sub-capital-at-stake)")
    checks.append(Check("node is below has_capital_at_stake's threshold (the exemption this "
                        "scenario exists to prove)", not notify.has_capital_at_stake(node),
                        f"starting_notional={node['starting_notional']}"))

    # THE SOXS PRECONDITION, reproduced by omission: unlike every other
    # fake_venue scenario, neither enable_auto_fill_detection(TICKER) nor
    # enable_node_auto_fill_detection(node['id']) is called here. TICKER is
    # still in AUTOMATION_ENABLED_TICKERS (isolation.configure_env seeds it
    # from scenarios_meta.TICKER unconditionally), so both fill-detection
    # code paths run at all -- they just decline to auto-reconcile, exactly
    # as SOXS's real node did on 2026-08-14.
    checks.append(Check("ticker is in automation scope (so both fill-detection paths actually run, "
                        "rather than being skipped for a different, uninteresting reason)",
                        TICKER in schwab_safety.AUTOMATION_ENABLED_TICKERS))
    checks.append(Check("ticker-level auto_fill_detection is NOT opted in (the SOXS precondition)",
                        not schwab_safety.auto_fill_detection_enabled(TICKER)))
    checks.append(Check("node-level auto_fill_detection is NOT opted in (the SOXS precondition)",
                        not schwab_safety.node_auto_fill_detection_enabled(node['id'])))

    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking schwab_safety._is_trading_day "
            "True for this run (orthogonal to the mechanism under test, same override Phase 2's "
            "post_fill_topup scenario uses)")
        schwab_safety._is_trading_day = lambda date_str: True

    # Entry-side state is SEEDED, not placed through the real BUY path -- same
    # accepted caveat as every other Phase 2 scenario (target is the
    # fill-detection/reminder chain, not entry placement).
    shares = max(int(NODE_NOTIONAL // price), 1)
    sig = {'current_price': price, 'last_bar': datetime.now()}
    order_id = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'BUY',
                                         shares, trail_offset=1.0)
    db.add_pending_buy(node, sig, channel='C0FAKEVENUE', ts='1000.1', order_id=order_id)
    db.mark_pending_buy_placed_by_wl_id(node['id'])
    # Backdated past BUY_REMINDER_MINUTES so check_buy_reminders' own cadence
    # gate (signals_notify.py ~3583) lets this row through on the single poll
    # this scenario runs -- mirrors the real incident, where reminders had
    # already been firing for hours by the time the fill happened.
    stale = (datetime.now() - timedelta(minutes=BUY_REMINDER_MINUTES + 5)).strftime('%Y-%m-%d %H:%M:%S')
    with db._conn() as c:
        c.execute("UPDATE pending_buys SET last_reminder_at=? WHERE wl_id=?", (stale, node['id']))
        c.commit()

    fill_price = round(price * 0.99, 4)
    broker.force_fill(order_id, fill_price)
    say(f"[setup] broker filled node's order {order_id} @ ${fill_price:.4f} for {shares} shares "
        f"(order is now genuinely FILLED at the broker -- both detection paths below are about "
        f"to fail to notice)")

    posted = []

    def _capture(text=None, *a, **kw):
        posted.append(text if text is not None else (a[0] if a else kw.get('text')))
        return ('C0FAKEVENUE', '9999.1')

    notify._post_message = _capture

    # ---------------------------------------------------------------- leg 1
    say("[leg 1] check_auto_fills() -- the slow poll path")
    notify.check_auto_fills([])

    checks.append(Check("leg 1: gate declined -- no position opened",
                        db.get_open_position_by_wl_id(node['id']) is None))
    checks.append(Check("leg 1: gate declined -- pending row still open",
                        db.get_pending_buy_by_wl_id(node['id']) is not None))
    checks.append(Check("leg 1: the gate itself stayed silent (no alert -- the reminder loop's job, "
                        "not check_auto_fills')", posted == [], f"posted={posted}"))

    # ---------------------------------------------------------------- leg 2
    say(f"[leg 2] emitting a real-shaped ACCT_ACTIVITY fill message for the SAME order {order_id} "
        f"and running drain_fill_queue() -- the fast stream path")
    activity_stream.emit_fill(CASH_ACCOUNT_NUMBER, order_id, TICKER, 'BUY', fill_price, shares)

    queued = activity_stream.queued_events()
    expected_tuple = (CASH_ACCOUNT_NUMBER, TICKER, 'BUY', fill_price, float(shares), str(order_id))
    checks.append(Check("real parser decoded the fill envelope correctly",
                        queued == [expected_tuple],
                        f"queued={queued} expected={[expected_tuple]}"))

    try:
        notify.drain_fill_queue()
        observations['drain_fill_queue_result'] = 'completed without raising'
    except Exception as e:
        observations['drain_fill_queue_result'] = f"{type(e).__name__}: {e}"
    say(f"[leg 2] drain_fill_queue() -> {observations['drain_fill_queue_result']}")
    checks.append(Check("drain_fill_queue() did not raise",
                        observations['drain_fill_queue_result'] == 'completed without raising'))

    fast = db.get_coverage_events(scenario_key="fast_path_fill_reconciliation")
    gated = [e for e in fast if e['result'] == 'auto_fill_detection_disabled' and e['node_id'] == node['id']]
    checks.append(Check("leg 2: the fast path found the SAME matching pending row (not the orphan-fill "
                        "branch -- this is the dangerous case, a known order dropped, not an unknown one) "
                        "and hit the identical opt-in gate as leg 1",
                        len(gated) == 1,
                        f"fast_path events={[(e['result'], e['node_id']) for e in fast]}"))
    orphan = db.get_coverage_events(scenario_key="orphaned_fill_detected")
    checks.append(Check("leg 2: did NOT land in the orphaned-fill branch (a matching pending row "
                        "exists -- that branch is for the strictly less-dangerous no-record case)",
                        orphan == [], f"orphan events={[(e['result'], e['detail']) for e in orphan]}"))
    checks.append(Check("leg 2: gate declined -- still no position opened",
                        db.get_open_position_by_wl_id(node['id']) is None))
    checks.append(Check("leg 2: gate declined -- pending row still open",
                        db.get_pending_buy_by_wl_id(node['id']) is not None))
    checks.append(Check("leg 2: the gate itself stayed silent (same as leg 1 -- BOTH real detection "
                        "paths drop this identically, the exact SOXS incident shape)",
                        posted == [], f"posted={posted}"))

    # ---------------------------------------------------------------- leg 3
    say("[leg 3] check_buy_reminders() -- the only thing left that still talks about this order")
    notify.check_buy_reminders()

    checks.append(Check("leg 3: exactly one alert fired", len(posted) == 1, f"posted={posted}"))
    msg = posted[0] if posted else ''
    checks.append(Check("leg 3: alert says CONFIRMED FILLED", 'CONFIRMED FILLED' in msg, msg))
    checks.append(Check("leg 3: alert names the ticker", TICKER in msg, msg))
    checks.append(Check("leg 3: alert carries the real fill price", f"{fill_price:.4f}" in msg, msg))
    checks.append(Check("leg 3: alert carries the real order id", str(order_id) in msg, msg))
    checks.append(Check("leg 3: alert is NOT the old stale 'still pending' wording -- exactly what "
                        "misled the human for hours on 2026-08-14",
                        'still pending' not in msg.lower(), msg))

    events = db.get_coverage_events(scenario_key='confirmed_fill_dropped_at_gate')
    fired = [e for e in events if e['result'] == 'alerted' and e['node_id'] == node['id']
             and e['ticker'] == TICKER]
    checks.append(Check("leg 3: coverage_events['confirmed_fill_dropped_at_gate']='alerted' logged, "
                        "attributed to this node",
                        len(fired) == 1, f"events={[(e['result'], e['node_id'], e['detail']) for e in events]}"))
    if fired:
        checks.append(Check("leg 3: coverage event detail carries the real fill price/qty/order_id "
                            "(not the drop -- the actual broker-confirmed facts)",
                            f"price={fill_price:.4f}" in fired[0]['detail']
                            and f"qty={shares:g}" in fired[0]['detail']
                            and f"order_id={order_id}" in fired[0]['detail'],
                            f"detail={fired[0]['detail']}"))

    checks.append(Check("leg 3: pending row for A is STILL open afterward -- the alert escalates to a "
                        "human, it does not auto-reconcile (a human tapping Filled is out of scope here, "
                        "matching how the real incident actually resolved)",
                        db.get_pending_buy_by_wl_id(node['id']) is not None))
    checks.append(Check("leg 3: still no position opened -- consistent with the alert being an "
                        "escalation, not a silent auto-fix",
                        db.get_open_position_by_wl_id(node['id']) is None))

    observations['node_wl_id'] = node['id']
    observations['price'] = price
    observations['fill_price'] = fill_price
    observations['order_id'] = order_id
    observations['shares'] = shares
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT ce.scenario_key, ce.mode, ce.ticker, ce.node_id, ce.result, ce.detail,
       wl.account, wl.state, wl.starting_notional,
       (SELECT COUNT(*) FROM pending_buys WHERE wl_id = wl.id) AS still_pending,
       (SELECT COUNT(*) FROM open_positions WHERE wl_id = wl.id) AS open_positions,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='fast_path_fill_reconciliation'
         AND result='auto_fill_detection_disabled' AND node_id=wl.id) AS fast_path_dropped,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='orphaned_fill_detected'
         AND node_id=wl.id) AS orphan_alerts
  FROM coverage_events ce
  JOIN watch_list wl ON wl.id = ce.node_id
 WHERE ce.scenario_key = 'confirmed_fill_dropped_at_gate'
   AND ce.result = 'alerted'
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly one 'alerted' event for this
    node, with the fast-path having ALSO independently dropped the same fill
    (fast_path_dropped==1), zero orphan-fill alerts (this is the known-order
    case, not the unknown-order one), the pending row still open (escalation,
    not auto-fix), and no position ever opened -- all read directly from the
    harness DB."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['ticker'] == _ticker and rows[0]['account'] == CASH_ALIAS
          and rows[0]['state'] == 'live' and rows[0]['fast_path_dropped'] == 1
          and rows[0]['orphan_alerts'] == 0 and rows[0]['still_pending'] == 1
          and rows[0]['open_positions'] == 0)
    return ok, rows
