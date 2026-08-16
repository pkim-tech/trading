"""Phase 2 scenario: `drought_handoff_cancel` / `drought_handoff_exit_placement`,
the real cancel/replace chain `signals_notify.check_drought_handoff` (Part 5)
drives once a node's own core signal fires again while a drought-overlay
entry/position is still live. Both Grid rows are covered in ONE file
(shared handoff-chain fixtures/state) rather than split -- exact precedent:
scenarios_replace_target_mismatch.py covers 2 Grid rows in one module for
the identical reason (one real code path, two accountability rows).

Real gap this proves against: entry-side drought (`drought_entry_placement`)
has real live proof (RETL, 2026-08-10, see CLAUDE.md) but HANDOFF itself --
cancelling the drought-overlay's OWN resting order, or exiting an OPEN
drought position, the moment core's real signal re-fires -- has never fired
live and (per tests/test_fake_broker_drought_handoff_scenario.py's own
docstring) is architecturally distinct from paper's synchronous HANDOFF
close: real has a resting-order cancel RACE (Case A) and an UNCONFIRMED-fill
window (Case B) paper never has. The existing fake_broker-tier tests already
regression-test check_drought_handoff's own branch logic in isolation
(cancel/raced_fill/cancel_unconfirmed/no_order_id_on_file for Case A; placed
SELL + confirmed-fill close, and the failure-alert branch, for Case B) --
what none of them do is chain Case B's UNCONFIRMED branch into the SEPARATE
poll (`check_own_sell_fills`) that Grid row's own notes say is the real
structural difference from paper ("must persist trail_state['exit_pending']
and let check_own_sell_fills/check_auto_fills close it on an unconfirmed-fill
poll") -- see leg C below for what that chain actually does today.

Three legs, three nodes. Node A/B share fv_cash + the scenario ticker (the
fill-race's own fall-through chain is what's under test there); node C gets
its OWN account (fv_margin) specifically so its leg can't be cross-
contaminated by node B's leftover resting order (see node B's finding below
-- real, not a scenario artifact, but isolating it keeps node C's own
unconfirmed-fill proof clean):

  NODE A  Case A, clean cancel. A drought entry order is resting, unfilled;
          core's real BUY signal re-fires; check_drought_handoff cancels the
          resting order via the REAL schwab_client.cancel_order round-trip
          (not a hand-seeded CANCELED status).
          => coverage_events['drought_handoff_cancel'] = 'cancelled_resting_entry'  <-- checked
          => pending_buys row cleared, broker order genuinely CANCELED       <-- checked

  NODE B  Case A, THE FILL-RACE (explicitly requested -- the one sub-case no
          existing test drives through the REAL two-call sequence: cancel
          request accepted, but a genuine broker fill lands in the gap
          before it's confirmed). Falls through to Case B in the SAME
          check_drought_handoff call -- proving the fall-through chain
          itself, not just the two halves in isolation.
          => coverage_events['drought_handoff_cancel'] = 'raced_fill'         <-- checked
          => the raced fill is reconciled as a real drought position (never
             silently discarded) -- confirmed via buy_fill_reconciled        <-- checked

          REAL FINDING (2026-08-15, this scenario's first run), not a bug in
          the sense of broken/incorrect behavior -- the failure-closed
          handling below is exactly what test_handoff_case_b_exit_failure_
          is_logged_and_alerted (fake_broker tier) already proves is
          CORRECT, just reached via a different, more realistic real trigger
          than that test's synthetic kill-switch: _reconcile_buy_fill (the
          same fill-reconciliation function every automated fill goes
          through) auto-places a real protective SL for the newly-opened
          drought position INLINE, in the same call, since the ticker is in
          AUTOMATION_ENABLED_TICKERS -- this is real, intended behavior
          (post_fill_topup's own leg 1 note: "check_auto_fills' single call
          also triggers _place_stop_loss_for_position"). Case B's immediate
          fall-through then tries to REPLACE that SAME just-placed SL with a
          market sell, milliseconds later -- well inside schwab_safety's own
          60s DUPLICATE_ORDER_WINDOW_SECS. The dup-order-window guard
          (schwab_safety.check_order, ~line 1948) has an explicit SELL-side
          fingerprint exemption for one known self-collision shape already
          (is_addon_leg / an open addon leg, ~line 1929's comment: "a leg's
          own SELL... always has the exact same (account, ticker, side,
          quantity) as the parent core position's own SELL") -- but NOT for
          this one (HANDOFF's own replace of its own just-placed SL), so it
          genuinely, deterministically (not a timing flake -- the window is
          60s, this collision is milliseconds) blocks Case B's replace as if
          it were an unrelated duplicate SELL. The system fails SAFE here:
          automated_exit_execution logs 'blocked', drought_handoff_exit_
          placement logs 'failed_or_blocked', the position is NOT silently
          dropped (still open, still protected by the SL that just placed),
          and a human is alerted to close it manually. Left open as a
          possible future enhancement (a HANDOFF-side dup-window exemption
          mirroring the addon-leg one) -- NOT fixed here, since it would
          touch schwab_safety.py and needs the same paired review as any
          other signals_*/schwab_*.py change.
          => coverage_events['drought_handoff_exit_placement'] =
             'failed_or_blocked'                                             <-- checked (real trigger)
          => coverage_events['automated_exit_execution'] = 'blocked',
             detail containing 'duplicate order'                             <-- checked
          => manual_sl_fallback_alert fires -- position stays open, protected <-- checked

  NODE C  Case B, THE UNCONFIRMED-FILL WINDOW -- what the Grid row's own
          notes flag as untested, and node C's own account keeps it clear of
          node B's leftover resting SL above. An OPEN drought position; core
          re-fires; check_drought_handoff places the real market SELL, but
          this leg delays schwab_client.get_filled_order's SELL-side
          confirmation (a real, if usually brief, gap -- Schwab's own
          order-status read lagging a fill that already happened at the
          matching engine) past check_drought_handoff's own
          _GAP_FILL_POLL_ATTEMPTS-attempt poll.
          => coverage_events['drought_handoff_exit_placement'] =
             'placed_unconfirmed'                                            <-- TARGET
          => trail_state['exit_pending'] persisted (reason='HANDOFF',
             order_id=<real exit order>) instead of closing synchronously --
             the exact structural difference from paper this row exists to
             prove                                                           <-- checked
          Then, once the broker-side confirmation is no longer artificially
          delayed, the REAL standalone poll (`check_own_sell_fills`, called
          exactly as active_signals.py's run_loop calls it every cycle) is
          run against this position, mirroring how the real daemon would
          eventually pick this fill up on a later cycle.

          FIXED (found 2026-08-15, fixed 2026-08-16) -- signals_notify.
          check_drought_handoff (~line 2237)'s `state['exit_pending'] = {...}`
          write on the unconfirmed branch used to carry only 'reason'/
          'order_id'/'placed_at'. Every OTHER call site that ever writes
          exit_pending in this file (line ~1344, line ~2479 -- notify_sell_
          signal's own TP/SL/TIME wait-for-manual-confirm write) always
          includes 'current_price' alongside it, because check_own_sell_fills
          (line ~3236) unconditionally reads exit_pending['current_price']
          when it closes the position:
              closed = db.close_position(pos['id'],
                            exit_signal_price=exit_pending['current_price'], ...)
          check_drought_handoff's write was the ONE exit_pending producer
          that omitted it -- so the very poll this Grid row's own notes name
          as the real close path (check_own_sell_fills) raised
          KeyError('current_price') the moment it tried to close a HANDOFF
          position that didn't confirm its fill inline. Fixed by adding
          'current_price': price to that dict, matching every other producer's
          shape -- paired independent-cold + contextual Opus review (required
          since the fix touches signals_notify.py) ran clean, no confirmed
          findings. The check below was `required=False` while this was an
          open gap; now that it's fixed and this run reconfirms it
          (check_own_sell_fills closes node C's position with no exception),
          it is `required=True` -- real regression coverage against a
          reintroduction of the bug, not just a note.

Entry-side state (Node B/C's drought positions/orders) is SEEDED, not placed
through the real BUY path -- same accepted caveat as every sibling Phase 2
scenario; this scenario's target is the HANDOFF cancel/exit chain, not
drought entry placement (already separately live-proven).
"""
from dataclasses import dataclass
from datetime import datetime

