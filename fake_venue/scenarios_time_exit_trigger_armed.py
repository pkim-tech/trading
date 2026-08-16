"""Phase 2 scenario: `time_exit_trigger_armed`, proving the hold-time-forced
force-replace branch of `_attempt_automated_exit_sell` (signals_notify.py:217)
still atomically replaces the ARM-TIME trailing-sell with a market sell and
correctly closes the position, end to end through the real production
entrypoint (`notify_sell_signal`, not the helper called in isolation).

Real incident this proves against: SH, 2026-07-29 -- an armed position
(trail_state.trailing=True) whose hold time expired sat stuck for hours on a
50%-wide resting trailing-sell order that was never going to fire on its own
before the natural hold-time deadline, because the exit machinery treated a
genuine trail-stop breach and hold-time-expired-while-armed as the same case.
Fixed via `state['exit_forced_by_hold_time']` (all 3 trailing-exit strategy
classes) plus the force-replace branch in `_attempt_automated_exit_sell`
(the code this scenario now proves against a real broker, not a mock).

Grid row (scripts/coverage_registry.py, id='time_exit_trigger_armed'): real
live proof exists (GDXU, 2026-08-07, position_id=71) but that's the only one
on file, and it predates this harness -- this scenario gives a repeatable,
deterministic proof of the same mechanism against a controlled broker.

Shape (one fake node, one fake account):

  Setup   node armed: trail_state = {trailing: True, peak: ...,
          exit_forced_by_hold_time: True} -- the position has already been
          through arm (notify_trailing_activated / _attempt_automated_sell,
          not re-exercised here, see automated_sell_execution's own
          scenario) and its trail_state.exit_order_id / open_positions.
          sl_order_id both point at a WIDE, still-resting TRAILING_STOP SELL
          order (the literal SH shape: an order nowhere near firing on its
          own) seeded directly at the broker.

  Run     the real `signals_notify.notify_sell_signal(pos, 'TIME', ...)` --
          the actual production entrypoint check_sell_condition's TIME
          branch calls, not `_attempt_automated_exit_sell` in isolation.
          -> logs coverage_events['time_exit_trigger_armed']='alert_fired'
             (this scenario's OWN target row)                       <-- checked
          -> `_attempt_automated_exit_sell` sees hold_time_forced=True and
             NOT yet replaced (state.hold_time_replaced unset) -> resolves
             resting_order_id to the ARM-TIME trailing-sell (not sl_order_id
             directly -- they're the same id here, matching what a real
             arm event leaves behind), labels it "trailing-sell"
          -> `_verify_resting_before_replace` (round-trip 1) finds it
             genuinely resting, correctly sized -- silent, no
             replace_target_mismatch (nothing has drifted)          <-- checked
          -> atomic replace (schwab_client.replace_equity_order_with_market)
             swaps it for a real MARKET SELL
          => coverage_events['automated_exit_execution']='placed',
             detail containing reason=TIME                          <-- checked
          => the old wide trailing-sell is REPLACED, not still resting
                                                                      <-- checked
          => the new MARKET SELL is FILLED immediately (FakeBroker fills a
             MARKET order same-tick, matching real same-tick behavior)
                                                                      <-- checked
          -> notify_sell_signal's own short bounded poll
             (get_filled_order by exact order_id) confirms the fill on the
             FIRST attempt (no need to wait out the full poll budget) and
             closes the position automatically -- no manual Slack tap
             needed, closing the exact "stuck for hours" gap SH hit
          => open_positions row for this node is gone (position closed)
                                                                      <-- checked
          => trade_log's closed row has exit_reason='TIME'           <-- checked
          => no manual_sl_fallback_alert (nothing failed)            <-- checked

Entry-side + arm-side state is SEEDED (sl_order_id / trail_state / the
resting trailing-sell order inserted directly), matching every other Phase 2
scenario's accepted caveat -- automated_sell_execution's own scenario proves
the ARM event itself; this scenario's target is what happens to an already-
armed position once hold-time forces the exit.
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
# Deliberately wide -- mirrors the real SH incident's ~50%-wide trail, an
# order that structurally cannot fire on its own before hold-time forces the
# exit. The exact width doesn't matter to the mechanism under test (the
# force-replace doesn't check how far the order is from triggering), but a
# tight trail would risk FakeBroker's advance_price ever firing it
# incidentally and confusing what closed the position.
WIDE_TRAIL_PCT = 50.0


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

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version='fake_venue_timeexitarmed',
                window=20, take_profit=10, stop_loss=FIXED_SL_PCT, max_hold_hours=56,
                state='live', account=CASH_ALIAS, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=FIXED_SL_PCT,
                label='fake-venue harness node (time_exit_trigger_armed)')
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
    price = venue.seed_quote(broker, TICKER, price, price_source_ticker=PRICE_SOURCE_TICKER)
    broker.set_cash_balance(CASH_ALIAS, 100_000.0)
    say(f"[setup] {TICKER} quote seeded at ${price:.4f}")

    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking schwab_safety._is_trading_day "
            "True for this run (orthogonal to the mechanism under test, same override every other "
            "Phase 2 scenario uses)")
        schwab_safety._is_trading_day = lambda date_str: True

    posted = []

    def _capture(text=None, *a, **kw):
        posted.append(text if text is not None else (a[0] if a else kw.get('text')))
        return ('C0FAKEVENUE', '9999.1')

    # Both bindings needed -- schwab_client.py imported _post_message by name
    # at import time, a separate local binding from signals_notify's/
    # signals_blocks' own (same gotcha scenarios_replace_target_mismatch.py
    # documents).
    import schwab_client
    notify._post_message = _capture
    schwab_client._post_message = _capture

    node = _add_node()
    say(f"[setup] node wl_id={node['id']} ({CASH_ALIAS}), fixed_sl={FIXED_SL_PCT}%")

    shares = max(int(NODE_NOTIONAL // price), 1)
    now = datetime.now()
    db.open_position(node, signal_price=price, signal_time=now, entry_price=price,
                      entry_time=now, shares=shares)
    pos = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("setup: position opened", pos is not None))

    # Seed the ARM-TIME state directly: an already-armed position whose
    # trail_state.exit_order_id points at a wide, still-resting trailing-sell
    # -- exactly what a real arm event (_attempt_automated_sell, proven by
    # its own scenario) would have left behind, and open_positions.
    # sl_order_id repointed to match (that function's own success path does
    # this unconditionally -- see its 2026-08-07 comment).
    arm_order_id = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'SELL', shares,
                                              trail_offset=WIDE_TRAIL_PCT)
    db.set_sl_order_id_by_position(pos['id'], arm_order_id)
    db.update_position_trail_state(pos['id'], {
        'trailing': True, 'peak': price * 1.05, 'exit_forced_by_hold_time': True,
        'exit_order_id': arm_order_id,
    })
    pos = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("setup: position is armed with exit_forced_by_hold_time=True and "
                        "exit_order_id pointing at the wide resting trailing-sell",
                        pos['trail_state'].get('exit_forced_by_hold_time') is True
                        and pos['trail_state'].get('exit_order_id') == arm_order_id
                        and pos['sl_order_id'] == arm_order_id,
                        f"trail_state={pos['trail_state']} sl_order_id={pos['sl_order_id']}"))
    checks.append(Check("setup: the arm-time order is a wide TRAILING_STOP SELL, resting",
                        broker.orders[arm_order_id]['orderType'] == 'TRAILING_STOP'
                        and broker.orders[arm_order_id]['status'] == 'WORKING',
                        f"order={broker.orders[arm_order_id]}"))

    orders_before = set(broker.orders)
    say(f"[run] calling the real signals_notify.notify_sell_signal(pos, 'TIME', ...) -- hold-time "
        f"forced the exit while armed, mirroring the exact SH 2026-07-29 shape")
    notify.notify_sell_signal(pos, 'TIME', current_price=price, target_price=price * 0.98)

    time_events = db.get_coverage_events(scenario_key="time_exit_trigger_armed")
    fired = [e for e in time_events if e['result'] == 'alert_fired' and e['node_id'] == node['id']]
    checks.append(Check("time_exit_trigger_armed fired 'alert_fired' -- this scenario's own target row",
                        len(fired) == 1, f"events={[(e['result'], e['node_id']) for e in time_events]}"))

    mismatch_events = [e for e in db.get_coverage_events(scenario_key='replace_target_mismatch')
                       if e['node_id'] == node['id']]
    checks.append(Check("round-trip 1 (_verify_resting_before_replace) found nothing to say -- "
                        "the arm-time order genuinely matched, no drift",
                        mismatch_events == [], f"events={[(e['result']) for e in mismatch_events]}"))

    exec_events = db.get_coverage_events(scenario_key="automated_exit_execution")
    placed = [e for e in exec_events if e['result'] == 'placed' and e['node_id'] == node['id']
              and 'reason=TIME' in (e['detail'] or '')]
    checks.append(Check("automated_exit_execution fired 'placed' for reason=TIME",
                        len(placed) == 1, f"events={[(e['result'], e['detail']) for e in exec_events]}"))

    checks.append(Check("the wide arm-time trailing-sell is now REPLACED, not still resting",
                        broker.orders[arm_order_id]['status'] == 'REPLACED',
                        f"status={broker.orders[arm_order_id]['status']}"))

    new_orders = set(broker.orders) - orders_before
    market_sells = [oid for oid in new_orders
                    if broker.orders[oid]['orderType'] == 'MARKET'
                    and broker.orders[oid]['orderLegCollection'][0]['instruction'] == 'SELL']
    checks.append(Check("exactly one new MARKET SELL order placed, replacing the wide trail",
                        len(market_sells) == 1,
                        f"new_orders={sorted(new_orders)} market_sells={market_sells}"))
    if market_sells:
        checks.append(Check("the new MARKET SELL is FILLED (same-tick, matching real MARKET order "
                            "behavior) -- this is what let notify_sell_signal's own poll confirm and "
                            "auto-close without a manual Slack tap",
                            broker.orders[market_sells[0]]['status'] == 'FILLED',
                            f"status={broker.orders[market_sells[0]]['status']}"))

    pos_after = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("position is no longer open -- auto-closed via the confirmed broker fill, "
                        "not left stuck waiting on a manual tap (the exact SH-shaped gap this "
                        "scenario proves against)",
                        pos_after is None, f"pos_after={pos_after}"))

    trades = db.get_trade_log_for_wl_id(node['id'])
    closed = [t for t in trades if t.get('exit_reason') == 'TIME']
    checks.append(Check("trade_log's closed row for this node has exit_reason='TIME'",
                        len(closed) == 1, f"trades={[(t.get('id'), t.get('exit_reason')) for t in trades]}"))

    fallback_events = [e for e in db.get_coverage_events(scenario_key='manual_sl_fallback_alert')
                       if e['node_id'] == node['id']]
    checks.append(Check("no manual_sl_fallback_alert fired -- nothing failed, this was a clean "
                        "automated replace+confirm",
                        fallback_events == []))

    checks.append(Check("no UNPROTECTED alert posted",
                        not any('UNPROTECTED' in p for p in posted), f"posted={posted}"))

    observations['node_wl_id'] = node['id']
    observations['price'] = price
    observations['shares'] = shares
    observations['arm_order_id'] = arm_order_id
    observations['posted'] = posted
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT wl.id AS wl_id,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='time_exit_trigger_armed'
         AND result='alert_fired' AND node_id=wl.id) AS armed_time_exit_alerts,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='automated_exit_execution'
         AND result='placed' AND node_id=wl.id) AS exit_placed,
       (SELECT COUNT(*) FROM trade_log WHERE wl_id=wl.id AND exit_reason='TIME') AS time_closes,
       (SELECT COUNT(*) FROM open_positions WHERE wl_id=wl.id) AS still_open
  FROM watch_list wl
 WHERE wl.ticker = ?
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly one node, with exactly one
    time_exit_trigger_armed alert, one placed automated_exit_execution, one
    TIME-reason closed trade_log row, and zero still-open positions --
    directly from the harness DB."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['armed_time_exit_alerts'] == 1
          and rows[0]['exit_placed'] == 1 and rows[0]['time_closes'] == 1
          and rows[0]['still_open'] == 0)
    return ok, rows
