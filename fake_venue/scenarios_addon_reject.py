"""Phase 2 scenario: `addon_reject` -- a deterministic broker REJECTION of the
real margin add-on-at-arm BUY (signals_notify.check_addon_trigger_real ->
schwab_client.place_equity_buy), closing the condition-based backlog item
that was waiting on a real WFH-day test against Schwab (docs/backlog_cache.md:
"Parked 2026-08-07 (evening), condition-based trigger set 2026-08-15 (revisit
when: user has a WFH day free to safely test against real broker rejections)
... study real Schwab rejection behavior via scripts/live_sanity_check.py,
then calibrate a new fake_broker.force_reject_next_order() to match"). The
harness can already fake arbitrary broker responses at the wire-format layer,
so this closes the gap deterministically instead of waiting on that day.

CALIBRATION NOTE (read before trusting this as final): no real add-on-BUY
rejection has ever been observed live -- the WFH-day test this scenario
substitutes for hasn't happened. Rather than inventing a rejection shape,
tests/fake_broker.py's new force_reject_next_order() reuses the ONE real
Schwab rejection already documented in this codebase: schwab_client.py's
_ORDER_CONFIRM_POLL_ATTEMPTS comment records a real 2026-07-24 incident (an
oversized BUY returned HTTP 201 with no exception, then resolved REJECTED
~0.3-0.7s later via the async order-status poll) -- not an immediate/
synchronous place_order failure. This scenario's rejection is the identical
shape: place_order succeeds (gets a real order_id), the order's own status is
REJECTED. That's a reasonable proxy specifically because
check_addon_trigger_real's rejection-handling machinery
(place_equity_buy -> _submit_order_with_retry [succeeds] ->
_post_order_confirmation -> _confirm_order_status polls REJECTED ->
OrderRejected raised -> the generic `except Exception` branch) is IDENTICAL
regardless of which real order class triggers it -- nothing in
schwab_client.py's placement/confirmation path distinguishes "add-on leg"
once past approve_and_record's is_addon_leg preconditions. What this scenario
does NOT prove: whether a real add-on-scale order's rejection is genuinely
always async/late like the 2026-07-24 incident, or could arrive differently
for this specific order class (smaller size, is_addon_leg-tagged, etc.) --
that confirmation is still gated on the real WFH-day test. This closes the
CODE-HANDLING gap deterministically; it does not close the
real-rejection-shape-confirmation gap, which stays open per
docs/backlog_cache.md until that test runs.

Shape (one fake node, one fake account, one already-open/armed CORE position
-- entry-side state is SEEDED, not placed through the real BUY path, same
accepted caveat as every other Phase 2 scenario; this scenario's target is
the add-on BUY's own rejection handling, not core entry):

  node A  (fv_cash, margin_capable)  addon_enabled=1, one open CORE position,
                                      trail_state.trailing=True (the real
                                      precondition check_addon_trigger_real
                                      requires before ever attempting a leg)

  fake_broker.force_reject_next_order('REJECTED') armed BEFORE the trigger,
  so the very next place_order() call (the add-on's own MARKET BUY) is the
  one that comes back REJECTED -- nothing else places a broker order in this
  scenario's sequence (unlike the notify_trailing_activated path, which would
  also place the parent's own protective SELL first and consume the one-shot
  flag on the wrong order; called directly for that reason, same as several
  other Phase 2 scenarios call a real notify.* function directly rather than
  driving the full arm flow).

  -> notify.check_addon_trigger_real(pos, current_price)   [real function,
                                                              called directly]
     -> schwab_safety.approve_and_record(is_addon_leg=True) verifies all five
        preconditions against the DB and PASSES them (this scenario is
        deliberately NOT testing the precondition gate -- see
        scenarios_replace_target_mismatch.py / the existing fake_broker addon
        tests for that) => coverage_events['addon_double_buy_exemption'] =
        'preconditions_passed'                                   <-- checked
     -> schwab_client.place_equity_buy places the real MARKET BUY;
        _submit_order_with_retry succeeds (gets a real order_id); the async
        _confirm_order_status poll sees REJECTED -> raises OrderRejected
     -> check_addon_trigger_real's generic `except Exception as e:` branch
        (NOT the SafetyViolation branch -- OrderRejected is not a
        SafetyViolation, so this exercises a different code path than every
        existing "blocked" addon test) fires:
        => coverage_events['addon_entry_placement'] = 'failed_unexpectedly',
           detail containing the OrderRejected message              <-- checked
        => a real Slack alert posts ("add-on leg placement failed
           unexpectedly: ..."), captured via the same notify._post_message /
           schwab_client._post_message double-patch
           scenarios_replace_target_mismatch.py uses (the REJECTED alert
           itself is posted from inside schwab_client._post_order_confirmation,
           the wrapper alert from signals_notify -- two distinct real posts)
                                                                      <-- checked

THE ACTUAL QUESTION THIS SCENARIO ANSWERS: does the `addon_legs` staging row
get created and then left stuck 'open' by a rejected order, or does it never
get written at all? Reading check_addon_trigger_real
(signals_notify.py:2657-2698): db.open_addon_leg() is only ever called AFTER
place_equity_buy returns successfully (either a real confirmed order_id, or
(None, None) for a dry_run ACCOUNT specifically) -- a rejection raised INSIDE
place_equity_buy, before it returns, means the try/except at the call site
catches it and returns immediately, and no addon_legs INSERT is ever reached.
So the real answer is stronger than "closes out cleanly": there is nothing to
close out -- the row is never created in the first place. This scenario
proves that ordering holds for real (zero addon_legs rows for the ticker
post-rejection, verified via a fresh read-only DB connection in verify_proof,
not trusted from in-process return values) rather than just from reading the
code, and proves the PARENT core position is left completely untouched (still
open, same share count) -- a rejected add-on must never corrupt or delete the
position it was trying to add on to.
"""
from dataclasses import dataclass
from datetime import datetime