from fake_venue import venue
from fake_venue.scenarios_meta import CASH_ALIAS, MARGIN_ALIAS, PRICE_SOURCE_TICKER, TICKER

FAKE_ACCOUNTS = [
    dict(alias=CASH_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='cash', margin_capable=0),
    # Node C gets its OWN account -- keeps its leg's ticker+account combo
    # clear of node B's leftover resting SL (see node B's real finding in the
    # module docstring: the dup-order-window block leaves that SL resting),
    # since schwab_safety's resting-order guards are ticker+account scoped,
    # not node-scoped.
    dict(alias=MARGIN_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='margin', margin_capable=1),
]
NODE_NOTIONAL = 2_000
FIXED_SL_PCT = 1.0
DROUGHT_CONFIRM_DAYS = 3


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ''
    required: bool = True

    def __post_init__(self):
        self.ok = bool(self.ok)


def _add_node(version_suffix, account=CASH_ALIAS):
    import signals_db as db

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout',
                version=f'fake_venue_droughthandoff_{version_suffix}',
                window=20, take_profit=10, stop_loss=FIXED_SL_PCT, max_hold_hours=56,
                state='live', account=account, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=FIXED_SL_PCT,
                label='fake-venue harness node (drought_handoff)')
    node = [n for n in db.get_watchlist() if n['ticker'] == TICKER
            and n['version'] == f'fake_venue_droughthandoff_{version_suffix}'][0]
    with db._conn() as c:
        c.execute("UPDATE watch_list SET drought_overlay_enabled=1, drought_confirm_days=? WHERE id=?",
                   (DROUGHT_CONFIRM_DAYS, node['id']))
        c.commit()
    return [n for n in db.get_watchlist() if n['id'] == node['id']][0]


