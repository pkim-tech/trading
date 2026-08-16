"""Phase 2 scenario: `gap_resize`, proving `signals_notify.check_gap_resize`'s
pre-market cancel-stale-order + replace-with-MARKET path end to end, driven
by a genuine SEQUENCE of quotes (overnight fall, then a gap up) rather than a
single static price -- the "replay" the harness's quote bridge exists to
support (fake_venue/venue.py's seed_quote docstring: "A soak/replay mode
(Phase 2) needs a real repeating feed here").

Real gap this closes: the 2026-07-24 live test (GDXD/GDXU) found neither
ticker had actually gapped, so the real cancel+replace-with-MARKET path was
never exercised live; a mid-day manufactured version was explicitly rejected
at the time (interpreting genuine overnight movement pre-open is the actual
mechanism, faking it mid-day doesn't test the same thing). The existing
pytest-tier tests (tests/test_part3_gap_resize.py -- mocked schwab_client
calls; tests/test_fake_broker_gap_resize_scenario.py -- a real FakeBroker but
no isolation-tripwire proof, no CLI, no schwab_stream import) already cover
check_gap_resize's own branch logic, including the exact two-phase
fall-then-gap-up shape this scenario reuses
(test_gap_resize_catches_correction_once_running_low_is_tracked). What none
of them prove: the full real-broker round trip (atomic replace, poll-for-
fill, reconcile, auto-SL-placement) running inside the persistent isolated
harness (isolation tripwire + schwab_stream + real coverage_events, matching
every sibling Phase 2 scenario), AND what happens when the replacement's OWN
fill confirmation does NOT land within check_gap_resize's own poll budget --
the exact "will be caught by the next check_auto_fills poll" fallback its own
comment names but no existing test drives through that real second poll.

Two nodes (this scenario isn't about ticker/account disambiguation -- Phase 1
already covers that; it's about the cancel+replace sequence and its
confirmation-poll fallback). Node B gets its own account -- see FAKE_ACCOUNTS'
comment below for why (a real, scenario-design finding, not a production bug):

  NODE A  the real overnight-gap replay + confirmed-inline happy path.
          Phase 1: price genuinely falls (a real quote update, not a single
          static seed) -- notify.update_real_pending_buys_running_low() (the
          real production tracker, RETL 2026-07-29 fix) tracks running_low
          down.
          Phase 2: price gaps back UP past the TRUE trigger (computed off the
          tracked running_low) but NOT past the STALE trigger the original,
          frozen signal_price would have produced -- the exact shape that
          proves the running_low fix actually matters here, not just that
          *some* gap fires a replace.
          notify.check_gap_resize() then cancels the resting trailing-buy via
          the real atomic schwab_client.replace_equity_order_with_market
          round trip, its own poll confirms the replacement's fill
          immediately (FakeBroker fills a MARKET order synchronously, same
          as post_fill_topup's leg 1), and reconciles it via the same
          _reconcile_buy_fill every other fill path uses.
          => coverage_events['gap_resize'] = 'replaced'                <-- checked
          => original resting order REPLACED at the broker; new MARKET BUY
             FILLED                                                    <-- checked
          => pending_buys row cleared, real position opened at the gap
             price, real protective SL auto-placed inline (ticker is in
             AUTOMATION_ENABLED_TICKERS, same as every other automated fill) <-- checked
          A second check_gap_resize() call afterward is a genuine no-op (no
          pending row left for this ticker) -- confirms the function doesn't
          re-act on an already-resolved row within the harness's single
          process the way the persisted gap_resize_date marker prevents
          across a restart (tests/test_part3_gap_resize.py's own
          idempotency test covers the restart shape; this just confirms nothing
          double-fires within one call sequence).                      <-- checked

  NODE B  the CONFIRMATION-POLL-EXHAUSTED case -- check_gap_resize's own
          docstring says it "polls for the replacement's own fill and
          reconciles it immediately rather than deferring to the next
          check_auto_fills cycle"; this leg is what happens when that inline
          poll genuinely does not confirm in time (a real Schwab order-status
          read lag, not a broker-side non-fill -- same shape as
          scenarios_drought_handoff.py's node C, applied to the BUY side).
          notify.check_gap_resize() still replaces the order for real (the
          broker DID fill it), but our own confirmation read is delayed past
          _GAP_FILL_POLL_ATTEMPTS, so the function posts a "not confirmed --
          will be caught by the next check_auto_fills poll" message and
          leaves the pending_buys row in place (already repointed at the
          NEW order id via db.set_pending_buy_order_id_by_wl_id, which runs
          BEFORE the poll loop -- so the fallback has something real to
          find). Once the artificial delay is lifted, the REAL standalone
          fallback poll (check_auto_fills, called exactly as
          active_signals.py's run_loop calls it every cycle) picks up the
          fill using that repointed order id and reconciles it.
          => coverage_events['gap_resize'] = 'replaced' (logged before the
             confirmation poll even starts -- unaffected by whether OUR read
             of the fill lands in time)                                <-- checked
          => pending_buys row NOT cleared by check_gap_resize itself, order_id
             repointed to the replacement                              <-- checked
          => no position opened by check_gap_resize itself             <-- checked
          => check_auto_fills (the real fallback, run separately, exactly as
             the daemon's own poll loop would) resolves the SAME replacement
             order via the repointed order_id and reconciles it          <-- checked

Entry-side state (both nodes' original resting trailing-buy orders) is
SEEDED, not placed through the real BUY path -- same accepted caveat as
every sibling Phase 2 scenario; this scenario's target is the cancel+replace+
reconcile chain, not trailing-buy entry placement (separately live-proven,
see CLAUDE.md's V5 go-live checklist).
"""
from dataclasses import dataclass
from datetime import datetime

