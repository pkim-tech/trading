"""Phase 2 scenario: `entry_abandon_timeout`, proving `check_entry_abandon`
(signals_notify.py:1538) end to end -- a never-bouncing trailing-buy timing
out, cancelling the real resting order, and correctly branching on every
outcome a real `schwab_client.cancel_order` round-trip can produce.

Real motivation: `cancel_order` (schwab_client.py:1017) has NEVER had a
production caller before `check_entry_abandon` (added 2026-07-31, the
[HIGHEST]-severity item left open by that session's audit -- see
docs/deep_backlog.md's 2026-07-31 entries). Every other real order-touching
function in this codebase migrated onto atomic replace-in-one-call
(replace_equity_order_with_market / replace_order_with_trailing_sell)
specifically to avoid a naked cancel-then-something-else window -- this is
the one deliberate exception, since an abandoned entry has nothing to
replace the cancelled order WITH. It has never fired against a real broker
in production and tests/fake_broker.py's own get_order() comment (added
2026-07-31, "added while building a fake_broker scenario test for
check_entry_abandon") shows this exact gap was already known and still open.
The existing pytest tier (tests/test_entry_abandon*.py) proves the branch
logic against a mocked schwab_client; what's new here is the REAL two-round-
trip cancel+confirm sequence (schwab_client.cancel_order's own
_confirm_order_status poll) against a REAL (fake) broker order book, plus the
one branch no mock can honestly model: the order actually filling in the
gap between the cancel request and the confirm poll (the raced_fill branch).

Eight legs, eight nodes, one shared FakeBroker account (fv_cash) -- every leg
independent (distinct wl_id/order_id), all driven through ONE real
check_entry_abandon() call so the function's own top-of-loop iteration over
every pending row (as active_signals.py's real poll loop does every cycle)
is what's under test, not eight isolated invocations:

  A  CLEAN CANCEL -- the ordinary case. Real order resting, timeout hit,
     schwab_client.cancel_order's real 2-round-trip sequence (cancel_order +
     _confirm_order_status poll) confirms CANCELED.
     => coverage_events['entry_abandon_timeout'] = 'abandoned', detail
        mentions did_cancel=True (via the posted message)             <-- checked
     => pending row cleared, broker order status CANCELED               <-- checked

  B  DRY-RUN -- node.state='dry_run' (_effectively_dry_run=True), no real
     order ever existed (order_id=None, matching the real convention --
     see update_dry_run_buys' docstring: schwab_client's dry_run
     short-circuit never returns a real order id). No cancel_order call
     should be attempted at all.
     => coverage_events['entry_abandon_timeout'] = 'abandoned', posted
        message says "no real order existed to cancel"                 <-- checked
     => pending row cleared, zero cancel_order-shaped broker mutation    <-- checked

  C  NO ORDER ID ON FILE -- the manual "Trailing Buy Order Placed" Slack
     flow (order_placed=True, order_id=None, a REAL/non-dry_run account).
     Per check_entry_abandon's own docstring this must NOT be treated as
     dry_run's "nothing to cancel" case -- a real order may be resting with
     no id on file, so the row must be left alone, not silently cleared.
     => coverage_events['entry_abandon_timeout'] = 'no_order_id_on_file'  <-- checked
     => pending row NOT cleared (still there next poll)                 <-- checked

  D  UNRECOGNIZED ACCOUNT -- pb['node']['account'] (the pinned signal-time
     snapshot check_entry_abandon deliberately reads instead of a live
     watch_list re-fetch, per its own docstring) names an account absent
     from schwab_safety.ACCOUNTS. Can't tell whether a real order might be
     resting -- fail closed.
     => coverage_events['entry_abandon_timeout'] = 'unrecognized_account' <-- checked
     => pending row NOT cleared                                          <-- checked

  E  CANCEL REQUEST ITSELF FAILS -- schwab_client.cancel_order's own
     `_get_client().cancel_order(...)` call raises (network/API error, not
     a broker-confirmed outcome). The exception must be caught, alerted,
     and the row left in place for a retry next poll -- not propagated
     (which would kill the whole check_entry_abandon loop mid-iteration
     and starve every OTHER pending row's own timeout check that cycle).
     => coverage_events['entry_abandon_timeout'] = 'cancel_failed'        <-- checked
     => pending row NOT cleared, order left WORKING at the broker         <-- checked
     => the REST of the loop still ran (leg A/etc.'s own events are
        present) -- proves E's exception didn't abort the batch          <-- checked

  F  CANCEL ACCEPTED BUT UNCONFIRMED -- the real cancel_order() HTTP call
     succeeds (broker-side status genuinely flips to CANCELED), but the
     post-cancel confirmation poll (_confirm_order_status -> get_order)
     fails on every attempt, so schwab_client.cancel_order returns
     (r, None) -- "unconfirmed," per its own docstring. check_entry_abandon
     must fail closed here too: a cancel HTTP 200 doesn't mean cancelled
     (the docstring's own citation: "confirmed live 2026-07-23 night").
     => coverage_events['entry_abandon_timeout'] = 'cancel_unconfirmed'   <-- checked
     => pending row NOT cleared (even though the broker-side order IS
        actually CANCELED by this point -- the row stays in place because
        WE never got confirmation of that, matching the fail-closed
        contract; next poll's fresh cancel_order attempt against an
        already-CANCELED order is itself proof the retry path is safe --
        FakeBroker.cancel_order is a no-op on a terminal-status order)    <-- checked

  G  RACED FILL -- the real bounce happens in the exact gap between
     check_entry_abandon deciding to abandon and the cancel request
     landing (order status is already FILLED by the time cancel_order's
     own confirm poll reads it). Per automation_principles.md #1 (never
     discard real broker truth), this must reconcile as a genuine
     position, NOT an abandoned entry -- bypassing the auto_fill_detection
     opt-in gate deliberately, per the function's own docstring.
     => coverage_events['entry_abandon_timeout'] = 'raced_fill'           <-- checked
     => coverage_events['buy_fill_reconciled'] = 'opened' for this node   <-- checked
     => a REAL position opens at the correct fill price/shares, pending
        row cleared by _reconcile_buy_fill (not by check_entry_abandon
        itself -- the function `continue`s right after, per its own
        code -- see FOUND note below)                                    <-- checked

  H  GAP-RESIZE RACE GUARD -- the exact race this session's brief asks to
     confirm is closed: pending_buys.gap_resize_date == today (as if
     check_gap_resize, running earlier in the SAME poll iteration, had
     JUST replaced this row's resting trailing-buy with a real MARKET
     order moments/seconds ago). check_entry_abandon must `continue` at
     the very TOP of its loop for this row -- before even computing
     bars_held -- so it can never cancel that brand-new replacement order
     even though this node's bars_held is (via the harness's own
     compute._bars_held patch below) also over threshold.
     => ZERO coverage_events['entry_abandon_timeout'] rows for this node
        (not even a 'skipped' event -- the guard is a silent `continue`,
        confirmed by absence, not a logged branch)                       <-- checked
     => pending row COMPLETELY UNTOUCHED: still present, order still
        WORKING at the broker (not cancelled)                            <-- checked

FIXED (found 2026-08-16, fixed 2026-08-16) -- a real, if minor, observability
gap in check_entry_abandon (signals_notify.py ~1707-1711): the 'abandoned'
coverage_event's `detail` field used to record only
`f"bars_held={bars_held} max={node['max_hold_hours']}"` -- it never recorded
`did_cancel` itself, even though the function computed it and used it to
choose the Slack message wording two lines later. A coverage-query trying to
distinguish leg A's real-cancel 'abandoned' from leg B's dry-run
no-real-order 'abandoned' by querying coverage_events alone (rather than
cross-referencing the posted Slack text) could not tell the two apart. Fixed
by extending `detail` to `f"...did_cancel={did_cancel}"` -- paired
independent-cold + contextual Opus review (required since the fix touches
signals_notify.py) ran clean, no confirmed findings. Legs A and B below now
assert on the coverage_event `detail` field directly (in addition to the
pre-existing posted-Slack-text checks, kept as-is since they're still real,
independent proof of the same distinction).

bars_held is never derivable from real cached data for a synthetic harness
ticker (compute._load_cache reads a real CSV under signals_config.RESEARCH_DIR
that will never exist for TEST_FAKE_VENUE_SCENARIO, and writing one there
would itself be a real production-path write the isolation tripwire exists to
catch) -- signals_compute._bars_held is monkeypatched directly instead,
exactly the established convention scenarios_drought_handoff.py uses for
signals_compute.compute_buy_signal (an env/data fact orthogonal to the
mechanism under test, not a bypass of check_entry_abandon's own logic).

Entry-side state (every resting order) is SEEDED, not placed through the real
BUY path -- same accepted caveat as every sibling Phase 2 scenario; this
scenario's target is the abandon/cancel chain, not entry placement.
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
MAX_HOLD_HOURS = 4
# Patched onto signals_compute._bars_held (see module docstring) -- must be
# comfortably >= MAX_HOLD_HOURS for every leg to actually hit the timeout.
PATCHED_BARS_HELD = 999


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ''
    required: bool = True

    def __post_init__(self):
        self.ok = bool(self.ok)


def _add_node(version_suffix, state='live'):
    import signals_db as db

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout',
                version=f'fake_venue_entryabandon_{version_suffix}',
                window=20, take_profit=10, stop_loss=FIXED_SL_PCT, max_hold_hours=MAX_HOLD_HOURS,
                state=state, account=CASH_ALIAS, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=FIXED_SL_PCT,
                label='fake-venue harness node (entry_abandon_timeout)')
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER
            and n['version'] == f'fake_venue_entryabandon_{version_suffix}'][0]


def _seed_pending(db, node, order_id=None, order_placed=True, account_override=None):
    """Mirrors add_pending_buy's real call shape, with an optional post-hoc
    account override on the frozen node_json snapshot (leg D) -- check_entry_
    abandon reads pb['node'], the pinned signal-time copy, not a live
    watch_list re-fetch (see its own docstring), so this is the faithful way
    to reproduce "the account on file at signal time doesn't resolve today,"
    not a hack around the real code's own documented behavior."""
    node_for_snapshot = dict(node, account=account_override) if account_override else node
    sig = {'current_price': PRICE, 'last_bar': datetime.now()}
    db.add_pending_buy(node_for_snapshot, sig, channel=None, ts=None, order_id=order_id)
    if order_placed:
        db.mark_pending_buy_placed_by_wl_id(node['id'])
    return db.get_pending_buy_by_wl_id(node['id'])