def _resting_sells(broker, account, ticker):
    return [o for o in broker.orders.values()
            if o['account'] == account and o['status'] == 'WORKING'
            and o['orderLegCollection'][0]['instruction'] == 'SELL'
            and o['orderLegCollection'][0]['instrument']['symbol'] == ticker]


def run(price=None, verbose=True):
    """Runs the scenario against the already-isolated, already-imported
    environment. Returns (checks, observations)."""
    import time as time_mod

    import schwab_client
    import signals_compute
    import signals_db as db
    import signals_notify as notify
    import schwab_safety

    def say(msg):
        if verbose:
            print(msg)

    checks, observations = [], {}

    db.ensure_tables()
    if [n for n in db.get_watchlist() if n['ticker'] == TICKER]:
        raise RuntimeError("this harness DB has already been used -- point --db-path at a fresh "
                           "file (the default temp dir is fresh every run)")
    venue.seed_fake_accounts(FAKE_ACCOUNTS)
    broker = venue.install_fake_broker([CASH_ALIAS, MARGIN_ALIAS])
    price = venue.seed_quote(broker, TICKER, price, price_source_ticker=PRICE_SOURCE_TICKER)
    broker.set_cash_balance(CASH_ALIAS, 100_000.0)
    broker.set_cash_balance(MARGIN_ALIAS, 100_000.0)
    say(f"[setup] {TICKER} quote seeded at ${price:.4f}")

    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking schwab_safety._is_trading_day "
            "True for this run (orthogonal to the mechanism under test, same override every other "
            "Phase 2 scenario uses)")
        schwab_safety._is_trading_day = lambda date_str: True

    shares = max(int(NODE_NOTIONAL // price), 1)

    # compute_buy_signal is monkeypatched, not driven off real historical bars
    # -- same accepted convention as every fake_broker-tier drought test
    # (tests/test_fake_broker_drought_handoff_scenario.py's own _buy_signal
    # helper). check_drought_handoff's own CRITICAL guard (only a real
    # signal=='BUY' dict, never a HOLD dict, can drive HANDOFF) is exercised
    # implicitly by every leg below actually firing.
    def _buy_signal(current_price):
        return {'current_price': current_price, 'signal': 'BUY', 'z_score': -2.5, 'last_bar': datetime.now()}

    signals_compute.compute_buy_signal = lambda n: _buy_signal(price)

    # ============================================================== NODE A
    say("[node A] Case A, clean cancel -- resting drought entry order, core re-fires")
    node_a = _add_node('a')
    order_a = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'BUY', shares, trail_offset=1.0)
    db.add_pending_buy(node_a, {'current_price': price, 'last_bar': datetime.now()}, None, None,
                       order_id=order_a, position_source='drought_overlay',
                       drought_confirm_days=DROUGHT_CONFIRM_DAYS)
    db.mark_pending_buy_placed_by_wl_id(node_a['id'])
    checks.append(Check("node A setup: drought entry order resting, unfilled",
                        broker.orders[order_a]['status'] == 'WORKING'))

    notify.check_drought_handoff(node_a)

    checks.append(Check("node A: the REAL cancel_order round-trip confirmed CANCELED at the broker",
                        broker.orders[order_a]['status'] == 'CANCELED',
                        f"status={broker.orders[order_a]['status']}"))
    checks.append(Check("node A: drought pending_buys row cleared",
                        db.get_drought_pending_buy(node_a['id']) is None))
    cancel_events = db.get_coverage_events(scenario_key="drought_handoff_cancel")
    cancel_a = [e for e in cancel_events if e['node_id'] == node_a['id']]
    checks.append(Check("node A: drought_handoff_cancel fired 'cancelled_resting_entry'",
                        len(cancel_a) == 1 and cancel_a[0]['result'] == 'cancelled_resting_entry',
                        f"events={[(e['result']) for e in cancel_a]}"))

    # ============================================================== NODE B
    say("[node B] Case A, the fill-race -- cancel finds the order already FILLED, "
        "falls through to Case B's real market SELL in the SAME poll")
    node_b = _add_node('b')
    order_b = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'BUY', shares, trail_offset=1.0)
    db.add_pending_buy(node_b, {'current_price': price, 'last_bar': datetime.now()}, None, None,
                       order_id=order_b, position_source='drought_overlay',
                       drought_confirm_days=DROUGHT_CONFIRM_DAYS)
    db.mark_pending_buy_placed_by_wl_id(node_b['id'])
    fill_price_b = round(price * 0.995, 4)
    broker.force_fill(order_b, fill_price_b)
    say(f"[node B] broker: order {order_b} raced to FILLED @ ${fill_price_b:.4f} the instant before "
        f"the real cancel_order call lands")
    checks.append(Check("node B setup: drought entry order raced to FILLED before HANDOFF's cancel call",
                        broker.orders[order_b]['status'] == 'FILLED'))

    notify.check_drought_handoff(node_b)

    cancel_events = db.get_coverage_events(scenario_key="drought_handoff_cancel")
    cancel_b = [e for e in cancel_events if e['node_id'] == node_b['id']]
    checks.append(Check("node B: drought_handoff_cancel fired 'raced_fill' -- the real cancel_order "
                        "round-trip itself reported FILLED, not a hand-seeded status",
                        len(cancel_b) == 1 and cancel_b[0]['result'] == 'raced_fill',
                        f"events={[(e['result']) for e in cancel_b]}"))
    checks.append(Check("node B: drought pending_buys row cleared (raced fill reconciled, not left "
                        "dangling)", db.get_drought_pending_buy(node_b['id']) is None))
    buy_events_b = db.get_coverage_events(scenario_key="buy_fill_reconciled")
    opened_b = [e for e in buy_events_b if e['node_id'] == node_b['id'] and e['result'] == 'opened']
    checks.append(Check("node B: the raced fill was reconciled into a REAL drought position via "
                        "_reconcile_buy_fill (the same fill-reconciliation path every automated fill "
                        "uses) -- never silently discarded", len(opened_b) == 1,
                        f"events={[(e['result'], e['detail']) for e in buy_events_b]}"))
    sl_events_b = db.get_coverage_events(scenario_key="sl_placement")
    sl_placed_b = [e for e in sl_events_b if e['node_id'] == node_b['id'] and e['result'] == 'placed']
    checks.append(Check("node B: _reconcile_buy_fill auto-placed a real protective SL inline (same "
                        "call) -- this is what Case B's immediate replace-attempt collides with below",
                        len(sl_placed_b) == 1, f"events={[(e['result'], e['detail']) for e in sl_events_b]}"))

    # REAL FINDING (see module docstring): Case B's immediate fall-through
    # tries to replace that just-placed SL milliseconds later -- well inside
    # schwab_safety's 60s dup-order-window, which has no exemption for this
    # self-collision shape (unlike the addon-leg one it does have). This is
    # NOT the ideal outcome, but IS the correctly-designed failure-closed
    # behavior -- proven below, not papered over.
    exec_events_b = db.get_coverage_events(scenario_key="automated_exit_execution")
    blocked_b = [e for e in exec_events_b if e['node_id'] == node_b['id'] and e['result'] == 'blocked']
    checks.append(Check("node B: Case B's replace-the-just-placed-SL attempt was correctly BLOCKED by "
                        "schwab_safety's dup-order-window guard (a real, deterministic self-collision -- "
                        "see module docstring's node B finding), not silently ignored or mis-executed",
                        len(blocked_b) == 1 and 'duplicate order' in (blocked_b[0]['detail'] or ''),
                        f"events={[(e['result'], e['detail']) for e in exec_events_b]}"))
    exit_events_b = db.get_coverage_events(scenario_key="drought_handoff_exit_placement")
    failed_b = [e for e in exit_events_b if e['node_id'] == node_b['id'] and e['result'] == 'failed_or_blocked']
    checks.append(Check("node B: drought_handoff_exit_placement fired 'failed_or_blocked' -- the "
                        "failure-closed branch, reached via a real (not synthetic) trigger",
                        len(failed_b) == 1, f"events={[(e['result']) for e in exit_events_b]}"))
    fallback_b = db.get_coverage_events(scenario_key="manual_sl_fallback_alert")
    alerted_b = [e for e in fallback_b if e['node_id'] == node_b['id'] and e['result'] == 'alerted']
    checks.append(Check("node B: manual_sl_fallback_alert fired -- a human is told to verify/close "
                        "manually, exactly as the failure-closed contract requires",
                        len(alerted_b) == 1, f"events={[(e['result'], e['detail']) for e in fallback_b]}"))
    checks.append(Check("node B: the drought position is NOT silently dropped -- still open, still "
                        "protected by the SL that was just placed (never left naked)",
                        db.get_drought_overlay_position(node_b['id']) is not None))
    sells_b = _resting_sells(broker, CASH_ALIAS, TICKER)
    checks.append(Check("node B: exactly one resting SELL remains (the auto-placed protective SL, "
                        "un-replaced since the replace attempt was blocked) -- no orphan/duplicate order",
                        len(sells_b) == 1, f"resting={[o['orderId'] for o in sells_b]}"))

    # ============================================================== NODE C
    say("[node C] Case B, the unconfirmed-fill window -- an OPEN drought position (own account, "
        "isolated from node B's leftover resting SL above); core re-fires; the real market SELL's own "
        "fill confirmation is delayed past check_drought_handoff's own poll budget (a real Schwab "
        "order-status lag, not a broker-side non-fill)")
    node_c = _add_node('c', account=MARGIN_ALIAS)
    entry_price_c = round(price * 0.98, 4)
    now = datetime.now()
    opened_c = db.open_drought_overlay_position(node_c, entry_price_c, now, entry_price_c, now,
                                                confirm_days=DROUGHT_CONFIRM_DAYS, shares=shares)
    checks.append(Check("node C setup: real open drought-overlay position", opened_c is not None))
    pos_c = db.get_drought_overlay_position(node_c['id'])
    checks.append(Check("node C setup: position visible via get_drought_overlay_position",
                        pos_c is not None))

    real_get_filled_order = schwab_client.get_filled_order
    real_sleep = time_mod.sleep
    delay_state = {'sell_calls': 0}
    DELAY_ATTEMPTS = notify._GAP_FILL_POLL_ATTEMPTS  # exhaust check_drought_handoff's own poll budget

    def _delayed_get_filled_order(account, ticker, side, order_id=None):
        # Only the SELL side is delayed -- node C's own BUY side was never in
        # play (its drought position was seeded already-open, not placed
        # through a BUY order this scenario tracks), and by this point node
        # A/B's own get_filled_order calls have already completed, so a
        # blanket SELL-side delay can't cross-contaminate an earlier leg.
        if side == 'SELL':
            delay_state['sell_calls'] += 1
            if delay_state['sell_calls'] <= DELAY_ATTEMPTS:
                return None
        return real_get_filled_order(account, ticker, side, order_id=order_id)

    schwab_client.get_filled_order = _delayed_get_filled_order
    notify.time.sleep = lambda *a, **kw: None  # skip the real ~15s poll-interval wait, not part of the mechanism under test
    try:
        notify.check_drought_handoff(node_c)
    finally:
        schwab_client.get_filled_order = real_get_filled_order
        notify.time.sleep = real_sleep

    checks.append(Check("node C: check_drought_handoff's own confirmation poll genuinely exhausted "
                        "its budget (the delay wrapper actually fired the full attempt count)",
                        delay_state['sell_calls'] >= DELAY_ATTEMPTS,
                        f"sell_calls={delay_state['sell_calls']} budget={DELAY_ATTEMPTS}"))

    exit_events = db.get_coverage_events(scenario_key="drought_handoff_exit_placement")
    unconfirmed_c = [e for e in exit_events if e['node_id'] == node_c['id']
                     and e['result'] == 'placed_unconfirmed']
    checks.append(Check("node C: drought_handoff_exit_placement fired 'placed_unconfirmed' -- the "
                        "TARGET this scenario exists to prove (Case B's fill confirmation genuinely "
                        "did not land inline, unlike node B's raced-through case above)",
                        len(unconfirmed_c) == 1,
                        f"events={[(e['result'], e['detail']) for e in exit_events]}"))

    pos_c_after = db.get_drought_overlay_position(node_c['id'])
    checks.append(Check("node C: the drought position is STILL OPEN locally -- HANDOFF did not close "
                        "it synchronously (the real structural difference from paper's HANDOFF, per "
                        "this Grid row's own notes)", pos_c_after is not None))
    state_c = (pos_c_after or {}).get('trail_state') or {}
    exit_pending_c = state_c.get('exit_pending') or {}
    checks.append(Check("node C: trail_state['exit_pending'] was persisted with reason='HANDOFF' and "
                        "the real placed order's id, for a later poll to pick up",
                        exit_pending_c.get('reason') == 'HANDOFF' and exit_pending_c.get('order_id') is not None,
                        f"exit_pending={exit_pending_c}"))
    sell_orders_c = [oid for oid, o in broker.orders.items()
                     if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                     and o['orderLegCollection'][0]['instruction'] == 'SELL'
                     and o['account'] == MARGIN_ALIAS]
    real_exit_order_c = sell_orders_c[0] if len(sell_orders_c) == 1 else None
    checks.append(Check("node C: exactly one new real market SELL order exists for this leg, and it "
                        "IS actually FILLED at the broker (the delay was in OUR confirmation read, "
                        "never in whether the broker itself filled it)",
                        real_exit_order_c is not None and broker.orders[real_exit_order_c]['status'] == 'FILLED',
                        f"sell_orders={sell_orders_c} status={broker.orders.get(real_exit_order_c, {}).get('status')}"))
    checks.append(Check("node C: the persisted exit_pending order_id matches the real broker order",
                        exit_pending_c.get('order_id') == real_exit_order_c,
                        f"exit_pending_order_id={exit_pending_c.get('order_id')} real={real_exit_order_c}"))

    # ---- the real daemon's OWN later poll, run exactly as active_signals.py's
    # run_loop calls it every cycle, now that the artificial delay is gone ----
    say("[node C] running the real signals_notify.check_own_sell_fills([pos]) -- the standalone "
        "poll the Grid row's own notes name as the real close path for this branch")
    check_own_sell_fills_error = None
    try:
        notify.check_own_sell_fills([pos_c_after])
    except Exception as e:
        check_own_sell_fills_error = f"{type(e).__name__}: {e}"
    observations['check_own_sell_fills_error'] = check_own_sell_fills_error
    say(f"[node C] check_own_sell_fills(...) -> "
        f"{'raised: ' + check_own_sell_fills_error if check_own_sell_fills_error else 'completed without raising'}")

    # required=True -- see module docstring's "FIXED" note. Was required=False
    # while check_drought_handoff's exit_pending write omitted 'current_price'
    # (a real KeyError waiting to fire the first time this branch hit live);
    # now that the fix landed (paired-reviewed, see docstring), this is real
    # regression coverage against the bug being reintroduced.
    checks.append(Check(
        "node C: check_own_sell_fills closes the HANDOFF position cleanly once confirmation lands "
        "(FIXED: signals_notify.check_drought_handoff's placed_unconfirmed exit_pending write, "
        "~line 2237, now includes 'current_price', matching every other exit_pending producer in "
        "this file -- check_own_sell_fills (~line 3236), which unconditionally reads "
        "exit_pending['current_price'], no longer raises KeyError for this real branch)",
        check_own_sell_fills_error is None and db.get_drought_overlay_position(node_c['id']) is None,
        f"check_own_sell_fills_error={check_own_sell_fills_error!r}",
        required=True,
    ))

    observations['node_a_wl_id'] = node_a['id']
    observations['node_b_wl_id'] = node_b['id']
    observations['node_c_wl_id'] = node_c['id']
    observations['price'] = price
    observations['shares'] = shares
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence. Now also covers
# node C's check_own_sell_fills close (the exit_pending['current_price'] fix
# above) -- previously excluded here since that branch was a known-open
# production gap; now that it's fixed, its close-via-trade_log is a real,
# provable fact just like the other mechanisms.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT wl.id AS wl_id, wl.account,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='drought_handoff_cancel'
         AND result='cancelled_resting_entry' AND node_id=wl.id) AS clean_cancels,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='drought_handoff_cancel'
         AND result='raced_fill' AND node_id=wl.id) AS raced_fills,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='drought_handoff_exit_placement'
         AND result='failed_or_blocked' AND node_id=wl.id) AS failed_or_blocked_placements,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='drought_handoff_exit_placement'
         AND result='placed_unconfirmed' AND node_id=wl.id) AS unconfirmed_placements,
       (SELECT COUNT(*) FROM open_positions WHERE wl_id=wl.id
         AND position_source='drought_overlay') AS open_drought_positions,
       (SELECT COUNT(*) FROM trade_log WHERE wl_id=wl.id
         AND exit_reason='HANDOFF') AS handoff_closed_trades
  FROM watch_list wl
 WHERE wl.ticker = ?
 ORDER BY wl.id
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly 3 nodes (A, B, C): node A with
    1 clean cancel; node B with 1 raced_fill AND 1 failed_or_blocked exit
    placement (the real dup-order-window collision, see module docstring --
    node B's OWN just-placed protective SL blocks its own HANDOFF replace);
    node C with 1 placed_unconfirmed exit-placement event, zero remaining
    open drought-overlay position, and 1 real HANDOFF-closed trade_log row --
    directly from the harness DB. The last two assert the exit_pending
    ['current_price'] fix's real effect (check_own_sell_fills actually
    closing the position), not just the absence of a KeyError."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    if len(rows) != 3:
        return False, rows
    node_a, node_b, node_c = rows
    ok = (node_a['clean_cancels'] == 1
          and node_b['raced_fills'] == 1 and node_b['failed_or_blocked_placements'] == 1
          and node_c['unconfirmed_placements'] == 1
          and node_c['open_drought_positions'] == 0
          and node_c['handoff_closed_trades'] == 1)
    return ok, rows
