"""Phase 2 scenario: `canary_full_lifecycle` (real canary letter A, IVV/SPXU in
production -- scripts/coverage_registry.py's `canary_full_lifecycle` row,
CLAUDE.md's "Canary A-F design restored" section), end to end through the
REAL execution chain -- not the daily A/B canary's own bar-close signal scan
(that's `active_signals._scan_buy_signals`/`check_sell_condition`, which needs
real hourly OHLC history from `_load_cache` and isn't faithfully fakeable
without a synthetic price series -- same boundary every other fake_venue
scenario already draws: the SIGNAL is seeded, the EXECUTION MECHANISM that
fires once the signal exists is what's driven for real).

The Grid row's own `code_path` says "a full-daemon regression, not one
function" -- this scenario is the first fake_venue proof of the WHOLE
entry->arm->exit chain in one run, not a single reconciliation leg the way
`post_fill_topup`/`sl_sync_placement` are. Three stages, each handing off to
the real production function the real canary's bar-close poll would call at
that exact point in the sequence (active_signals.py:670-683):

  Stage 1 (entry fill)
    node seeded with a resting BUY order (same pattern as every other Phase 2
    scenario) -> broker fills it -> notify.check_auto_fills() [real slow-poll
    reconciliation path]
    -> _reconcile_buy_fill opens the position
    -> ticker is in AUTOMATION_ENABLED_TICKERS, so _place_stop_loss_for_position
       fires too -- a real protective STOP order rests at the broker before
       any arm/exit logic runs, exactly like a real entry
    => coverage_events['buy_fill_reconciled'] = 'opened'
    => coverage_events['sl_placement'] = 'placed'

  Stage 2 (arm)
    Mirrors what `active_signals.py`'s poll loop does immediately before
    calling `notify_trailing_activated`: `check_sell_condition` would have
    already persisted `trail_state={'trailing': True, 'peak': ...}` to the DB
    -- seeded here directly (this scenario's one deliberate seed, matching
    the module's own comment: "check_sell_condition already persisted the
    newly-armed state... pos here is still the pre-arm in-memory copy the
    caller passed in") -- then `notify.notify_trailing_activated(pos, cp)` is
    called with the SAME pre-arm-in-memory-copy shape active_signals.py
    itself passes (fetched right after Stage 1, before the trail_state seed
    above). This is real code doing real work: `_attempt_automated_sell`
    atomically REPLACES the resting protective STOP with a real TRAILING_STOP
    SELL order (the mechanism this canary's hair-trigger `trail_pct`/`arm_
    sell_pct`=0.1% exists to force same-day), then re-reads the position
    fresh and persists `exit_order_id`/`order_placed` onto the real
    just-armed trail_state -- the exact clobber-avoidance fix this function's
    own docstring describes (2026-07-22 CRITICAL incident).
    => coverage_events['automated_sell_execution'] = 'placed'
    => open_positions.sl_order_id repointed at the new TRAILING_STOP order
    => the original STOP order is dead (broker-side CANCELED via the atomic
       replace, not a separate cancel_order call)

  Stage 3 (trail-stop breach -> auto-close)
    Broker fills the TRAILING_STOP order from Stage 2 (a real trailing-stop
    breach, mirroring what would happen if price pulled back past the armed
    peak by `trail_pct`) -> `notify.notify_sell_signal(fresh_pos, 'TRAIL', cp,
    target)` is called with the SAME post-arm fresh-read shape active_signals.py
    itself passes (`_stash_exit_decision_bar` in production; a plain fresh
    read here since exit_decision_bar isn't part of what this scenario proves).
    `_attempt_automated_exit_sell` resolves reason='TRAIL'+not hold_time_forced
    to `state['exit_order_id']` (the Stage 2 order) rather than placing a
    redundant order -- `notify_sell_signal`'s own short bounded poll
    (`get_filled_order`, same `_GAP_FILL_POLL_ATTEMPTS` pattern check_gap_resize
    uses) then confirms the fill that was already forced, closes the position
    for real (`db.close_position(..., exit_reason='TRAIL')`), and posts the
    auto-closed alert -- no manual Slack tap anywhere in this chain.
    => coverage_events['automated_exit_confirmed'] = 'closed'
    => trade_log row written with exit_reason='TRAIL', a real closed trade

This is deliberately NOT a claim that the daily bar-close signal-computation
side of canary A is proven here -- that's what the real daily canary node
itself (and coverage_check.py's trade_lifecycle check) still does. What this
closes is the much larger unproven surface: given a real armed position, does
the WHOLE downstream mechanical chain (replace-to-trailing at arm, replace-
to-trailing reuse at exit, atomic order replace, fill polling, position
close, trade_log write) actually work end to end through real code against a
real (fake) broker -- previously only ever exercised piecemeal by unit tests
mocking one function call at a time.
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
# Matches the real IVV/SPXU canary A config exactly (scripts/add_canary_nodes.py):
# fixed_sl=30% (practically unreachable), arm(take_profit)=0.1% and trail_pct=0.1%
# both hair-trigger -- forces every state transition to fire, same as the real node.
FIXED_SL_PCT = 30.0
ARM_PCT = 0.1
TRAIL_PCT = 0.1


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

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version='fake_venue_canary_a',
                window=5, take_profit=ARM_PCT, stop_loss=0, max_hold_hours=48,
                state='live', account=CASH_ALIAS, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=0.1, trail_pct=TRAIL_PCT, fixed_sl_override=FIXED_SL_PCT,
                entry_timing='close', label='fake-venue harness node (canary_full_lifecycle)')
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
    say(f"[setup] node wl_id={node['id']} ({CASH_ALIAS}), fixed_sl={FIXED_SL_PCT}% "
        f"arm={ARM_PCT}% trail={TRAIL_PCT}% (matches real IVV/SPXU canary A config)")

    schwab_safety.enable_auto_fill_detection(TICKER)
    schwab_safety.enable_node_auto_fill_detection(node['id'])

    # Same rationale as every sibling Phase 2 scenario: check_order's BUY-only
    # trading-day gate is orthogonal to the mechanism under test here (arm/exit
    # replace-and-fill, not entry placement's own calendar gate), and the
    # harness must be runnable deterministically any day.
    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking schwab_safety._is_trading_day "
            "True for this run (see module docstring: orthogonal to the mechanism under test)")
        schwab_safety._is_trading_day = lambda date_str: True

    # ---------------------------------------------------------------- stage 1
    # Entry-side state is SEEDED, not placed through the real BUY path -- same
    # accepted caveat as every sibling scenario (this scenario's target is
    # arm->exit, not entry-signal computation or entry placement itself).
    shares = max(int(NODE_NOTIONAL // price), 1)
    sig = {'current_price': price, 'last_bar': datetime.now()}
    order_id = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'BUY',
                                         shares, trail_offset=0.1)
    db.add_pending_buy(node, sig, channel=None, ts=None, order_id=order_id)
    db.mark_pending_buy_placed_by_wl_id(node['id'])

    fill_price = round(price * 0.99, 4)
    broker.force_fill(order_id, fill_price)
    say(f"[stage 1] broker filled entry order {order_id} @ ${fill_price:.4f}; running check_auto_fills()")
    notify.check_auto_fills([])

    pos_after_entry = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("position opened for the entry fill", pos_after_entry is not None,
                        f"shares={pos_after_entry['shares'] if pos_after_entry else None}"))
    checks.append(Check("entry order's pending row cleared",
                        [p for p in db.get_pending_buys() if p['ticker'] == TICKER] == []))
    buy_events = db.get_coverage_events(scenario_key="buy_fill_reconciled")
    checks.append(Check("buy_fill_reconciled fired 'opened'",
                        any(e['result'] == 'opened' and e['node_id'] == node['id'] for e in buy_events),
                        f"results={[(e['result'], e['node_id']) for e in buy_events]}"))

    sl_events = db.get_coverage_events(scenario_key="sl_placement")
    placed_sl = [e for e in sl_events if e['result'] == 'placed' and e['node_id'] == node['id']]
    checks.append(Check("_place_stop_loss_for_position fired 'placed' at entry, before any arm/exit "
                        "logic runs -- a real protective stop rests at the broker exactly like a real "
                        "entry, same as the daily canary's actual sequence",
                        len(placed_sl) == 1,
                        f"events={[(e['result'], e['detail']) for e in sl_events]}"))
    entry_sl_order_id = pos_after_entry.get('sl_order_id') if pos_after_entry else None
    checks.append(Check("open_positions.sl_order_id set by the real entry-time SL placement",
                        entry_sl_order_id is not None, f"sl_order_id={entry_sl_order_id}"))

    # ---------------------------------------------------------------- stage 2
    # Mirrors active_signals.py:670-673 exactly: check_sell_condition would
    # have already persisted the armed trail_state before calling
    # notify_trailing_activated with the STILL-PRE-ARM in-memory pos copy
    # (fetched right after stage 1, before this seed write) -- see this
    # module's docstring for why that ordering matters (the 2026-07-22
    # CRITICAL clobber incident notify_trailing_activated's own docstring
    # describes).
    entry_price = pos_after_entry['entry_price']
    arm_price = round(entry_price * (1 + ARM_PCT / 100 + 0.0005), 4)  # crosses the 0.1% arm threshold
    db.update_position_trail_state(pos_after_entry['id'], {'trailing': True, 'peak': arm_price})
    # Represents real elapsed time between entry and arm -- see
    # venue.age_recent_order_records's docstring. Without this, the entry-time
    # SL placement (a SELL) and the arm-time trailing-sell replace (also a
    # SELL, same qty) land inside schwab_safety's 60s dup-order window purely
    # because this scenario drives both stages back-to-back in one process;
    # real production always has >=POLL_SECS(300s) between them.
    venue.age_recent_order_records(70)
    say(f"[stage 2] seeding armed trail_state (trailing=True, peak=${arm_price:.4f}) -- the state "
        f"check_sell_condition would have just persisted -- then running the real "
        f"notify.notify_trailing_activated(pos, cp) with the pre-arm-shaped pos copy")
    orders_before_arm = set(broker.orders)
    notify.notify_trailing_activated(pos_after_entry, arm_price)

    sell_exec_events = db.get_coverage_events(scenario_key="automated_sell_execution")
    placed_trail = [e for e in sell_exec_events if e['result'] == 'placed' and e['node_id'] == node['id']]
    checks.append(Check("_attempt_automated_sell (via notify_trailing_activated) fired "
                        "automated_sell_execution='placed' -- the real broker-side arm replace",
                        len(placed_trail) == 1,
                        f"events={[(e['result'], e['detail']) for e in sell_exec_events]}"))

    pos_after_arm = db.get_position_by_id(pos_after_entry['id'])
    trail_state_after_arm = pos_after_arm.get('trail_state') or {} if pos_after_arm else {}
    checks.append(Check("armed trail_state survived notify_trailing_activated's own re-read/merge "
                        "(the exact clobber this function's docstring describes fixing) -- trailing "
                        "still True, not reset",
                        bool(trail_state_after_arm.get('trailing')),
                        f"trail_state={trail_state_after_arm}"))
    checks.append(Check("trail_state.order_placed=True and a real exit_order_id recorded",
                        trail_state_after_arm.get('order_placed') is True
                        and trail_state_after_arm.get('exit_order_id') is not None,
                        f"order_placed={trail_state_after_arm.get('order_placed')} "
                        f"exit_order_id={trail_state_after_arm.get('exit_order_id')}"))
    trail_order_id = trail_state_after_arm.get('exit_order_id')

    checks.append(Check("open_positions.sl_order_id repointed at the new TRAILING_STOP order "
                        "(not left pointing at the now-dead original STOP)",
                        pos_after_arm is not None and pos_after_arm.get('sl_order_id') == trail_order_id
                        and trail_order_id != entry_sl_order_id,
                        f"sl_order_id={pos_after_arm.get('sl_order_id') if pos_after_arm else None} "
                        f"expected={trail_order_id} original_stop={entry_sl_order_id}"))

    new_orders = set(broker.orders) - orders_before_arm
    trailing_sell_orders = [oid for oid in new_orders
                            if broker.orders[oid]['orderType'] == 'TRAILING_STOP'
                            and broker.orders[oid]['orderLegCollection'][0]['instruction'] == 'SELL']
    checks.append(Check("exactly one new broker TRAILING_STOP SELL order placed for the arm "
                        "(the atomic replace-with-trailing-sell)",
                        len(trailing_sell_orders) == 1 and trailing_sell_orders[0] == trail_order_id,
                        f"new_orders={sorted(new_orders)} trailing_sell_orders={trailing_sell_orders} "
                        f"expected_id={trail_order_id}"))
    original_stop_order = broker.orders.get(entry_sl_order_id) if entry_sl_order_id else None
    checks.append(Check("the original protective STOP order is no longer WORKING at the broker "
                        "(atomic replace canceled it, not a separate cancel_order call)",
                        original_stop_order is not None and original_stop_order['status'] != 'WORKING',
                        f"status={original_stop_order['status'] if original_stop_order else None}"))

    # ---------------------------------------------------------------- stage 3
    # A real trailing-stop breach: broker fills the arm-time order, mirroring
    # what a real pullback past the armed peak by trail_pct would produce.
    exit_fill_price = round(arm_price * (1 - TRAIL_PCT / 100 - 0.0005), 4)
    broker.force_fill(trail_order_id, exit_fill_price)
    say(f"[stage 3] broker filled the trailing-stop order {trail_order_id} @ ${exit_fill_price:.4f} "
        f"(real breach); running the real notify.notify_sell_signal(fresh_pos, 'TRAIL', cp, target)")

    fresh_pos = db.get_position_by_id(pos_after_arm['id'])
    checks.append(Check("fresh_pos re-read carries the armed trail_state before notify_sell_signal "
                        "is called -- mirrors active_signals.py's own fresh-read-before-notify pattern",
                        bool((fresh_pos.get('trail_state') or {}).get('trailing')) if fresh_pos else False))
    target = fresh_pos['entry_price'] * (1 - TRAIL_PCT / 100) if fresh_pos else None
    notify.notify_sell_signal(fresh_pos, 'TRAIL', exit_fill_price, target)

    exit_confirmed_events = db.get_coverage_events(scenario_key="automated_exit_confirmed")
    closed_events = [e for e in exit_confirmed_events if e['result'] == 'closed' and e['node_id'] == node['id']]
    checks.append(Check("automated_exit_confirmed fired 'closed' -- the real fill-confirmed auto-close, "
                        "no manual Slack tap anywhere in this chain",
                        len(closed_events) == 1,
                        f"events={[(e['result'], e['detail']) for e in exit_confirmed_events]}"))
    checks.append(Check("position no longer open", db.get_open_position_by_wl_id(node['id']) is None))

    trades = db.get_closed_trades_for_ticker_on_date(TICKER, datetime.now().strftime('%Y-%m-%d'),
                                                       wl_id=node['id'])
    checks.append(Check("trade_log has exactly one closed trade for this node today, exit_reason='TRAIL' "
                        "-- exactly what the real daily canary's coverage_check.py trade_lifecycle "
                        "expectation checks for (canary_full_lifecycle, expect_exit_reason=['TRAIL'])",
                        len(trades) == 1 and trades[0]['exit_reason'] == 'TRAIL',
                        f"trades={[(t.get('exit_reason'), t.get('exit_price')) for t in trades]}"))

    observations['node_wl_id'] = node['id']
    observations['price'] = price
    observations['shares'] = shares
    observations['entry_sl_order_id'] = entry_sl_order_id
    observations['trail_order_id'] = trail_order_id
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT tl.wl_id, tl.exit_reason, tl.entry_price, tl.exit_price, wl.account, wl.state,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='sl_placement' AND result='placed'
         AND node_id=tl.wl_id) AS sl_placed_events,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='automated_sell_execution'
         AND result='placed' AND node_id=tl.wl_id) AS arm_events,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='automated_exit_confirmed'
         AND result='closed' AND node_id=tl.wl_id) AS exit_events
  FROM trade_log tl
  JOIN watch_list wl ON wl.id = tl.wl_id
 WHERE tl.ticker = ?
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly one closed trade with
    exit_reason='TRAIL', a real entry-time SL placement event, a real arm
    event, and a real exit-confirm event, all attached to the same node --
    the whole chain, from the harness DB directly."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['exit_reason'] == 'TRAIL' and rows[0]['sl_placed_events'] == 1
          and rows[0]['arm_events'] == 1 and rows[0]['exit_events'] == 1 and rows[0]['state'] == 'live')
    return ok, rows
