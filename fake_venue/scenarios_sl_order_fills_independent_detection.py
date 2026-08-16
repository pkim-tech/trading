"""Phase 2 scenario: `sl_order_fills_independent_detection`, end to end
through a REAL protective-stop placement and a broker-side fill our code
never touches.

Real incident this proves against (docs/deep_backlog.md, "Resolved
2026-08-07 (evening)"): LABD (`soxl_ira`), a 1-share hair-trigger
(fixed_sl=0.3%) position, had its real stop fill ~7 minutes after entry --
but `check_own_sell_fills`/`check_auto_fills` only ever polled
`trail_state.exit_pending.order_id`, an id that only exists once OUR
bar-close signal check has already computed an exit and called
`_attempt_automated_exit_sell`. Nothing polled `open_positions.sl_order_id`
directly. The position sat stuck open LOCALLY for 8+ hours; every retry
against the now-terminal order 400'd and posted a false "UNPROTECTED --
place a stop-loss manually" alert on an already-safely-closed position.

Fixed via `signals_notify.check_sl_order_fills` (signals_notify.py ~3254),
wired into `active_signals.py`'s `run_loop` at line ~1181 -- BEFORE the
bar-close exit scan, and NOT gated to the 10:25-10:40/15:25-15:40 signal
windows the way `check_sell_condition`/`notify_sell_signal` are. It runs
every single poll cycle, independent of whether a signal window is even
open. `tests/test_fake_broker_sl_order_fills_scenario.py` already regression-
tests the function's own branch logic directly (5 tests: no-exit-pending
close, TRAIL-vs-SL labeling by order identity not the `trailing` flag,
exit_pending dedup, qty-mismatch alert-not-close) -- this scenario proves
something those tests don't: the REAL placement path
(`signals_notify._place_stop_loss_for_position` -> `schwab_client.
place_stop_loss` -> a genuine broker STOP order) is what actually sets
`sl_order_id`, not a hand-seeded row, and the whole sequence runs under the
fake-venue isolation tripwire (no accidental production DB/broker touch).

Shape (one fake node, one fake account):

  Setup   a real position is opened (seeded, like the sibling scenarios --
          entry-side isn't this scenario's target) with a hair-trigger
          fixed_sl, matching LABD's real config shape.

  Leg 1   the REAL protective stop is placed via
          `signals_notify._place_stop_loss_for_position` -- exercises the
          genuine `schwab_client.place_stop_loss` call, a real broker STOP
          order lands in FakeBroker's order book, and `open_positions.
          sl_order_id` is set by the real production write
          (`db.set_sl_order_id_by_position`), not test scaffolding.
          => coverage_events['sl_placement'] = 'placed'            <-- checked

  Leg 2   the stop fires ON ITS OWN at the broker -- `FakeBroker.
          advance_price`'s own resting-STOP auto-trigger, mirroring a real
          stop firing continuously between polls, with ZERO involvement
          from any of our code (no `check_sell_condition`, no bar-close
          scan, no signal window is ever consulted anywhere in this
          scenario -- that absence IS the point: the LABD incident happened
          precisely because nothing else was watching this order). The
          position is asserted still open LOCALLY at this instant --
          reproducing the exact undetected-fill window the incident hinged
          on -- before the poll below ever runs.

  Leg 3   `signals_notify.check_sl_order_fills` (the real function, called
          exactly as `active_signals.py`'s `run_loop` calls it every cycle)
          detects the fill and closes the position -- proving detection
          here comes from the STANDALONE poll of `sl_order_id`, not from any
          bar-close-computed `exit_pending` (asserted empty/absent
          throughout leg 2, so leg 3's close can't be attributed to that
          mechanism).
          => coverage_events['automated_exit_confirmed'] =
             'closed_via_sl_order_poll', detail contains
             'via_sl_order_poll=1'                                 <-- TARGET
          => trade_log gets exit_reason='SL' at the real fill price
          => open_positions row is gone -- no 8-hour-stuck-open window
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from fake_venue import venue
from fake_venue.scenarios_meta import CASH_ALIAS, PRICE_SOURCE_TICKER, TICKER

FAKE_ACCOUNTS = [
    dict(alias=CASH_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='cash', margin_capable=0),
]
NODE_NOTIONAL = 2_000
# Matches LABD's real config shape (fixed_sl=0.3%, the incident's own
# hair-trigger setup) rather than a generic round number -- a wider SL would
# still exercise the same code path, but this keeps the scenario faithful to
# the real thing it's reproducing.
FIXED_SL_PCT = 0.3


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

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version='fake_venue_sl_poll',
                window=20, take_profit=10, stop_loss=1, max_hold_hours=56,
                state='live', account=CASH_ALIAS, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=FIXED_SL_PCT,
                label='fake-venue harness node (sl_order_fills_independent_detection)')
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]


def run(price=None, verbose=True):
    """Runs the scenario against the already-isolated, already-imported
    environment. Returns (checks, observations)."""
    import schwab_client
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

    node = _add_node()
    say(f"[setup] node wl_id={node['id']} ({CASH_ALIAS}), fixed_sl={FIXED_SL_PCT}% "
        f"(matches LABD's real hair-trigger config)")

    # Entry-side state is SEEDED, not placed through the real BUY path -- same
    # accepted caveat as the sibling Phase 2 scenarios (this scenario's target
    # is the protective-stop placement + independent fill-detection chain, not
    # entry placement itself).
    entry_time = datetime.now() - timedelta(hours=1)
    entry_price = price
    shares = max(int(NODE_NOTIONAL // price), 1)
    opened = db.open_position(node, signal_price=price, signal_time=entry_time,
                              entry_price=entry_price, entry_time=entry_time, shares=shares)
    checks.append(Check("real position opened for the node", opened))
    pos = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("position visible via get_open_position_by_wl_id", pos is not None))
    checks.append(Check("position has no sl_order_id yet (placement hasn't run)",
                        pos is not None and pos.get('sl_order_id') is None))

    # ---------------------------------------------------------------- leg 1
    # The REAL production placement path -- not a hand-seeded resting order.
    say(f"[leg 1] calling the real signals_notify._place_stop_loss_for_position(node, {TICKER!r})")
    notify._place_stop_loss_for_position(node, TICKER)

    sl_events = db.get_coverage_events(scenario_key="sl_placement")
    placed = [e for e in sl_events if e['result'] == 'placed' and e['node_id'] == node['id']]
    checks.append(Check("_place_stop_loss_for_position's real placement fired 'placed'",
                        len(placed) == 1,
                        f"events={[(e['result'], e['detail']) for e in sl_events]}"))

    pos = db.get_open_position_by_wl_id(node['id'])
    sl_order_id = pos.get('sl_order_id') if pos else None
    checks.append(Check("open_positions.sl_order_id set by the real placement write "
                        "(db.set_sl_order_id_by_position, not test scaffolding)",
                        sl_order_id is not None,
                        f"sl_order_id={sl_order_id}"))

    stop_orders = [o for oid, o in broker.orders.items()
                   if oid == sl_order_id and o['orderType'] == 'STOP']
    checks.append(Check("a genuine broker STOP order exists for sl_order_id, "
                        "sized for the position's real share count",
                        len(stop_orders) == 1 and stop_orders[0]['orderLegCollection'][0]['quantity'] == shares,
                        f"stop_orders={[(o['orderType'], o['stopPrice'], o['orderLegCollection'][0]['quantity']) for o in stop_orders]}"))
    stop_price = stop_orders[0]['stopPrice'] if stop_orders else None
    expected_stop_price = round(entry_price * (1 - FIXED_SL_PCT / 100), 4)
    checks.append(Check("stop is anchored to entry_price * (1 - fixed_sl%), matching "
                        "strategies.py's own SL check exactly",
                        stop_price is not None and abs(stop_price - expected_stop_price) < 0.01,
                        f"stop_price={stop_price} expected~={expected_stop_price}"))

    # ---------------------------------------------------------------- leg 2
    # The stop fires ON ITS OWN at the broker -- zero involvement from any of
    # our code. No check_sell_condition, no bar-close scan, no signal-window
    # check anywhere in this scenario. This is the exact undetected-fill
    # window LABD sat in for 8+ hours: the order is FILLED at the broker but
    # nothing local has noticed yet.
    fill_price = round(stop_price * 0.999, 4)
    say(f"[leg 2] broker fires the resting STOP on its own (advance_price to ${fill_price:.4f}, "
        f"no code of ours involved)")
    broker.advance_price(TICKER, last=fill_price, bid=fill_price, ask=fill_price)
    checks.append(Check("the stop is FILLED at the broker",
                        broker.orders[sl_order_id]['status'] == 'FILLED',
                        f"status={broker.orders[sl_order_id]['status']}"))

    pos_before_poll = db.get_open_position_by_wl_id(node['id'])
    exit_pending_before = ((pos_before_poll or {}).get('trail_state') or {}).get('exit_pending')
    checks.append(Check("position is STILL OPEN locally -- the fill is real but undetected "
                        "(reproduces LABD's exact stuck-open window)",
                        pos_before_poll is not None,
                        f"pos={'present' if pos_before_poll else None}"))
    checks.append(Check("no exit_pending exists -- proves the eventual close below can't be "
                        "attributed to the bar-close-only exit_pending mechanism (never computed here)",
                        not exit_pending_before,
                        f"exit_pending={exit_pending_before}"))

    # ---------------------------------------------------------------- leg 3
    # The real, standalone poll -- called exactly as active_signals.py's
    # run_loop calls it every cycle, BEFORE the bar-close exit scan (which
    # this scenario deliberately never invokes at all).
    say("[leg 3] calling the real signals_notify.check_sl_order_fills([pos]) -- "
        "the standalone poll, independent of any bar-close/signal-window check")
    notify.check_sl_order_fills([pos_before_poll])

    pos_after_poll = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("position closed by the independent poll -- no more 8-hour stuck-open window",
                        pos_after_poll is None))

    with db._conn() as c:
        rows = c.execute(
            "SELECT exit_reason, exit_price, wl_id, account FROM trade_log WHERE ticker=?",
            (TICKER,)).fetchall()
    checks.append(Check("exactly one trade_log row, closed", len(rows) == 1,
                        f"rows={rows}"))
    if rows:
        exit_reason, exit_price, wl_id, account = rows[0]
        checks.append(Check("exit_reason='SL' (order identity != exit_order_id, and "
                            "no exit_forced_by_hold_time -- a genuine stop breach)",
                            exit_reason == 'SL', f"exit_reason={exit_reason}"))
        checks.append(Check("exit_price matches the broker's real fill price, not a "
                            "theoretical/target price",
                            exit_price is not None and abs(exit_price - fill_price) < 0.0005,
                            f"exit_price={exit_price} fill_price={fill_price}"))
        checks.append(Check("trade_log row attributed to the real node/account",
                            wl_id == node['id'] and account == CASH_ALIAS,
                            f"wl_id={wl_id} account={account}"))

    close_events = db.get_coverage_events(scenario_key="automated_exit_confirmed")
    closed_via_poll = [e for e in close_events
                       if e['result'] == 'closed_via_sl_order_poll' and e['node_id'] == node['id']
                       and 'via_sl_order_poll=1' in (e['detail'] or '')]
    checks.append(Check("automated_exit_confirmed fired 'closed_via_sl_order_poll' with the "
                        "distinguishing detail marker (not one of the 2 sibling call sites "
                        "sharing this scenario_key -- coverage_registry.py's bad_results "
                        "depends on this exact result string)",
                        len(closed_via_poll) == 1,
                        f"events={[(e['result'], e['detail']) for e in close_events]}"))

    observations['node_wl_id'] = node['id']
    observations['price'] = price
    observations['stop_price'] = stop_price
    observations['fill_price'] = fill_price
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT tl.ticker, tl.exit_reason, tl.exit_price, tl.wl_id, tl.account, wl.state,
       (SELECT COUNT(*) FROM open_positions WHERE wl_id = tl.wl_id) AS still_open_positions,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='automated_exit_confirmed'
         AND result='closed_via_sl_order_poll' AND node_id=tl.wl_id) AS sl_poll_close_events
  FROM trade_log tl
  JOIN watch_list wl ON wl.id = tl.wl_id
 WHERE tl.ticker = ?
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly one closed trade_log row, with
    exit_reason='SL', zero still-open positions for the node, and exactly one
    'closed_via_sl_order_poll' coverage_events row -- directly from the
    harness DB."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['exit_reason'] == 'SL'
          and rows[0]['still_open_positions'] == 0 and rows[0]['sl_poll_close_events'] == 1
          and rows[0]['state'] == 'live')
    return ok, rows