from fake_venue import venue
from fake_venue.scenarios_meta import CASH_ALIAS, MARGIN_ALIAS, PRICE_SOURCE_TICKER, TICKER

FAKE_ACCOUNTS = [
    dict(alias=CASH_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='cash', margin_capable=0),
    # Node B gets its OWN account -- schwab_safety's duplicate-order guard
    # (check_order's _has_open_order, "already has an open/working order in
    # this account") is ticker+account scoped, not node-scoped. This
    # scenario's first run found that live: node A's leftover resting SL
    # (from its own auto-SL-placement + top-up) made node B's real BUY
    # attempt collide and get BLOCKED as a false duplicate -- exactly the
    # same isolation precedent scenarios_drought_handoff.py's node C uses
    # for the identical reason (see that module's own comment on MARGIN_ALIAS).
    dict(alias=MARGIN_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='margin', margin_capable=1),
]
NODE_NOTIONAL = 2_000
FIXED_SL_PCT = 1.0
TRAIL_BUY_PCT = 1.0
# Comfortably inside MAX_RUNNING_LOW_DROP_PCT=20% (signals_helpers.py) so
# update_real_pending_buys_running_low adopts the fall in one poll, not the
# bounded-floor partial-adoption case (that's exercised separately in
# tests/test_fake_broker_gap_resize_scenario.py::test_running_low_bounded_
# against_a_single_anomalous_print -- not this scenario's target).
OVERNIGHT_DROP_PCT = 3.0


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
                version=f'fake_venue_gapresize_{version_suffix}',
                window=20, take_profit=10, stop_loss=FIXED_SL_PCT, max_hold_hours=56,
                state='live', account=account, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=TRAIL_BUY_PCT, trail_pct=1.0, fixed_sl_override=FIXED_SL_PCT,
                label='fake-venue harness node (gap_resize)')
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER
            and n['version'] == f'fake_venue_gapresize_{version_suffix}'][0]