PRICE = None  # set in run(), read by _seed_pending -- module-level since add_pending_buy needs a price


def run(price=None, verbose=True):
    """Runs the scenario against the already-isolated, already-imported
    environment. Returns (checks, observations)."""
    global PRICE
    import schwab_client
    import schwab_safety
    import signals_compute
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
    PRICE = price
    broker.set_cash_balance(CASH_ALIAS, 100_000.0)
    say(f"[setup] {TICKER} quote seeded at ${price:.4f}")

    schwab_safety.enable_auto_fill_detection(TICKER)

    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking schwab_safety._is_trading_day "
            "True for this run (orthogonal to the mechanism under test, same override every other "
            "Phase 2 scenario uses)")
        schwab_safety._is_trading_day = lambda date_str: True

    # See module docstring: no real cached hourly data will ever exist for a
    # synthetic ticker, and writing one under RESEARCH_DIR would itself be a
    # production-path write. This is the harness's equivalent of
    # scenarios_drought_handoff.py's compute_buy_signal monkeypatch.
    signals_compute._bars_held = lambda df_hourly, signal_time: PATCHED_BARS_HELD
    say(f"[setup] signals_compute._bars_held patched to always return {PATCHED_BARS_HELD} "
        f"(>= MAX_HOLD_HOURS={MAX_HOLD_HOURS} for every leg)")

    posted = []

    def _capture(text=None, *a, **kw):
        posted.append(text if text is not None else (a[0] if a else kw.get('text')))
        return ('C0FAKEVENUE', '9999.1')

    # Both bindings needed -- schwab_client.py's own cancel_order() posts its
    # confirm/unconfirmed/warning messages via its own local `_post_message`
    # import, same gotcha scenarios_replace_target_mismatch.py's module
    # docstring documents.
    notify._post_message = _capture
    schwab_client._post_message = _capture

    shares = max(int(NODE_NOTIONAL // price), 1)

    # ============================================================== LEG A
    say("[leg A] clean cancel -- real order resting, timeout hit, real "
        "cancel_order round-trip confirms CANCELED")
    node_a = _add_node('a')
    order_a = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'BUY', shares, trail_offset=1.0)
    _seed_pending(db, node_a, order_id=order_a)
    checks.append(Check("leg A setup: order resting WORKING",
                        broker.orders[order_a]['status'] == 'WORKING'))

    # ============================================================== LEG B
    say("[leg B] dry-run node -- no real order ever existed")
    node_b = _add_node('b', state='dry_run')
    _seed_pending(db, node_b, order_id=None)

    # ============================================================== LEG C
    say("[leg C] manual 'Trailing Buy Order Placed' flow -- order_placed=True, "
        "no order_id on file, REAL account")
    node_c = _add_node('c')
    order_c = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'BUY', shares, trail_offset=1.0)
    _seed_pending(db, node_c, order_id=None)  # no id captured, mirroring the manual flow exactly

    # ============================================================== LEG D
    say("[leg D] unrecognized account on the pinned signal-time node snapshot")
    node_d = _add_node('d')
    order_d = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'BUY', shares, trail_offset=1.0)
    _seed_pending(db, node_d, order_id=order_d, account_override='fv_ghost_unrecognized')

    # ============================================================== LEG E
    say("[leg E] the cancel_order() call itself raises (network/API error)")
    node_e = _add_node('e')
    order_e = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'BUY', shares, trail_offset=1.0)
    _seed_pending(db, node_e, order_id=order_e)
    real_cancel_order = broker.cancel_order

    def _cancel_order_raising(order_id, account_hash):
        if order_id == order_e:
            raise RuntimeError("fake_venue: simulated network failure on cancel_order")
        return real_cancel_order(order_id, account_hash)

    broker.cancel_order = _cancel_order_raising

    # ============================================================== LEG F
    say("[leg F] cancel_order() succeeds at the broker but the post-cancel "
        "confirm poll fails every attempt (real 200, unconfirmed status)")
    node_f = _add_node('f')
    order_f = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'BUY', shares, trail_offset=1.0)
    _seed_pending(db, node_f, order_id=order_f)
    real_get_order = broker.get_order

    def _get_order_raising(order_id, account_hash):
        if order_id == order_f:
            raise RuntimeError("fake_venue: simulated confirm-poll failure")
        return real_get_order(order_id, account_hash)

    broker.get_order = _get_order_raising

    # ============================================================== LEG G
    say("[leg G] the real bounce lands in the gap -- order is already FILLED "
        "by the time our cancel_order call reads its status")
    node_g = _add_node('g')
    order_g = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'BUY', shares, trail_offset=1.0)
    _seed_pending(db, node_g, order_id=order_g)
    fill_price_g = round(price * 0.995, 4)
    broker.force_fill(order_g, fill_price_g)
    say(f"[leg G] broker: order {order_g} FILLED @ ${fill_price_g:.4f} moments before check_entry_abandon runs")
    checks.append(Check("leg G setup: order already FILLED before the abandon check runs",
                        broker.orders[order_g]['status'] == 'FILLED'))

    # ============================================================== LEG H
    say("[leg H] gap-resize race guard -- pending_buys.gap_resize_date == today, "
        "as if check_gap_resize just replaced this row's order in the SAME poll cycle")
    node_h = _add_node('h')
    order_h = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'BUY', shares, trail_offset=1.0)
    pending_h = _seed_pending(db, node_h, order_id=order_h)
    today_str = datetime.now().strftime('%Y-%m-%d')
    db.mark_gap_resize_attempted(pending_h['id'], today_str)
    pending_h_after = db.get_pending_buy_by_wl_id(node_h['id'])
    checks.append(Check("leg H setup: gap_resize_date stamped for today on this row",
                        pending_h_after['gap_resize_date'] == today_str,
                        f"gap_resize_date={pending_h_after['gap_resize_date']!r} expected={today_str!r}"))

    checks.append(Check("eight independent pending buys queued for one real check_entry_abandon() call",
                        len([p for p in db.get_pending_buys() if p['ticker'] == TICKER]) == 8))

    # ---------------------------------------------------------- the call
    say("[call] notify.check_entry_abandon() -- one real pass over all eight pending rows")
    try:
        notify.check_entry_abandon()
        observations['check_entry_abandon_call'] = 'completed without raising'
    except Exception as e:
        observations['check_entry_abandon_call'] = f"{type(e).__name__}: {e}"
    finally:
        broker.cancel_order = real_cancel_order
        broker.get_order = real_get_order
    say(f"[call] check_entry_abandon() -> {observations['check_entry_abandon_call']}")
    checks.append(Check("check_entry_abandon() did not raise across all eight rows",
                        observations['check_entry_abandon_call'] == 'completed without raising'))

    events = db.get_coverage_events(scenario_key="entry_abandon_timeout")
    by_node = {}
    detail_by_node = {}
    for e in events:
        by_node.setdefault(e['node_id'], []).append(e['result'])
        if e['result'] == 'abandoned':
            detail_by_node[e['node_id']] = e['detail']

    # ---------------------------------------------------------------- A
    checks.append(Check("leg A: 'abandoned' fired for the clean-cancel node",
                        by_node.get(node_a['id']) == ['abandoned'],
                        f"results={by_node.get(node_a['id'])}"))
    checks.append(Check("leg A: broker order confirmed CANCELED",
                        broker.orders[order_a]['status'] == 'CANCELED',
                        f"status={broker.orders[order_a]['status']}"))
    checks.append(Check("leg A: pending row cleared",
                        db.get_pending_buy_by_wl_id(node_a['id']) is None))
    checks.append(Check("leg A: posted message claims a real order was cancelled",
                        any(f"{TICKER}" in p and "resting order cancelled" in p for p in posted),
                        f"posted={posted}"))
    checks.append(Check("leg A: coverage_event 'abandoned' detail records did_cancel=True directly "
                        "(FIXED: previously only the posted Slack text carried this distinction, "
                        "not the coverage_events row itself -- see module docstring)",
                        detail_by_node.get(node_a['id'], '').endswith('did_cancel=True'),
                        f"detail={detail_by_node.get(node_a['id'])!r}"))

    # ---------------------------------------------------------------- B
    checks.append(Check("leg B: 'abandoned' fired for the dry-run node",
                        by_node.get(node_b['id']) == ['abandoned'],
                        f"results={by_node.get(node_b['id'])}"))
    checks.append(Check("leg B: pending row cleared",
                        db.get_pending_buy_by_wl_id(node_b['id']) is None))
    checks.append(Check("leg B: posted message correctly says no real order existed "
                        "(NOT the misleading 'resting order cancelled' claim)",
                        any(f"{TICKER}" in p and "no real order existed to cancel" in p for p in posted),
                        f"posted={posted}"))
    checks.append(Check("leg B: coverage_event 'abandoned' detail records did_cancel=False directly "
                        "(FIXED: distinguishes this dry-run no-op from leg A's real cancel purely via "
                        "coverage_events, no cross-referencing the Slack text required)",
                        detail_by_node.get(node_b['id'], '').endswith('did_cancel=False'),
                        f"detail={detail_by_node.get(node_b['id'])!r}"))

    # ---------------------------------------------------------------- C
    checks.append(Check("leg C: 'no_order_id_on_file' fired",
                        by_node.get(node_c['id']) == ['no_order_id_on_file'],
                        f"results={by_node.get(node_c['id'])}"))
    checks.append(Check("leg C: pending row NOT cleared (real order may still be resting, "
                        "no id to target a cancel at)",
                        db.get_pending_buy_by_wl_id(node_c['id']) is not None))
    checks.append(Check("leg C: the manually-placed order at the broker was never touched",
                        broker.orders[order_c]['status'] == 'WORKING',
                        f"status={broker.orders[order_c]['status']}"))

    # ---------------------------------------------------------------- D
    checks.append(Check("leg D: 'unrecognized_account' fired",
                        by_node.get(node_d['id']) == ['unrecognized_account'],
                        f"results={by_node.get(node_d['id'])}"))
    checks.append(Check("leg D: pending row NOT cleared",
                        db.get_pending_buy_by_wl_id(node_d['id']) is not None))
    checks.append(Check("leg D: the order was never touched (can't safely target a cancel "
                        "at an account we can't resolve)",
                        broker.orders[order_d]['status'] == 'WORKING',
                        f"status={broker.orders[order_d]['status']}"))

    # ---------------------------------------------------------------- E
    checks.append(Check("leg E: 'cancel_failed' fired",
                        by_node.get(node_e['id']) == ['cancel_failed'],
                        f"results={by_node.get(node_e['id'])}"))
    checks.append(Check("leg E: pending row NOT cleared (retry next poll)",
                        db.get_pending_buy_by_wl_id(node_e['id']) is not None))
    checks.append(Check("leg E: the order is still WORKING at the broker (the raised exception "
                        "happened before any broker-side mutation)",
                        broker.orders[order_e]['status'] == 'WORKING',
                        f"status={broker.orders[order_e]['status']}"))
    checks.append(Check("leg E: a real exception during one row's cancel attempt did NOT abort "
                        "the rest of the batch -- leg A's own event is still present",
                        node_a['id'] in by_node,
                        "proves the per-row try/except around cancel_order actually isolates failures"))

    # ---------------------------------------------------------------- F
    checks.append(Check("leg F: 'cancel_unconfirmed' fired",
                        by_node.get(node_f['id']) == ['cancel_unconfirmed'],
                        f"results={by_node.get(node_f['id'])}"))
    checks.append(Check("leg F: pending row NOT cleared, despite the broker-side order actually "
                        "being CANCELED by this point -- fail-closed on our own confirmation, "
                        "not on the broker's true state (the real 2026-07-23 incident this "
                        "function's docstring cites)",
                        db.get_pending_buy_by_wl_id(node_f['id']) is not None
                        and broker.orders[order_f]['status'] == 'CANCELED',
                        f"pending_row_present={db.get_pending_buy_by_wl_id(node_f['id']) is not None} "
                        f"broker_status={broker.orders[order_f]['status']}"))

    # ---------------------------------------------------------------- G
    checks.append(Check("leg G: 'raced_fill' fired, not 'abandoned' -- a real fill that landed "
                        "in the cancel gap is never discarded as a timeout",
                        by_node.get(node_g['id']) == ['raced_fill'],
                        f"results={by_node.get(node_g['id'])}"))
    pos_g = db.get_open_position_by_wl_id(node_g['id'])
    checks.append(Check("leg G: a REAL position opened from the raced fill, at the exact "
                        "fill price/shares",
                        pos_g is not None and pos_g['shares'] == shares
                        and abs(pos_g['entry_price'] - fill_price_g) < 0.0005,
                        f"shares={pos_g['shares'] if pos_g else None} expected={shares} "
                        f"entry={pos_g['entry_price'] if pos_g else None} expected={fill_price_g:.4f}"))
    checks.append(Check("leg G: pending row cleared -- by _reconcile_buy_fill (the raced_fill "
                        "branch itself only `continue`s, per its own code; the row's clearing is a "
                        "side effect of the reconciliation call, not a separate clear_pending_buy_by_"
                        "wl_id inside check_entry_abandon)",
                        db.get_pending_buy_by_wl_id(node_g['id']) is None))
    buy_fill_events_g = db.get_coverage_events(scenario_key="buy_fill_reconciled")
    opened_g = [e for e in buy_fill_events_g if e['result'] == 'opened' and e['node_id'] == node_g['id']]
    checks.append(Check("leg G: buy_fill_reconciled fired 'opened' for the raced-fill node too "
                        "(the same real reconciliation path every other fill goes through)",
                        len(opened_g) == 1, f"events={[(e['result'], e['node_id']) for e in buy_fill_events_g]}"))

    # ---------------------------------------------------------------- H
    checks.append(Check("leg H: ZERO entry_abandon_timeout events for this node -- the guard is a "
                        "silent `continue` at the top of the loop, confirmed by absence",
                        node_h['id'] not in by_node,
                        f"results={by_node.get(node_h['id'])}"))
    checks.append(Check("leg H: pending row completely untouched (gap_resize_date still stamped, "
                        "row still present)",
                        db.get_pending_buy_by_wl_id(node_h['id']) is not None))
    checks.append(Check("leg H: the resting order was never cancelled -- the exact real order "
                        "check_gap_resize just replaced moments earlier must survive this function "
                        "in the same poll cycle",
                        broker.orders[order_h]['status'] == 'WORKING',
                        f"status={broker.orders[order_h]['status']}"))

    observations['node_ids'] = {k: v['id'] for k, v in {
        'a': node_a, 'b': node_b, 'c': node_c, 'd': node_d,
        'e': node_e, 'f': node_f, 'g': node_g, 'h': node_h,
    }.items()}
    observations['price'] = price
    observations['shares'] = shares
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT wl.id AS wl_id, wl.version, wl.state,
       (SELECT GROUP_CONCAT(result) FROM coverage_events
         WHERE scenario_key='entry_abandon_timeout' AND node_id=wl.id) AS abandon_results,
       (SELECT detail FROM coverage_events
         WHERE scenario_key='entry_abandon_timeout' AND node_id=wl.id
           AND result='abandoned' LIMIT 1) AS abandon_detail,
       (SELECT COUNT(*) FROM pending_buys WHERE wl_id=wl.id) AS pending_rows,
       (SELECT COUNT(*) FROM open_positions WHERE wl_id=wl.id) AS open_positions
  FROM watch_list wl
 WHERE wl.ticker = ?
 ORDER BY wl.version
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires all 8 nodes on file, with each leg's
    expected abandon_results/pending_rows/open_positions state, directly from
    the harness DB -- not from the in-process checks above. Legs A/B also
    require the 'abandoned' event's own `detail` field to carry the correct
    did_cancel value (FIXED: previously only the posted Slack text carried
    this distinction, not the coverage_events row -- see module docstring)."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    if len(rows) != 8:
        return False, rows
    by_suffix = {r['version'].rsplit('_', 1)[-1]: r for r in rows}
    expected = {
        'a': dict(abandon_results='abandoned', pending_rows=0, open_positions=0, did_cancel='True'),
        'b': dict(abandon_results='abandoned', pending_rows=0, open_positions=0, did_cancel='False'),
        'c': dict(abandon_results='no_order_id_on_file', pending_rows=1, open_positions=0, did_cancel=None),
        'd': dict(abandon_results='unrecognized_account', pending_rows=1, open_positions=0, did_cancel=None),
        'e': dict(abandon_results='cancel_failed', pending_rows=1, open_positions=0, did_cancel=None),
        'f': dict(abandon_results='cancel_unconfirmed', pending_rows=1, open_positions=0, did_cancel=None),
        'g': dict(abandon_results='raced_fill', pending_rows=0, open_positions=1, did_cancel=None),
        'h': dict(abandon_results=None, pending_rows=1, open_positions=0, did_cancel=None),
    }
    ok = all(
        suffix in by_suffix
        and by_suffix[suffix]['abandon_results'] == exp['abandon_results']
        and by_suffix[suffix]['pending_rows'] == exp['pending_rows']
        and by_suffix[suffix]['open_positions'] == exp['open_positions']
        and (exp['did_cancel'] is None
             or by_suffix[suffix]['abandon_detail'].endswith(f"did_cancel={exp['did_cancel']}"))
        for suffix, exp in expected.items()
    )
    return ok, rows