from fake_venue import venue
from fake_venue.scenarios_meta import CASH_ALIAS, PRICE_SOURCE_TICKER, TICKER

FAKE_ACCOUNTS = [
    dict(alias=CASH_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='cash', margin_capable=1),
]
NODE_NOTIONAL = 2_000
PARENT_SHARES = 20


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

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version='fake_venue_addon_reject',
                window=20, take_profit=10, stop_loss=1, max_hold_hours=56,
                state='live', account=CASH_ALIAS, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                label='fake-venue harness node (addon_reject)')
    node = [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]
    with db._conn() as c:
        c.execute("UPDATE watch_list SET addon_enabled=1 WHERE id=?", (node['id'],))
        c.commit()
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]


def _open_core_position(node, price, shares):
    """Mirrors tests/test_fake_broker_addon_entry_scenario.py's own
    _open_core_position helper: entry-side state seeded directly (not placed
    through the real BUY path -- accepted Phase 2 caveat), with
    trail_state.trailing=True set explicitly since in real production
    signals_compute.check_sell_condition persists that BEFORE
    notify_trailing_activated (and this scenario's tail,
    check_addon_trigger_real) ever runs -- calling check_addon_trigger_real
    directly bypasses that real precondition-setting step, so it must be
    seeded here to faithfully reproduce it."""
    import signals_db as db

    now = datetime.now()
    db.open_position(node, signal_price=price, signal_time=now, entry_price=price,
                      entry_time=now, shares=shares)
    with db._conn() as c:
        c.execute("UPDATE open_positions SET account=? WHERE ticker=?", (CASH_ALIAS, node['ticker']))
        c.commit()
    pos = db.get_open_position(node['ticker'])
    db.update_position_trail_state(pos['id'], {'trailing': True, 'peak': price})
    return db.get_open_position(node['ticker'])