def run(price=None, verbose=True):
    """Runs the scenario against the already-isolated, already-imported
    environment. Returns (checks, observations)."""
    import time as time_mod

    import schwab_client
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
    broker = venue.install_fake_broker([CASH_ALIAS, MARGIN_ALIAS])
    price = venue.seed_quote(broker, TICKER, price, price_source_ticker=PRICE_SOURCE_TICKER)
    broker.set_cash_balance(CASH_ALIAS, 100_000.0)
    broker.set_cash_balance(MARGIN_ALIAS, 100_000.0)
    say(f"[setup] {TICKER} quote seeded at ${price:.4f} (signal price, P0)")

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

    # Both bindings needed -- schwab_client.py's own replace/place confirmation
    # posts (the "✅ Replaced..."/"not confirmed" messages) come from inside
    # schwab_client.py, not signals_notify.py (same reasoning as
    # scenarios_replace_target_mismatch.py's identical dual-bind).
    notify._post_message = _capture
    schwab_client._post_message = _capture

    p0 = price

    # ============================================================== NODE A
    say("[node A] real overnight-gap replay: fall, then a gap-up past the TRUE "
        "(tracked) trigger but not the STALE (frozen signal_price) one")
    node_a = _add_node('a')
    order_shares_a = max(int(NODE_NOTIONAL // (p0 * (1 + TRAIL_BUY_PCT / 100))), 1)
    order_a = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'BUY',
                                        order_shares_a, trail_offset=TRAIL_BUY_PCT)
    sig_a = {'current_price': p0, 'last_bar': datetime.now()}
    db.add_pending_buy(node_a, sig_a, channel=None, ts=None, order_id=order_a)
    db.mark_pending_buy_placed_by_wl_id(node_a['id'])
    pending_a_pre = [p for p in db.get_pending_buys() if p['ticker'] == TICKER][0]
    checks.append(Check("node A setup: running_low starts at signal_price (pre-tracking state)",
                        pending_a_pre['running_low'] == p0,
                        f"running_low={pending_a_pre['running_low']} signal_price={p0}"))

    # ---- Phase 1: a real overnight fall, driven through a real quote update
    p_low = round(p0 * (1 - OVERNIGHT_DROP_PCT / 100), 4)
    broker.set_quote(TICKER, last=p_low, bid=p_low, ask=round(p_low + 0.01, 4))
    say(f"[node A] [phase 1] quote falls to ${p_low:.4f} ({OVERNIGHT_DROP_PCT}% overnight drop); "
        f"running update_real_pending_buys_running_low()")
    notify.update_real_pending_buys_running_low()
    pending_a_phase1 = [p for p in db.get_pending_buys() if p['ticker'] == TICKER][0]
    checks.append(Check("node A: real production tracker (update_real_pending_buys_running_low) "
                        "adopted the real overnight fall -- this is the RETL 2026-07-29 fix, proven "
                        "against a real quote sequence, not a hand-set DB value",
                        abs(pending_a_phase1['running_low'] - p_low) < 0.0005,
                        f"running_low={pending_a_phase1['running_low']} expected={p_low:.4f}"))

    # ---- Phase 2: gap up, clearing the TRUE trigger but not the STALE one
    p_gap = round(p0 * 0.99, 4)
    true_trigger = pending_a_phase1['running_low'] * (1 + TRAIL_BUY_PCT / 100)
    stale_trigger = p0 * (1 + TRAIL_BUY_PCT / 100)
    checks.append(Check("test setup: the gap-up price clears the TRUE (tracked) trigger but stays "
                        "under the STALE (frozen signal_price) trigger -- proves the running_low fix "
                        "is what actually makes this a real gap-resize firing, not any old jump",
                        true_trigger < p_gap < stale_trigger,
                        f"true_trigger={true_trigger:.4f} p_gap={p_gap:.4f} stale_trigger={stale_trigger:.4f}"))
    broker.set_quote(TICKER, last=p_gap, bid=p_gap, ask=round(p_gap + 0.01, 4))
    say(f"[node A] [phase 2] quote gaps up to ${p_gap:.4f}; running check_gap_resize()")

    notify.check_gap_resize()

    checks.append(Check("node A: the originally-seeded trailing-buy is REPLACED at the broker (real "
                        "atomic cancel+replace round trip)",
                        broker.orders[order_a]['status'] == 'REPLACED',
                        f"status={broker.orders[order_a]['status']}"))
    gap_events_a = db.get_coverage_events(scenario_key='gap_resize')
    replaced_a = [e for e in gap_events_a if e['result'] == 'replaced' and e['node_id'] == node_a['id']]
    checks.append(Check("node A: gap_resize fired 'replaced'", len(replaced_a) == 1,
                        f"events={[(e['result'], e['detail']) for e in gap_events_a]}"))
    # detail is "shares=<N> price=<P>" -- parsed (not hardcoded) so this stays
    # honest if the sizing math above ever shifts. Disambiguates gap_resize's
    # OWN replacement order by quantity from a second, legitimate MARKET BUY
    # this scenario's first run found: the gap fill (7 shares @ $247.50 =
    # $1,732.50) lands $267.50 under the $2,000 target -- comfortably above
    # the `delta > fill_price` top-up gate (_reconcile_fill), so a REAL
    # post-fill top-up buy (1 share) fires too, same mechanism
    # scenarios_post_fill_topup.py already proves end to end. Expected,
    # correct behavior, not a bug -- but it means "exactly one new MARKET BUY
    # order" is the wrong assertion for this leg; quantity-matching the
    # gap_resize event's own reported share count is the honest one.
    gap_resize_shares_a = None
    if replaced_a:
        try:
            gap_resize_shares_a = int(replaced_a[0]['detail'].split('shares=')[1].split(' ')[0])
        except (IndexError, ValueError):
            pass
    market_buys_a = [oid for oid, o in broker.orders.items()
                     if o['account'] == CASH_ALIAS and o['orderType'] == 'MARKET'
                     and o['orderLegCollection'][0]['instruction'] == 'BUY']
    matching_a = [oid for oid in market_buys_a
                 if broker.orders[oid]['orderLegCollection'][0]['quantity'] == gap_resize_shares_a]
    replacement_a = matching_a[0] if len(matching_a) == 1 else None
    checks.append(Check("node A: gap_resize's own MARKET BUY replacement (matched by its reported "
                        "share count) exists at the broker and is already FILLED (a real MARKET order "
                        "fills immediately)",
                        replacement_a is not None and broker.orders[replacement_a]['status'] == 'FILLED',
                        f"market_buys={market_buys_a} expected_shares={gap_resize_shares_a} "
                        f"matching={matching_a}"))
    checks.append(Check("node A: pending_buys row cleared -- the fill confirmed inline and reconciled, "
                        "not deferred to check_auto_fills",
                        [p for p in db.get_pending_buys() if p['ticker'] == TICKER
                         and p['node']['id'] == node_a['id']] == []))
    pos_a = db.get_open_position_by_wl_id(node_a['id'])
    checks.append(Check("node A: a real position opened at the gap price (not the stale signal price)",
                        pos_a is not None and abs(pos_a['entry_price'] - p_gap) < 0.0005,
                        f"entry_price={pos_a['entry_price'] if pos_a else None} expected={p_gap:.4f}"))
    sl_events_a = db.get_coverage_events(scenario_key='sl_placement')
    sl_placed_a = [e for e in sl_events_a if e['result'] == 'placed' and e['node_id'] == node_a['id']]
    checks.append(Check("node A: a real protective SL was auto-placed inline for the gap-resize fill "
                        "(same automatic SL path every automated fill gets -- ticker is in "
                        "AUTOMATION_ENABLED_TICKERS)",
                        len(sl_placed_a) == 1,
                        f"events={[(e['result'], e['detail']) for e in sl_events_a]}"))

    # Idempotency within this run: nothing left for check_gap_resize to act on.
    gap_events_before_replay = len(db.get_coverage_events(scenario_key='gap_resize'))
    notify.check_gap_resize()
    gap_events_after_replay = len(db.get_coverage_events(scenario_key='gap_resize'))
    checks.append(Check("node A: a second check_gap_resize() call is a genuine no-op (no pending row "
                        "left for this ticker) -- no duplicate replace/event",
                        gap_events_after_replay == gap_events_before_replay,
                        f"before={gap_events_before_replay} after={gap_events_after_replay}"))

    # ============================================================== NODE B
    say("[node B] the confirmation-poll-exhausted case -- the replacement fills for real at the "
        "broker, but OUR OWN confirmation read is delayed past check_gap_resize's own poll budget")
    # Own account (MARGIN_ALIAS) -- this scenario's first run found that
    # sharing node A's account here gets node B's real BUY attempt BLOCKED
    # by schwab_safety's ticker+account-scoped duplicate-order guard (node A
    # left a resting protective SL behind), a real but off-target collision
    # for what this leg is actually trying to prove. See FAKE_ACCOUNTS' own
    # comment above.
    node_b = _add_node('b', account=MARGIN_ALIAS)
    schwab_safety.enable_auto_fill_detection(TICKER)
    schwab_safety.enable_node_auto_fill_detection(node_b['id'])

    # Signal price set well below the current (already-gapped) quote so this
    # leg's trigger is already cleared the instant check_gap_resize runs --
    # the real overnight-tracking mechanism is node A's proof, this leg's
    # target is the confirmation-poll fallback, not a second tracking proof.
    p0_b = round(p_gap * 0.95, 4)
    order_shares_b = max(int(NODE_NOTIONAL // (p0_b * (1 + TRAIL_BUY_PCT / 100))), 1)
    order_b = broker.seed_resting_order(MARGIN_ALIAS, TICKER, 'TRAILING_STOP', 'BUY',
                                        order_shares_b, trail_offset=TRAIL_BUY_PCT)
    sig_b = {'current_price': p0_b, 'last_bar': datetime.now()}
    db.add_pending_buy(node_b, sig_b, channel=None, ts=None, order_id=order_b)
    db.mark_pending_buy_placed_by_wl_id(node_b['id'])
    checks.append(Check("node B setup: trigger already cleared at the current (post-gap) quote",
                        p_gap >= p0_b * (1 + TRAIL_BUY_PCT / 100),
                        f"current_quote={p_gap:.4f} trigger={p0_b * (1 + TRAIL_BUY_PCT / 100):.4f}"))

    real_get_filled_order = schwab_client.get_filled_order
    real_sleep = time_mod.sleep
    delay_state = {'buy_calls': 0}
    DELAY_ATTEMPTS = notify._GAP_FILL_POLL_ATTEMPTS

    def _delayed_get_filled_order(account, ticker, side, order_id=None):
        # Only BUY-side calls are delayed, and only after node A's own leg has
        # already fully completed above -- so this can't cross-contaminate
        # node A's earlier (already-resolved) confirmation read.
        if side == 'BUY':
            delay_state['buy_calls'] += 1
            if delay_state['buy_calls'] <= DELAY_ATTEMPTS:
                return None
        return real_get_filled_order(account, ticker, side, order_id=order_id)

    schwab_client.get_filled_order = _delayed_get_filled_order
    notify.time.sleep = lambda *a, **kw: None  # skip the real ~15s poll-interval wait, not part of the mechanism under test
    try:
        notify.check_gap_resize()
    finally:
        schwab_client.get_filled_order = real_get_filled_order
        notify.time.sleep = real_sleep

    checks.append(Check("node B: check_gap_resize's own confirmation poll genuinely exhausted its "
                        "budget (the delay wrapper actually fired the full attempt count)",
                        delay_state['buy_calls'] >= DELAY_ATTEMPTS,
                        f"buy_calls={delay_state['buy_calls']} budget={DELAY_ATTEMPTS}"))
    checks.append(Check("node B: the resting order was still genuinely REPLACED at the broker -- the "
                        "delay was in OUR confirmation read, never in whether the broker itself acted",
                        broker.orders[order_b]['status'] == 'REPLACED',
                        f"status={broker.orders[order_b]['status']}"))
    new_buys_b = [oid for oid, o in broker.orders.items()
                 if o['account'] == MARGIN_ALIAS and o['orderType'] == 'MARKET'
                 and o['orderLegCollection'][0]['instruction'] == 'BUY']
    replacement_b = new_buys_b[0] if len(new_buys_b) == 1 else None
    checks.append(Check("node B: exactly one new real MARKET BUY replacement order exists for this "
                        "leg, and it IS actually FILLED at the broker",
                        replacement_b is not None and broker.orders[replacement_b]['status'] == 'FILLED',
                        f"market_buys={new_buys_b} status={broker.orders.get(replacement_b, {}).get('status')}"))
    gap_events_b = [e for e in db.get_coverage_events(scenario_key='gap_resize')
                   if e['result'] == 'replaced' and e['node_id'] == node_b['id']]
    checks.append(Check("node B: gap_resize still fired 'replaced' -- logged before the confirmation "
                        "poll even starts, so it's unaffected by our own read being delayed",
                        len(gap_events_b) == 1, f"events={[(e['result']) for e in gap_events_b]}"))
    checks.append(Check("node B: 'not confirmed -- will be caught by the next check_auto_fills poll' "
                        "alert posted", any('fill not confirmed' in p for p in posted), f"posted={posted}"))
    pending_b_after_gap_resize = [p for p in db.get_pending_buys() if p['ticker'] == TICKER
                                  and p['node']['id'] == node_b['id']]
    checks.append(Check("node B: pending_buys row NOT cleared by check_gap_resize itself (confirmation "
                        "never landed inline) -- but its order_id IS repointed to the real replacement, "
                        "so the fallback poll below has something real to find",
                        len(pending_b_after_gap_resize) == 1
                        and pending_b_after_gap_resize[0]['order_id'] == replacement_b,
                        f"pending={pending_b_after_gap_resize} expected_order_id={replacement_b}"))
    checks.append(Check("node B: no position opened by check_gap_resize itself -- reconciliation was "
                        "genuinely deferred, not silently skipped or double-counted",
                        db.get_open_position_by_wl_id(node_b['id']) is None))

    # ---- the real daemon's OWN later poll, run exactly as active_signals.py's
    # run_loop calls it every cycle, now that the artificial delay is gone ----
    say("[node B] running the real signals_notify.check_auto_fills([]) -- the real fallback "
        "check_gap_resize's own docstring names for this exact case")
    check_auto_fills_error = None
    try:
        notify.check_auto_fills([])
    except Exception as e:
        check_auto_fills_error = f"{type(e).__name__}: {e}"
    observations['check_auto_fills_error'] = check_auto_fills_error
    say(f"[node B] check_auto_fills([]) -> "
        f"{'raised: ' + check_auto_fills_error if check_auto_fills_error else 'completed without raising'}")
    checks.append(Check("node B: check_auto_fills (the real fallback) resolves the SAME replacement "
                        "order via the repointed order_id and reconciles it -- closing the exact loop "
                        "check_gap_resize's own comment promises",
                        check_auto_fills_error is None
                        and db.get_open_position_by_wl_id(node_b['id']) is not None
                        and [p for p in db.get_pending_buys() if p['ticker'] == TICKER
                             and p['node']['id'] == node_b['id']] == [],
                        f"error={check_auto_fills_error!r} "
                        f"position={db.get_open_position_by_wl_id(node_b['id'])}"))
    pos_b = db.get_open_position_by_wl_id(node_b['id'])
    checks.append(Check("node B: the reconciled position's entry_price is the real replacement fill "
                        "price (the post-gap quote), not the original stale signal price",
                        pos_b is not None and abs(pos_b['entry_price'] - p_gap) < 0.0005,
                        f"entry_price={pos_b['entry_price'] if pos_b else None} expected={p_gap:.4f}"))

    observations['node_a_wl_id'] = node_a['id']
    observations['node_b_wl_id'] = node_b['id']
    observations['p0'] = p0
    observations['p_low'] = p_low
    observations['p_gap'] = p_gap
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT wl.id AS wl_id, wl.account,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='gap_resize'
         AND result='replaced' AND node_id=wl.id) AS gap_resize_replaced,
       (SELECT COUNT(*) FROM open_positions WHERE wl_id=wl.id) AS open_positions
  FROM watch_list wl
 WHERE wl.ticker = ?
 ORDER BY wl.id
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly 2 nodes (A, B), each with
    exactly one 'replaced' gap_resize event AND exactly one open position --
    node A's confirmed inline by check_gap_resize itself, node B's confirmed
    by the real check_auto_fills fallback after its own confirmation poll
    was exhausted -- directly from the harness DB."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    if len(rows) != 2:
        return False, rows
    ok = all(r['gap_resize_replaced'] == 1 and r['open_positions'] == 1 for r in rows)
    return ok, rows