def run(price=None, verbose=True):
    """Runs the scenario against the already-isolated, already-imported
    environment. Returns (checks, observations)."""
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
    broker = venue.install_fake_broker([CASH_ALIAS])
    venue.seed_account_number_env({CASH_ALIAS: "88880003"})
    price = venue.seed_quote(broker, TICKER, price, price_source_ticker=PRICE_SOURCE_TICKER)
    broker.set_cash_balance(CASH_ALIAS, 100_000.0)
    say(f"[setup] {TICKER} quote seeded at ${price:.4f}")

    # Same environmental-fact override every other Phase 2 scenario uses --
    # orthogonal to the mechanism under test, needed so the harness is
    # runnable deterministically on a weekend/holiday.
    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking schwab_safety._is_trading_day "
            "True for this run (orthogonal to the mechanism under test)")
        schwab_safety._is_trading_day = lambda date_str: True

    posted = []

    def _capture(text=None, *a, **kw):
        posted.append(text if text is not None else (a[0] if a else kw.get('text')))
        return ('C0FAKEVENUE', '9999.1')

    # Both bindings needed (scenarios_replace_target_mismatch.py's precedent):
    # the REJECTED alert is posted from inside schwab_client._post_order_
    # confirmation, the wrapper "placement failed unexpectedly" alert from
    # signals_notify.check_addon_trigger_real -- two distinct real call sites.
    notify._post_message = _capture
    schwab_client._post_message = _capture

    node = _add_node()
    say(f"[setup] node wl_id={node['id']} ({CASH_ALIAS}), addon_enabled=1")
    pos = _open_core_position(node, price, PARENT_SHARES)
    checks.append(Check("core position open and armed (trail_state.trailing=True) before the trigger",
                        pos is not None and (pos.get('trail_state') or {}).get('trailing') is True,
                        f"pos={pos}"))

    # ---------------------------------------------------------------- leg 1
    broker.force_reject_next_order('REJECTED')
    say(f"[leg 1] armed force_reject_next_order('REJECTED'); calling "
        f"check_addon_trigger_real(pos, current_price=${price:.4f}) directly")
    notify.check_addon_trigger_real(pos, current_price=price)

    exemption_events = db.get_coverage_events(scenario_key="addon_double_buy_exemption")
    checks.append(Check("all five is_addon_leg preconditions verified true before the broker call "
                        "(this scenario targets the REJECTION path specifically, not the gate)",
                        any(e['result'] == 'preconditions_passed' and e['ticker'] == TICKER
                            for e in exemption_events),
                        f"events={[(e['result'], e['ticker']) for e in exemption_events]}"))

    placement_events = db.get_coverage_events(scenario_key="addon_entry_placement")
    rejected = [e for e in placement_events if e['result'] == 'failed_unexpectedly' and e['ticker'] == TICKER]
    checks.append(Check("addon_entry_placement logged 'failed_unexpectedly' for the rejected order "
                        "(the generic except-Exception branch, NOT the SafetyViolation 'blocked' branch "
                        "every existing addon test exercises)",
                        len(rejected) == 1,
                        f"events={[(e['result'], e['detail']) for e in placement_events]}"))
    if rejected:
        checks.append(Check("logged detail names the real OrderRejected/REJECTED failure, not a generic "
                            "or blank message",
                            'REJECTED' in rejected[0]['detail'],
                            f"detail={rejected[0]['detail']}"))

    checks.append(Check("no 'placed' addon_entry_placement event fired for this ticker "
                        "(the rejected order must never be reported as a successful placement)",
                        not any(e['result'] == 'placed' and e['ticker'] == TICKER for e in placement_events),
                        f"events={[(e['result'], e['ticker']) for e in placement_events]}"))

    fill_events = db.get_coverage_events(scenario_key="addon_entry_fill")
    checks.append(Check("no addon_entry_fill event of any kind fired for this ticker "
                        "(a rejected order never reaches the fill-confirmation code at all)",
                        not any(e['ticker'] == TICKER for e in fill_events),
                        f"events={[(e['result'], e['ticker']) for e in fill_events]}"))

    checks.append(Check("a real Slack alert posted reporting the failed add-on placement",
                        any('add-on leg placement failed unexpectedly' in (m or '') for m in posted),
                        f"posted={posted}"))
    checks.append(Check("a real Slack alert posted reporting the broker's own REJECTED status "
                        "(schwab_client._post_order_confirmation's distinct alert)",
                        any('REJECTED' in (m or '') and TICKER in (m or '') for m in posted),
                        f"posted={posted}"))

    # ---------------------------------------------------------------- proof
    leg = db.get_open_addon_leg_by_parent(pos['id'])
    checks.append(Check("NO addon_legs row was ever created for the rejected order -- open_addon_leg() "
                        "is only reached after place_equity_buy returns successfully, so a rejection "
                        "raised inside it means there is nothing to leave stuck 'open', not a row that "
                        "needed closing out",
                        leg is None,
                        f"leg={leg}"))

    pos_after = db.get_open_position(node['ticker'])
    checks.append(Check("parent core position is completely untouched by the rejected add-on attempt "
                        "(still open, same share count)",
                        pos_after is not None and pos_after['shares'] == PARENT_SHARES,
                        f"pos_after={pos_after}"))

    rejected_order = [o for o in broker.orders.values()
                      if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                      and o['orderLegCollection'][0]['instruction'] == 'BUY']
    checks.append(Check("exactly one real broker order was attempted for the add-on BUY, and it carries "
                        "the REJECTED status (not silently left WORKING/FILLED)",
                        len(rejected_order) == 1 and rejected_order[0]['status'] == 'REJECTED',
                        f"orders={[(o['status'], o['orderLegCollection'][0]['instruction']) for o in rejected_order]}"))

    observations['node_wl_id'] = node['id']
    observations['price'] = price
    observations['parent_position_id'] = pos['id']
    observations['posted_messages'] = posted
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT wl.ticker, wl.state, wl.account, wl.addon_enabled,
       (SELECT COUNT(*) FROM addon_legs WHERE ticker=wl.ticker) AS addon_leg_rows,
       (SELECT COUNT(*) FROM open_positions WHERE ticker=wl.ticker) AS open_position_rows,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='addon_entry_placement'
         AND result='failed_unexpectedly' AND ticker=wl.ticker) AS reject_events,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='addon_double_buy_exemption'
         AND result='preconditions_passed' AND ticker=wl.ticker) AS preconditions_passed_events
  FROM watch_list wl
 WHERE wl.ticker = ?
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires the parent position still open, ZERO
    addon_legs rows (never created, not created-then-stuck), exactly one
    'failed_unexpectedly' rejection event, and exactly one 'preconditions_
    passed' exemption event -- directly from the harness DB, not trusted from
    in-process return values."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['addon_leg_rows'] == 0 and rows[0]['open_position_rows'] == 1
          and rows[0]['reject_events'] == 1 and rows[0]['preconditions_passed_events'] == 1
          and rows[0]['state'] == 'live' and rows[0]['addon_enabled'] == 1)
    return ok, rows
