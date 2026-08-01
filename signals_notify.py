"""
Slack-facing core: notify_* functions, reminder loops, and the
reference-table/morning-report builder.

Chart generation lives in signals_charts.py, Slack block/message builders
in signals_blocks.py, Bolt interactive handlers in signals_handlers.py.
"""
import json
import time
from datetime import datetime

import strategies
import signals_config as cfg
import signals_db as db
import signals_compute as compute
import schwab_safety
import schwab_client
import schwab_stream
from signals_charts import _chart_buy, _chart_sell, _upload_chart
from signals_blocks import _post_message, _post_chunked, _build_buy_blocks, _build_sell_blocks
from signals_helpers import (
    _proximity_emoji, _existing_position_note, _last_sale_recovery, _phase_emoji,
    buy_order_sizing, log_poll, mode_tag, resolve_at_bar_close,
)
# scripts/ has no __init__.py but is still importable as a Python 3 implicit
# namespace package as long as repo root is on sys.path (true whenever this
# module is reached via active_signals.py, run from repo root) -- same
# import tests/test_coverage_check.py already uses.
from scripts.coverage_check import run_check as _coverage_run_check, _is_trading_day as _coverage_is_trading_day


def _attempt_automated_buy(node, sizing):
    """Places a real (or dry_run) trailing-buy order via schwab_client for a
    pilot-scope ticker instead of waiting on a human to click 'Trailing Buy
    Order Placed'. Returns (False, None) (falls back to the existing manual
    flow) if the ticker isn't in scope, or if schwab_safety blocks the order
    for any reason (paused, outside signal window, kill switch, etc.) --
    schwab_client already Slack-posts the BLOCKED/DRY RUN message either way,
    this function just decides which button set the caller should render.
    Returns (True, order_id) on success -- order_id is None in dry_run."""
    ticker = node['ticker']
    if ticker not in schwab_safety.AUTOMATION_ENABLED_TICKERS:
        return False, None
    if sizing['shares'] < 1:
        # A too-small notional/price combo sizes to 0 shares -- without this,
        # the real broker call would reject it, but only after dinging the
        # node circuit breaker's order_failures streak and posting a
        # "blocked"-shaped alert that misleadingly implies a safety guard
        # fired rather than "there's nothing to actually buy" (found in the
        # 2026-07-31 audit's still-open list).
        db.log_coverage_event("automated_buy_execution", _coverage_mode(node.get('account')), ticker=ticker,
                               node_id=node.get('id'), result="shares_too_small",
                               detail=f"shares={sizing['shares']} price={sizing.get('price')}")
        return False, None
    account = node.get('account')
    try:
        _, order_id = schwab_client.place_trailing_buy(
            account, ticker, sizing['shares'], sizing['price'], sizing['trail_buy_pct'])
    except schwab_safety.SafetyViolation:
        return False, None
    except Exception as e:
        _post_message(f"⚠️ {ticker} automated order placement failed unexpectedly: {e} — falling back to manual")
        return False, None
    return True, order_id


def _attempt_automated_sell(pos, current_price):
    """Sell-side mirror of _attempt_automated_buy -- places the trailing-sell
    order via schwab_client for a pilot-scope ticker instead of waiting on the
    'Order Placed' button. Returns (False, None) on any block/failure (falls
    back to the manual flow), or (True, order_id) on success -- the caller
    persists order_id so a later fill can be confirmed by exact-order lookup
    (schwab_client.get_filled_order's order_id mode) instead of ever guessing
    from a fuzzy ticker+side match. If a STOP order is already resting from
    entry (pos['sl_order_id'], Part 4 Section 6), cancels it first -- otherwise
    both orders would be live simultaneously for the same shares (oversell
    attempt or rejected order).

    Gated on both ticker scope AND the position's own node mode=='live'
    (automation_principles.md #7) -- the BUY side (_scan_buy_signals) already
    requires mode=='live'; this mirrors it on exit instead of relying on
    ticker membership alone, which would let a research-mode ticker's real
    position (e.g. one sharing a ticker with an unrelated live-mode node) get
    routed through an automated sell. Falls back to manual (False) if no
    matching node is found at all, same fail-closed direction as a mode
    mismatch. Looks the node up by wl_id (the position's own FK to
    watch_list), not (ticker, window) -- two concurrent nodes could otherwise
    share a window and resolve to the wrong one's mode (see docs/
    backlog_cache.md's wl_id refactor entry)."""
    ticker = pos['ticker']
    if ticker not in schwab_safety.AUTOMATION_ENABLED_TICKERS:
        return False, None
    node = db.get_watch_list_node_by_id(pos.get('wl_id'))
    if node is None or node.get('mode') != 'live':
        db.log_coverage_event("automated_sell_mode_skip", _coverage_mode(pos.get('account')), ticker=ticker,
                               position_id=pos.get('id'), node_id=pos.get('wl_id'), result="skipped",
                               detail=f"node_mode={node.get('mode') if node else None!r}")
        return False, None
    if not schwab_safety.node_automation_enabled(pos.get('wl_id')):
        return False, None
    account = pos.get('account')
    _mode = _coverage_mode(account)
    shares = pos.get('shares')
    trail_sell_pct = pos.get('trail_sell_pct')
    if not shares or not trail_sell_pct:
        return False, None
    sl_order_id = pos.get('sl_order_id')
    try:
        if sl_order_id:
            # Atomic replace (cancel-old + create-new as a single broker call)
            # instead of a separate cancel_order + place_trailing_sell -- closes
            # the window where a confirmed cancel could be followed by a failed/
            # blocked new placement, leaving nothing resting at the broker in
            # between (found 2026-07-27, raised directly by the user).
            _, exit_order_id = schwab_client.replace_order_with_trailing_sell(
                account, ticker, sl_order_id, shares, current_price, trail_sell_pct)
        else:
            _, exit_order_id = schwab_client.place_trailing_sell(account, ticker, shares, current_price, trail_sell_pct)
    except Exception as e:
        # A failed replace/placement here is the same "genuinely unprotected"
        # case as before -- there's no safe automatic recovery (re-placing the
        # same SL could itself hit the same block/failure), so surface the SL
        # price the user would need to manually re-enter at the broker rather
        # than leaving them to recompute it (found via Opus review, 2026-07-22).
        db.log_coverage_event("automated_sell_execution", _mode, ticker=ticker, position_id=pos.get('id'),
                               node_id=pos.get('wl_id'),
                               result="blocked" if isinstance(e, schwab_safety.SafetyViolation) else "failed_unexpectedly",
                               detail=str(e))
        if sl_order_id:
            sl_pct = pos.get('fixed_sl') if strategies.uses_fixed_sl(pos['strategy']) else pos.get('stop_loss')
            sl_price = pos['signal_price'] * (1 - sl_pct / 100) if sl_pct else None
            price_note = f"place stop-loss SELL {shares} @ ~${sl_price:.2f}" if sl_price else "place a stop-loss SELL manually"
            db.log_coverage_event("manual_sl_fallback_alert", _mode, ticker=ticker, position_id=pos.get('id'),
                                   node_id=pos.get('wl_id'), result="alerted", detail=price_note)
            _post_message(
                f"🚨 *{ticker}* ({account} · {mode_tag(account)}) UNPROTECTED — {price_note}\n"
                f"(auto trailing-sell replace of stop-loss {sl_order_id} failed: {e})"
            )
        elif not isinstance(e, schwab_safety.SafetyViolation):
            _post_message(f"⚠️ {ticker} automated trailing-sell placement failed unexpectedly: {e} — falling back to manual")
        return False, None
    db.log_coverage_event("automated_sell_execution", _mode, ticker=ticker, position_id=pos.get('id'),
                           node_id=pos.get('wl_id'), result="placed",
                           detail=f"shares={shares} trail_sell_pct={trail_sell_pct}")
    if sl_order_id and exit_order_id is not None:
        # sl_order_id (open_positions column, distinct from trail_state.exit_order_id)
        # is now dead -- replaced by this trailing-sell. Point it at the new order so
        # any later reader (a fresh replace attempt via this same function on re-arm,
        # the manual "cancel the existing stop-loss order" reminder text, the TRAIL+
        # hold_time_forced fallback in _attempt_automated_exit_sell) sees what's
        # actually resting instead of a dead order id -- same stuck-exit/false-alert
        # bug class as the exit_order_id staleness fixed 2026-07-31, just via this
        # replace path instead (found in the 2026-07-31 audit's "still open" list).
        # `exit_order_id is not None` guard added after a later review: extract_order_id
        # can legitimately return None on a real success (e.g. a missing Location
        # header) -- without the guard, a real successful replace with an unextractable
        # id would erase the previous (valid, still-real) sl_order_id, silencing the
        # missing_sl reconciliation check (gated on sl_order_id truthy) and dropping
        # resting_order_id to None for a later hold-time-forced exit, which then hits
        # the fresh place_equity_sell path and gets rejected by the resting-order dup
        # guard with no alert at all (the except block's both branches false). Matches
        # the established pattern already used 600 lines away in
        # _place_stop_loss_for_position (`if sl_order_id is not None:`).
        db.set_sl_order_id_by_position(pos['id'], exit_order_id)
    return True, exit_order_id


def _attempt_automated_exit_sell(pos, reason, current_price):
    """Places a real MARKET sell for a TP/SL/TIME exit signal, mirroring what
    the backtest kernel assumes happens: an exact bar-close exit fill, not a
    further wait for a trailing pullback. Distinct from _attempt_automated_sell
    (TRAIL reason only, places a TRAILING order at the earlier arm event) --
    before this, TP/SL/TIME exits had NO automated path at all and always
    waited on a manual Exited/Skipped tap, unlike the strategy's own
    assumption of an automatic exit (found 2026-07-27, real: SH's TIME exit
    sat unmanaged for hours in live trading).

    For reason=='TRAIL' (a genuine trail-stop breach), a trailing-sell order
    was already placed at the earlier arm event (notify_trailing_activated)
    -- this returns that existing order_id instead of placing a second,
    redundant order for the same shares. hold_time_forced (from
    state['exit_forced_by_hold_time']) identifies the OTHER case -- hold-time
    expired while armed, not a genuine breach; signals_compute.py reports
    this as reason=='TIME', not 'TRAIL', since 2026-08-01 (previously both
    collapsed to 'TRAIL', which every human-facing consumer -- Slack
    messages, trade_log -- had no way to distinguish from a real breach).
    The resting trailing-sell order is nowhere near its actual trigger in
    this case, so it must be force-replaced with a market sell just like a
    genuine TP/SL/TIME exit, not passively polled (found live 2026-07-29, SH:
    stuck for hours waiting on a 50%-wide trail order that was never going
    to fire on its own before the natural hold-time deadline). The checks
    below key off hold_time_forced directly (not reason -- they always did,
    reason=='TRAIL' was redundant alongside it even before this change).

    Also reuses an already-placed, still-unresolved TP/SL/TIME exit order
    from an EARLIER bar, if one exists (state['exit_pending']['order_id']) --
    without this, sell_alerted's dedup only covers the bar the order was
    placed on (active_signals.py's (position_id, bar_ts) key changes every
    new bar), so a still-true TIME/SL condition on the next bar would
    otherwise place a SECOND real market sell for the same shares before the
    first is confirmed filled (found by Sonnet review, 2026-07-27). EXCEPT
    when the pending order is still the original arm-time resting trailing-sell
    and hold_time_forced has since become true (found live 2026-07-30, SH:
    the reuse-guard above returned that stale order unconditionally, so the
    hold_time_forced branch below was never reached even after it was
    correctly set to True -- the 2026-07-29 fix only ever worked the first
    time a hold-time-forced exit fired on a position with no exit_pending yet)
    -- reusing it there would just repeat the exact bug this branch exists to
    fix. Every other reason (TP/SL/TIME) and the genuine-breach TRAIL case
    (hold_time_forced still False) are unaffected -- this only changes
    behavior for the narrow TRAIL+hold_time_forced+not-yet-replaced case.

    Returns the real order_id on success, or None (falls back to manual) on
    any block/scope-miss/failure -- same guards as _attempt_automated_sell
    (ticker automation scope, node.mode=='live', node_automation_enabled)."""
    ticker = pos['ticker']
    state = pos.get('trail_state') or {}
    exit_pending = state.get('exit_pending') or {}
    pending_order_id = exit_pending.get('order_id')
    hold_time_forced = bool(state.get('exit_forced_by_hold_time'))
    # Discriminator is a dedicated flag (hold_time_replaced), NOT
    # pending_order_id == exit_order_id -- that equality check broke once
    # the code below started refreshing exit_order_id to match the new
    # market-sell id after a successful replace (2026-07-31 fix for a stale
    # exit_order_id after force-replace). Reusing exit_order_id as both "the
    # order to replace" and "proof a replace already happened" made the two
    # concepts collide: after the very first replace they're always equal
    # again, so this guard would stay permanently True and re-issue a fresh
    # replace_equity_order_with_market against the same live order every bar
    # until fill (found via integration re-check, 2026-07-31 -- introduced by
    # the exit_order_id fix itself, caught before landing). A dedicated flag
    # can't collide with the field it's supposed to gate.
    # 2026-08-01: signals_compute.py now reports 'TIME' (not 'TRAIL') for the
    # hold-time-forced case, so in real calling code reason=='TRAIL' and
    # hold_time_forced are mutually exclusive going forward. Kept explicit
    # here anyway (not simplified to just `reason == 'TRAIL'`) as a defensive
    # belt-and-suspenders check -- hold_time_forced is the authoritative
    # signal either way, and this guards against any caller (including a
    # test simulating the pre-fix collapse, or a future caller) passing an
    # inconsistent combination.
    still_unreplaced_trail_order = hold_time_forced and not state.get('hold_time_replaced')
    if pending_order_id is not None and not still_unreplaced_trail_order:
        return pending_order_id
    if reason == 'TRAIL' and not hold_time_forced:
        return state.get('exit_order_id')
    if ticker not in schwab_safety.AUTOMATION_ENABLED_TICKERS:
        return None
    node = db.get_watch_list_node_by_id(pos.get('wl_id'))
    if node is None or node.get('mode') != 'live':
        db.log_coverage_event("automated_sell_mode_skip", _coverage_mode(pos.get('account')), ticker=ticker,
                               position_id=pos.get('id'), node_id=pos.get('wl_id'), result="skipped",
                               detail=f"node_mode={node.get('mode') if node else None!r} reason={reason}")
        return None
    if not schwab_safety.node_automation_enabled(pos.get('wl_id')):
        return None
    account = pos.get('account')
    _mode = _coverage_mode(account)
    shares = pos.get('shares')
    if not shares:
        return None
    # For a hold-time-forced TRAIL exit, the real resting order is normally
    # the trailing-sell placed at arm time (exit_order_id) -- sl_order_id is
    # stale/dead in that case, replaced by the trailing-sell when the
    # position armed. But arming (state['trailing']=True) is persisted
    # independently of whether that trailing-sell placement actually
    # succeeded (signals_compute.check_sell_condition writes 'trailing'
    # before notify_trailing_activated ever runs) -- if _attempt_automated_sell
    # failed (broker exception, SafetyViolation, automation paused/disabled),
    # exit_order_id was never set and the ORIGINAL SL is still the thing
    # actually resting at the broker. Without this fallback, resting_order_id
    # would resolve to None here, sending a fresh place_equity_sell with no
    # replacing_order_id -- which schwab_safety's resting-SELL guard then
    # blocks forever against that still-live SL, permanently self-blocking
    # the hold-time-forced exit (found live via execution-path walkthrough,
    # 2026-07-31 -- same stuck-exit symptom as the 2026-07-29/30 SH incidents,
    # through yet another door). Every other reason (TP/SL/TIME) always
    # resolves to the protective SL, as before.
    resting_order_id = pos.get('sl_order_id')
    if hold_time_forced and state.get('exit_order_id'):
        resting_order_id = state.get('exit_order_id')
    resting_order_label = (
        "trailing-sell" if (hold_time_forced and resting_order_id == state.get('exit_order_id'))
        else "stop-loss"
    )
    try:
        if resting_order_id:
            # Atomic replace instead of cancel_order + place_equity_sell -- same
            # rationale as _attempt_automated_sell's TRAIL-side fix: closes the
            # window where a confirmed cancel could be followed by a failed/
            # blocked new placement, leaving nothing resting at the broker in
            # between (found 2026-07-27, raised directly by the user).
            _, order_id = schwab_client.replace_equity_order_with_market(
                account, ticker, resting_order_id, "SELL", shares, current_price)
        else:
            _, order_id = schwab_client.place_equity_sell(account, ticker, shares, current_price)
    except Exception as e:
        db.log_coverage_event("automated_exit_execution", _mode, ticker=ticker, position_id=pos.get('id'),
                               node_id=pos.get('wl_id'),
                               result="blocked" if isinstance(e, schwab_safety.SafetyViolation) else "failed_unexpectedly",
                               detail=f"reason={reason}: {e}")
        if resting_order_id:
            sl_pct = pos.get('fixed_sl') if strategies.uses_fixed_sl(pos['strategy']) else pos.get('stop_loss')
            sl_price = pos['signal_price'] * (1 - sl_pct / 100) if sl_pct else None
            price_note = f"place stop-loss SELL {shares} @ ~${sl_price:.2f}" if sl_price else "place a stop-loss SELL manually"
            db.log_coverage_event("manual_sl_fallback_alert", _mode, ticker=ticker, position_id=pos.get('id'),
                                   node_id=pos.get('wl_id'), result="alerted", detail=price_note)
            _post_message(
                f"🚨 *{ticker}* ({account} · {mode_tag(account)}) UNPROTECTED — {price_note}\n"
                f"(auto {reason} exit replace of {resting_order_label} {resting_order_id} failed: {e})"
            )
        elif not isinstance(e, schwab_safety.SafetyViolation):
            _post_message(f"⚠️ {ticker} automated {reason} exit placement failed unexpectedly: {e} — falling back to manual")
        return None
    db.log_coverage_event("automated_exit_execution", _mode, ticker=ticker, position_id=pos.get('id'),
                           node_id=pos.get('wl_id'), result="placed", detail=f"reason={reason} shares={shares}")
    if resting_order_id and resting_order_id == pos.get('sl_order_id') and order_id is not None:
        # Same staleness fix as _attempt_automated_sell's success path: whatever
        # was actually replaced here (a genuine TP/SL/TIME exit's SL, or the
        # TRAIL+hold_time_forced fallback that resolved to the original SL
        # because the arm-time trailing-sell placement itself had failed) is
        # now dead -- point sl_order_id at the real order that's resting now
        # instead of leaving it pointing at a dead order id forever.
        # `order_id is not None` guard added after a later review -- see the
        # matching comment in _attempt_automated_sell's success path for why.
        db.set_sl_order_id_by_position(pos['id'], order_id)
    if hold_time_forced:
        # The just-replaced order (trailing-sell or, on the failed-placement
        # fallback above, the original SL) is now dead -- point exit_order_id
        # at the real order that's actually resting/placed now. Without this,
        # exit_order_id keeps pointing at a dead order forever; if a human
        # later taps "Skip" on the exit reminder (clearing exit_pending but
        # leaving trailing/exit_forced_by_hold_time set), the NEXT forced-exit
        # attempt would try to replace that same dead order again and fail
        # (found via execution-path walkthrough, 2026-07-31).
        new_state = dict(state)
        new_state['exit_order_id'] = order_id
        new_state['hold_time_replaced'] = True
        db.update_position_trail_state(pos['id'], new_state)
    return order_id


_RECONCILE_ALERTED: dict[str, float] = {}
_RECONCILE_COOLDOWN_SECS = 900  # 15 min -- matches the reminder-nag/section-alert cadence elsewhere


# _fresh_node (round 3) was removed 2026-07-25 -- a later Opus review pass
# (round 6) proved it had become a pure no-op: after round 5's fix pinned
# `account` to the signal-time snapshot (a real resting order's account is a
# physical fact fixed at placement, not "whatever watch_list says now"), the
# only field this function still touched was `id` -- refreshed by looking it
# up FROM node['id'] and writing the same value back, tautologically a no-op
# by construction. Every real correctness need (account pinning, trigger
# fields pinned) is already satisfied by using pending['node'] directly. The
# 4 real pending_buys rows that predated `_PENDING_BUY_NODE_KEYS` gaining
# 'id' were separately, permanently backfilled (round 4) -- there's no
# remaining case this helper protects.


def _coverage_mode(account):
    """'dry_run'/'live' for coverage_events logging, from the real per-account
    flag -- falls back to 'dry_run' (the safe/conservative label) if the
    account isn't recognized rather than raising, since logging must never
    interfere with the real control-flow it's observing."""
    limits = schwab_safety.ACCOUNTS.get(account)
    return "live" if (limits and not limits.dry_run) else "dry_run"


def _alert_reconcile_mismatch(pos, kind, text):
    """Posts a reconciliation mismatch alert, rate-limited per (position,
    mismatch-kind) so an already-alerted, still-unresolved mismatch doesn't
    repost every poll cycle (same pattern as active_signals._guarded's
    per-section cooldown). Checked against coverage_snoozes first -- a known,
    human-acknowledged condition (e.g. UDOW's deliberately-seeded stale test
    position) skips both the coverage_events log and the Slack alert entirely
    while snoozed, rather than just the alert, so the accountability grid's
    daily/all-time counts aren't inflated by a condition someone already
    explained. Time-bounded by design: the snooze expires and resumes
    alerting rather than silencing the scenario forever.

    Returns True if this was a real (non-snoozed) mismatch -- used by
    check_live_state_reconciliation to feed the node-level circuit breaker's
    reconciliation_mismatches streak, which should reflect true state, not
    alert-cooldown noise, but should still respect a human-acknowledged
    snooze the same way the alert/log do."""
    if db.is_snoozed("reconciliation_mismatch", ticker=pos.get('ticker'),
                      account=pos.get('account'), node_id=pos.get('wl_id'), kind=kind):
        return False
    db.log_coverage_event(
        "reconciliation_mismatch", _coverage_mode(pos.get('account')),
        ticker=pos.get('ticker'), position_id=pos.get('id'),
        node_id=pos.get('wl_id'), result=kind, detail=text
    )
    key = f"{pos['id']}:{kind}"
    last = _RECONCILE_ALERTED.get(key, 0)
    if time.time() - last < _RECONCILE_COOLDOWN_SECS:
        return True
    _RECONCILE_ALERTED[key] = time.time()
    _post_message(text)
    return True


_STALE_PRICE_ALERTED: dict[str, float] = {}
_STALE_PRICE_COOLDOWN_SECS = 900  # 15 min -- same cadence as the other suppression/mismatch alerts


def alert_stale_price_exit_suppressed(pos):
    """A real (non-paper) position's mid-bar exit check was silently skipped
    because `signals_compute._current_price` returned None -- either the
    open-market-before-first-refresh race the stale-price guard was built for
    (2026-07-22, HIBL paper trade), or a genuine same-day data-refresh
    failure. Either way, no SL/trailing-stop/TIME check ran against this
    position at all for this poll (backlog item, HIBL incident writeup: a
    real position's stale-guard suppression previously produced only a
    `log_poll` trace line, no Slack alert). Rate-limited per position so a
    persistent same-day data outage doesn't repost every poll cycle."""
    ticker = pos['ticker']
    account = pos.get('account')
    key = str(pos['id'])
    last = _STALE_PRICE_ALERTED.get(key, 0)
    if time.time() - last < _STALE_PRICE_COOLDOWN_SECS:
        return
    _STALE_PRICE_ALERTED[key] = time.time()
    _post_message(
        f"⚠️ *{ticker}* ({account} · {mode_tag(account)}) exit check skipped this poll — no fresh price available\n"
        f"(`_current_price` returned None: stale/missing same-day data; position remains open, "
        f"unmonitored until the next successful refresh)"
    )


def check_live_state_reconciliation(open_positions):
    """Detection-only live-state reconciliation (automation_principles.md #5,
    #1 -- backlog 2026-07-21). For each open position on an automation-scope
    ticker, compares the broker's real state against what open_positions/
    trail_state records, and posts a proposed-fix Slack alert (text only) on
    any mismatch:
      - share-count mismatch (open_positions.shares vs. the broker's real
        position size)
      - missing protective order (expected resting SL pre-arm, or resting
        trailing-sell post-arm, isn't actually resting at the broker)

    Each position also feeds schwab_safety.record_node_streak's
    reconciliation_mismatches streak (2026-07-29, monitor-only node-level
    circuit breaker) -- one hit/clean call per position per poll, not per
    mismatch-kind, so 3 mismatches found in a single poll don't themselves
    count as a 3-poll streak.

    Deliberately detection/alert-only -- never executes a remediation itself,
    matches the explicit user call that an auto-correcting version would be a
    new automated-trading decision layer on top of ones this project has
    already found real bugs in, and a false-positive mismatch (legitimate
    slippage, a deliberate manual override, a timing lag) would trigger a
    real, wrong "fix". The broker is treated as ground truth (automation_
    principles.md #1) -- the suggested fix always corrects the DB/order side,
    never assumes the broker is wrong. Best-effort: a fetch failure just skips
    that position for this cycle (nothing here blocks or gates a real order,
    so there's no fail-closed obligation the way schwab_safety.check_order
    has)."""
    for pos in open_positions:
        if pos.get('is_dry_run_sim'):
            # No real order was ever placed for this position -- the broker has
            # no matching state to compare against at all (that's the whole
            # reason it's synthesized), so this would only ever produce a false
            # share-count/missing-protective-order mismatch.
            continue
        ticker = pos['ticker']
        if ticker not in schwab_safety.AUTOMATION_ENABLED_TICKERS:
            continue
        account = pos.get('account')
        if not account:
            continue
        # Re-fetch fresh before comparing -- `open_positions` (the caller's
        # arg) is a snapshot taken once at the top of this poll cycle; an
        # earlier step in the *same* cycle (_check_position_exit) can close
        # or update a position before this function runs, leaving a stale
        # in-memory row here. Found live 2026-07-28: GDXU's real TRAIL close
        # at 13:30:09 produced a false "shares"/"missing_trailing_sell"
        # mismatch 6 seconds later, comparing the broker's correct post-close
        # state (0 shares, no order) against this cycle's stale belief that
        # the position was still open -- the original fix (2026-07-28) only
        # re-checked openness via wl_id (skipping legacy wl_id-less rows
        # entirely) and then discarded the fresh row, still comparing against
        # the stale `pos` below. Widened 2026-07-31 (execution-path
        # walkthrough) to use db.get_position_by_id (works with or without
        # wl_id) and to actually use the fetched row for every comparison
        # that follows, closing both gaps at once.
        pos = db.get_position_by_id(pos['id'])
        if pos is None:
            continue
        _node_id = pos.get('wl_id')
        try:
            real_shares = schwab_client.get_real_position(account, ticker)
            orders = schwab_safety._open_orders(account)
        except Exception as e:
            db.log_coverage_event(
                "reconciliation_fetch_failed", _coverage_mode(account),
                ticker=ticker, position_id=pos.get('id'), node_id=_node_id, result="skipped", detail=str(e)
            )
            print(f"  [reconcile] {ticker}: fetch failed, skipping this cycle: {e}")
            continue

        mismatch_found = False
        expected_shares = pos.get('shares')
        if expected_shares is not None and real_shares != expected_shares:
            mismatch_found |= _alert_reconcile_mismatch(
                pos, "shares",
                f"⚠️ *{ticker}* ({account} · {mode_tag(account)}) live-state mismatch: `open_positions` tracks "
                f"{expected_shares:g} shares, broker shows {real_shares:g} — broker is ground "
                f"truth; suggested fix: verify no unexpected fill/manual trade explains the gap, "
                f"then correct `open_positions.shares` to {real_shares:g}"
            )

        state = pos.get('trail_state') or {}
        has_sell_order = any(
            leg.get('instruction') == 'SELL' and leg.get('instrument', {}).get('symbol') == ticker
            for o in orders for leg in o.get('orderLegCollection', [])
        )
        if expected_shares is None:
            # Nothing was actually checked for this position this poll (the
            # share compare above is skipped, and so are the protective-order
            # checks below) -- must not record a streak outcome either way.
            # Found by Opus review 2026-07-30: mismatch_found is unconditionally
            # False in this branch (the only check that could have set it is
            # gated on expected_shares is not None), so this used to always
            # record a fabricated hit=False "clean poll," silently resetting
            # (and un-tripping) a genuine in-progress mismatch streak on any
            # poll where shares happened to be unknown.
            continue
        if state.get('trailing') and not state.get('order_placed') and not state.get('last_reminder_at'):
            # Armed (trailing=True, persisted by check_sell_condition) but
            # order_placed never got set AND last_reminder_at is absent --
            # the real signature of "notify_trailing_activated never ran at
            # all" (daemon crashed/restarted in the window between the two,
            # an unrecoverable gap: just_activated_trailing can never re-fire
            # once trailing=True is already persisted). NOT gated on
            # order_placed alone: that also matches the normal, expected
            # awaiting-manual-confirmation state (a non-live-mode/paused
            # node, where _attempt_automated_sell legitimately declines and
            # notify_trailing_activated posts the manual alert instead of
            # auto-placing) -- but that path DOES set last_reminder_at
            # unconditionally (signals_notify.py's notify_trailing_activated,
            # `state['last_reminder_at'] = ...` runs regardless of
            # auto_placed), so gating on its absence correctly excludes it.
            # Confirmed reachable false-positive without this gate (Opus
            # review, 2026-07-31): every poll would re-flag the normal
            # pending-manual-confirmation window and feed a streak hit,
            # tripping the node circuit breaker within ~3 polls of any
            # legitimate arm awaiting a human tap. The arm-time order might
            # exist or might not either way (has_sell_order can't distinguish
            # "old SL still resting, untouched" from "genuinely unprotected",
            # since a crash before the atomic replace even started leaves the
            # old SL fully intact) -- so this still alerts regardless of
            # has_sell_order rather than trying to infer safety from broker
            # state alone (found via arming-logic walkthrough, 2026-07-31:
            # the prior version of this check required order_placed=True to
            # fire at all, so this exact stuck state fell through both
            # branches here and was invisible everywhere else too --
            # check_trailing_reminders also can't recover it, since it bails
            # on this same missing last_reminder_at).
            mismatch_found |= _alert_reconcile_mismatch(
                pos, "armed_order_never_confirmed",
                f"⚠️ *{ticker}* ({account} · {mode_tag(account)}) live-state mismatch: armed (trailing stop active) "
                f"but no trailing-sell order was ever confirmed placed — "
                f"{'a resting SELL order was found (likely the original stop-loss, still intact)' if has_sell_order else 'NO resting SELL order was found at all'}; "
                f"suggested fix: check the broker directly and either manually place a trailing-sell "
                f"for {expected_shares:g} shares or confirm the existing resting order is adequate"
            )
        elif state.get('trailing') and state.get('order_placed') and not has_sell_order:
            mismatch_found |= _alert_reconcile_mismatch(
                pos, "missing_trailing_sell",
                f"⚠️ *{ticker}* ({account} · {mode_tag(account)}) live-state mismatch: trailing-sell marked placed but "
                f"no resting SELL order found at the broker — position may be unprotected; "
                f"suggested fix: place a trailing-sell order for {expected_shares:g} shares now"
            )
        elif not state.get('trailing') and pos.get('sl_order_id') and not has_sell_order:
            mismatch_found |= _alert_reconcile_mismatch(
                pos, "missing_sl",
                f"⚠️ *{ticker}* ({account} · {mode_tag(account)}) live-state mismatch: SL order id {pos['sl_order_id']} "
                f"is recorded but no resting SELL order found at the broker — position may be "
                f"unprotected; suggested fix: place a stop-loss order for {expected_shares:g} shares now"
            )
        schwab_safety.record_node_streak(
            ticker, account, "reconciliation_mismatches", hit=mismatch_found, node_id=_node_id)


def _attempt_automated_market_buy(node, sizing):
    """Market-buy mirror of _attempt_automated_buy (Part 4, Section 4) --
    places a real (or dry_run) plain market order via schwab_client.place_equity_buy
    for a pilot-scope, non-trailing-buy node (e.g. TrailingExitZScoreBreakout)
    instead of waiting on the manual price-entry flow. Returns (False, None) if
    the ticker isn't in automation scope or schwab_safety blocks the order."""
    ticker = node['ticker']
    if ticker not in schwab_safety.AUTOMATION_ENABLED_TICKERS:
        return False, None
    if sizing['shares'] < 1:
        # Same guard as _attempt_automated_buy -- see that function's comment.
        db.log_coverage_event("automated_buy_execution", _coverage_mode(node.get('account')), ticker=ticker,
                               node_id=node.get('id'), result="shares_too_small",
                               detail=f"shares={sizing['shares']} price={sizing.get('price')}")
        return False, None
    account = node.get('account')
    try:
        _, order_id = schwab_client.place_equity_buy(account, ticker, sizing['shares'], sizing['price'])
    except schwab_safety.SafetyViolation:
        return False, None
    except Exception as e:
        _post_message(f"⚠️ {ticker} automated market-buy placement failed unexpectedly: {e} — falling back to manual")
        return False, None
    return True, order_id


_SL_FAST_CONFIRM_ATTEMPTS = 5
_SL_FAST_CONFIRM_INTERVAL_SECS = 2
_SL_PLACEMENT_RETRY_ATTEMPTS = 3
_SL_PLACEMENT_RETRY_DELAY_SECS = 2


def _place_stop_loss_for_position(node, ticker):
    """Places the real resting STOP order for a freshly-opened automated
    position -- market-buy (Part 4, Section 6) or trailing-buy (extended
    2026-07-24, called from both _reconcile_buy_fill's auto-fill path and
    signals_handlers.handle_trail_buy_fill_price's manual 'Filled' path).
    Reads the final share count back off open_positions (post any top-up
    _reconcile_fill already applied for market-buy fills) so the stop covers
    the whole position, not just a provisional quantity.
    Anchored to pos['entry_price'] (the real fill), matching exactly what
    strategies.py's own check_exit uses for the same SL comparison
    (`stop_price = ctx['entry_price'] * (1 - stop_loss)`, strategies.py) --
    ALWAYS true regardless of strategy, unlike this function's old basis
    (signal_price, the passed-in trigger price). That used to be defended as
    backtest parity ("the kernel computes stop_price = entry_price * (1 -
    sl%) where entry_price IS the trigger"), which only actually holds for
    the market-buy strategies (entry ~= signal bar close). For the
    trailing-buy strategies (backtester._simulate_trail_both/_simulate_trail_buy),
    the kernel's real entry_price is `running_low * (1 + trail_buy_pct)` (or
    the gap-through Open) -- a materially different, LATER value than
    signal_price whenever price kept falling before the bounce, which is the
    normal case for this strategy. Anchoring the real broker stop to the
    wrong, earlier signal_price could place it ABOVE the real entry price
    (and even above market), either getting it rejected outright (leaving
    the position with zero broker protection) or triggering immediately on
    placement (found via a fresh execution-path walkthrough, 2026-07-31 --
    contradicted CLAUDE.md's own claimed invariant, "Schwab stop order set at
    the algo's exact fixed_sl price, no padding"). Looks the position up by
    node['id'] (wl_id), not ticker -- ticker-only would size/anchor off a
    sibling node's position and stamp its broker order id onto the wrong row
    if 2+ nodes share this ticker (see docs/backlog_cache.md's wl_id refactor
    entry)."""
    pos = db.get_open_position_by_wl_id(node['id'])
    if not pos or not pos.get('shares'):
        return
    account = node.get('account')
    sl_pct = pos.get('fixed_sl') if strategies.uses_fixed_sl(pos['strategy']) else pos['stop_loss']
    if not sl_pct:
        return
    stop_price = pos['entry_price'] * (1 - sl_pct / 100)
    shares = int(pos['shares'])
    try:
        _, sl_order_id = schwab_client.place_stop_loss(account, ticker, shares, stop_price)
    except schwab_safety.SafetyViolation as e:
        # Not retried -- a policy block (kill switch, paused automation, an
        # existing SL/SELL order already resting) won't resolve differently
        # on a bare retry, matching _submit_order_with_retry's established
        # convention elsewhere in this module.
        db.log_coverage_event("sl_placement", _coverage_mode(account), ticker=ticker, position_id=pos.get('id'),
                               node_id=node.get('id'), result="blocked", detail=str(e))
        _post_message(
            f"🚨 *{ticker}* ({account} · {mode_tag(account)}) UNPROTECTED — place stop-loss SELL {shares} @ ~${stop_price:.2f}\n"
            f"(stop-loss placement blocked: {e})"
        )
        return
    except Exception as e:
        # A genuine broker rejection here (not a policy block) is commonly
        # "the stop price must be on the correct side of the current bid/ask"
        # -- i.e. real time has passed since entry_price was recorded (a fill
        # reconciled hours late after a daemon restart, a thin/volatile
        # ticker) and the market has already crossed the target. Retrying the
        # SAME resting-STOP order would just fail again for the same reason.
        # Self-correcting retry instead (found live 2026-07-31, LABD -- real
        # incident, this exact rejection): re-check the real current price
        # each attempt; if the market has already crossed the target stop
        # (the position has effectively already breached its stop in real
        # time), exit now via a real MARKET sell -- same principle as the
        # exit-side gap-through-trigger fill this codebase already uses
        # elsewhere -- instead of retrying a resting order doomed to reject
        # again. If the market hasn't crossed it, just retry the resting
        # STOP; the broker rejection may have been transient. Every attempt
        # still goes through schwab_safety.check_order, whose own duplicate-
        # order guard makes this safe against a race where something is
        # already resting by the time a retry runs -- that attempt just
        # SafetyViolation-blocks cleanly rather than double-placing.
        last_error = e
        for attempt in range(1, _SL_PLACEMENT_RETRY_ATTEMPTS):
            time.sleep(_SL_PLACEMENT_RETRY_DELAY_SECS)
            try:
                current_price = schwab_client.get_current_price(ticker)
            except Exception:
                current_price = None
            if current_price is not None and current_price <= stop_price:
                try:
                    _, market_order_id = schwab_client.place_equity_sell(account, ticker, shares, current_price)
                except schwab_safety.SafetyViolation:
                    db.log_coverage_event(
                        "sl_placement", _coverage_mode(account), ticker=ticker, position_id=pos.get('id'),
                        node_id=node.get('id'), result="blocked_on_retry",
                        detail="already protected by a resting order")
                    return
                except Exception as e2:
                    last_error = e2
                    continue
                db.log_coverage_event(
                    "sl_placement", _coverage_mode(account), ticker=ticker, position_id=pos.get('id'),
                    node_id=node.get('id'), result="placed_as_market_already_breached",
                    detail=f"target_stop={stop_price:.4f} current_price={current_price:.4f} attempt={attempt}")
                # Record exit_pending so check_own_sell_fills/check_exit_reminders
                # can actually find and close this position once the market
                # order fills -- without this, a real order was just placed
                # (this WILL flatten the position) but nothing tracks it: no
                # trade_log row, no P&L, the DB believes the position is still
                # open indefinitely, reconciliation starts false-alerting a
                # share-count mismatch, and the NEXT genuine exit signal would
                # place a second real SELL for shares that no longer exist --
                # a naked-short path, since nothing is resting anymore for the
                # duplicate-order guard to catch (found live 2026-07-31, LABD --
                # the market_order_id from this exact branch was previously
                # discarded unused; confirmed live: the real order placed
                # (1007409713143) went untracked until this fix). Also alerts,
                # matching this project's standing convention that a real
                # forced exit is never silent. Re-fetch fresh before building
                # this write -- pos was fetched once at the top of this
                # function, and several seconds (retry sleeps) may have
                # passed since; a stale-snapshot overwrite here is the exact
                # bug class fixed repeatedly elsewhere in this module tonight.
                fresh_pos = db.get_position_by_id(pos['id']) or pos
                new_state = dict(fresh_pos.get('trail_state') or {})
                new_state['exit_pending'] = {
                    'reason': 'SL', 'current_price': current_price, 'target_price': stop_price,
                    'reminder_channel': None, 'reminder_ts': None, 'reminder_count': 0,
                    'last_reminder_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'order_id': market_order_id,
                }
                db.update_position_trail_state(pos['id'], new_state)
                _post_message(
                    f"🤖 *{ticker}* ({account} · {mode_tag(account)}) SL already breached by the time it could be "
                    f"placed — market SELL {shares} submitted @ ~${current_price:.2f} instead "
                    f"(target stop was ${stop_price:.2f})"
                )
                return
            try:
                _, sl_order_id = schwab_client.place_stop_loss(account, ticker, shares, stop_price)
            except schwab_safety.SafetyViolation:
                db.log_coverage_event(
                    "sl_placement", _coverage_mode(account), ticker=ticker, position_id=pos.get('id'),
                    node_id=node.get('id'), result="blocked_on_retry",
                    detail="already protected by a resting order")
                return
            except Exception as e2:
                last_error = e2
                continue
            db.log_coverage_event(
                "sl_placement", _coverage_mode(account), ticker=ticker, position_id=pos.get('id'),
                node_id=node.get('id'), result="placed_on_retry",
                detail=f"stop_price={stop_price:.4f} attempt={attempt}")
            if sl_order_id is not None:
                db.set_sl_order_id_by_position(pos['id'], sl_order_id)
            return
        db.log_coverage_event("sl_placement", _coverage_mode(account), ticker=ticker, position_id=pos.get('id'),
                               node_id=node.get('id'), result="failed_unexpectedly", detail=str(last_error))
        _post_message(
            f"🚨 *{ticker}* ({account} · {mode_tag(account)}) UNPROTECTED — place stop-loss SELL {shares} @ ~${stop_price:.2f}\n"
            f"(stop-loss placement failed after {_SL_PLACEMENT_RETRY_ATTEMPTS} attempts: {last_error})"
        )
        return
    db.log_coverage_event("sl_placement", _coverage_mode(account), ticker=ticker, position_id=pos.get('id'),
                           node_id=node.get('id'), result="placed", detail=f"stop_price={stop_price:.4f}")
    if sl_order_id is not None:
        db.set_sl_order_id_by_position(pos['id'], sl_order_id)


def _sync_confirm_and_protect(ticker, node, order_id=None):
    """Synchronous fast-confirm step (Part 4, Section 6), run immediately after
    an automated market buy is placed -- ~70-80% of this strategy's trades exit
    via SL, the primary defense mechanism, so a freshly-opened position needs a
    resting stop within seconds, not whenever the async fill pipeline
    (schwab_stream websocket + check_auto_fills 5-min poll fallback) happens to
    notice. A market order fills in seconds, so this short budget covers the
    normal case. On a hit, reuses _reconcile_buy_fill directly (idempotent/
    dedup-safe via clearing pending_buys first, same as the poll/websocket
    paths) so the position opens, tops up, and gets protected in one place.
    On timeout (rare -- API/exchange hiccup), fires an urgent Slack alert
    instead of silently deferring -- the position will still get protected
    once the async pipeline eventually confirms the fill and re-triggers this,
    but a human should know about the gap in the meantime.

    order_id (the real id returned by _attempt_automated_market_buy) is passed
    through to get_filled_order's exact-order lookup -- without it, a slow
    fill here could fall through to the fuzzy ticker+side fallback and match a
    stale unrelated prior fill for the same ticker+account (2026-07-27 GDXU
    incident's exact root cause)."""
    account = node.get('account')
    ticker_label = ticker
    for _ in range(_SL_FAST_CONFIRM_ATTEMPTS):
        fill = schwab_client.get_filled_order(account, ticker, 'BUY', order_id=order_id)
        if fill is not None:
            break
        time.sleep(_SL_FAST_CONFIRM_INTERVAL_SECS)
    else:
        db.log_coverage_event("sl_placement_fast_confirm_timeout", _coverage_mode(account), ticker=ticker,
                               node_id=node.get('id'), result="timed_out")
        _post_message(f"\U0001F6A8 {ticker_label} — market buy placed but fill not confirmed after "
                      f"{_SL_FAST_CONFIRM_ATTEMPTS * _SL_FAST_CONFIRM_INTERVAL_SECS}s — position may be "
                      f"temporarily UNPROTECTED (no stop-loss resting). Will be placed once the fill is "
                      f"confirmed by the auto-fill poll or account-activity stream.")
        return
    _reconcile_buy_fill(ticker, fill['price'], fill['quantity'], wl_id=node['id'])


_MAX_RUNNING_LOW_DROP_PCT = 20.0  # see update_real_pending_buys_running_low's docstring


def update_real_pending_buys_running_low():
    """Tracks pending_buys.running_low for a REAL (non-dry_run) resting
    trailing-buy -- mirrors update_dry_run_buys' tracking logic below, but
    deliberately WITHOUT any fill synthesis: a real fill is detected via
    check_auto_fills/drain_fill_queue, never here.

    Exists because check_gap_resize reads running_low assuming it reflects
    real-time price tracking, but before this function, running_low was only
    ever updated for dry_run accounts (update_dry_run_buys' own tracking,
    below) -- a real trailing-buy resting for hours had running_low frozen at
    its original signal_price the entire time, producing a stale buy_trigger
    for check_gap_resize's overnight-gap correction to check against. Found
    live 2026-07-29 (RETL): confirmed via docs/deep_backlog.md that the one
    real historical gap_resize firing (GDXU, 2026-07-27) was a deliberately
    rigged test (running_low manually set to $1.00) -- never an honest test
    of this tracking actually working for a real order. Called every poll,
    same unconditional cadence as update_dry_run_buys.

    Uses schwab_client.get_current_price (real-time Schwab quote), NOT
    signals_compute._current_price (cached hourly bar close, updated once/hour)
    -- matching check_gap_resize's own price source is the whole point, since
    the point of tracking between polls is capturing intra-hour movement an
    hourly cache wouldn't show anyway. update_dry_run_buys uses the cached
    price deliberately, for consistency with the rest of its simulation --
    that reasoning doesn't apply here, this is real-order tracking meant to
    feed a real-time check.

    Sanity-bounds each update against implausible single-print moves --
    get_current_price prefers `quote.extended.lastPrice` (deliberately, so
    this function can track a genuine overnight/pre-market price fall ahead
    of check_gap_resize's pre-open check, see e.g.
    tests/test_fake_broker_gap_resize_scenario.py's Phase 1), but extended
    hours are thin/wide-spread enough that a single anomalous print (bad
    tick, one illiquid odd-lot trade) can otherwise permanently ratchet
    running_low down to a price nothing real ever confirmed -- running_low is
    monotonically non-increasing (`min(...)`), so a bad print never
    self-corrects even if the very next quote reverts. A false-low running_low
    produces an artificially low buy_trigger for check_gap_resize to compare
    the real opening print against, firing a real, unwarranted MARKET buy.
    Found in the 2026-07-31 audit's still-open list. Bound: a single poll may
    not lower running_low by more than _MAX_RUNNING_LOW_DROP_PCT in one step
    (generous -- comfortably above any real single-poll move for a real
    ticker at the default 5-min POLL_SECS cadence, automation_principles.md
    #7a's cheap-static-buffer preference over precisely modeling which prints
    are real). A genuine large overnight fall still reaches its true low
    within a couple of polls (each capped step compounds), so real gap
    coverage is preserved -- only a single wild/erroneous print's full
    magnitude is ever rejected outright, and even that print still pulls
    running_low down by the capped amount rather than being ignored entirely."""
    for pb in db.get_pending_buys():
        wl_id = pb.get('wl_id')
        if not wl_id:
            continue
        node = db.get_watch_list_node_by_id(wl_id)
        if node is None or node.get('mode') != 'live':
            continue
        account = node.get('account')
        limits = schwab_safety.ACCOUNTS.get(account)
        if not limits or limits.dry_run:
            continue  # dry_run accounts are already covered by update_dry_run_buys
        if not db._is_trailing_buy(node):
            continue  # a market-buy-eligible node has no bounce/running-low concept
        ticker = pb['ticker']
        try:
            price = schwab_client.get_current_price(ticker)
        except Exception:
            continue
        if price is None:
            continue
        current = pb['running_low'] or pb['signal_price']
        floor = current * (1 - _MAX_RUNNING_LOW_DROP_PCT / 100)
        running_low = max(min(current, price), floor)
        if running_low != pb['running_low']:
            db.update_pending_buy_running_low(wl_id, running_low)


_ENTRY_ABANDON_ALERTED: dict[str, float] = {}
_ENTRY_ABANDON_ALERT_COOLDOWN_SECS = 900  # 15 min -- matches _RECONCILE_COOLDOWN_SECS elsewhere


def _throttled_entry_abandon_alert(wl_id, kind, text):
    """Rate-limits the three check_entry_abandon branches that leave the
    pending_buys row in place (unrecognized_account, no_order_id_on_file,
    cancel_failed) -- bars_held only ever grows once max_hold_hours is
    crossed, so without this each re-enters and reposts on every single poll
    forever, burying real trade alerts (found by review: this module already
    has the right pattern for exactly this, _RECONCILE_ALERTED/
    _RECONCILE_COOLDOWN_SECS above, just not applied here). Keyed per
    (wl_id, kind) so a genuinely different condition on the same row still
    alerts immediately rather than being silenced by an unrelated cooldown."""
    key = f"{wl_id}:{kind}"
    last = _ENTRY_ABANDON_ALERTED.get(key, 0)
    if time.time() - last < _ENTRY_ABANDON_ALERT_COOLDOWN_SECS:
        return
    _ENTRY_ABANDON_ALERTED[key] = time.time()
    _post_message(text)


def check_entry_abandon():
    """Live equivalent of the backtest kernel's entry-abandon timeout
    (backtester.py's `wait_bars >= max_hours_to_hold` in
    `_simulate_trail_buy`/`_simulate_trail_both`, all four resolutions):
    without this, a trailing buy that never bounces has no timeout live at
    all -- the real broker order rests GOOD_TILL_CANCEL forever
    (schwab_client._build_trailing_order), and schwab_safety._has_open_order/
    _has_open_buy_order_in_account then permanently block every other real
    BUY attempt on that ticker (or account, for the account-wide guard) since
    they see it as still-open. Found in the 2026-07-31 exit/arm/entry audit,
    [HIGHEST] of the items left open that session.

    Mirrors the kernel's threshold exactly: reuses `max_hold_hours` (the same
    value used for the in-position TIME exit) and trading-hour-bar counting
    (`compute._bars_held`), not wall-clock hours -- a wait spanning a night
    or weekend must not count faster than the kernel's own hourly-bar loop
    would. Kernel semantics on abandon are cancel-and-forget (no trade
    recorded, not a fallback market buy) -- matched here for every
    population (real live orders, dry_run rows with no real order, and paper
    rows via check_paper_entry_abandon below), since a market-buy-node's
    pending row never has a bounce phase to abandon (skipped via
    db._is_trailing_buy). Called unconditionally every poll, same cadence as
    the other pending_buys trackers.

    Reads account/max_hold_hours from pb['node'] (the signal-time pinned
    snapshot), not a live re-fetch of the watch_list row -- a real resting
    order's account is a physical fact fixed at placement, matching the
    established convention this module already follows for every other
    pending_buys consumer (check_gap_resize, check_auto_fills,
    _reconcile_buy_fill -- see the _fresh_node removal note above). Re-
    fetching live would let a later account edit on the node silently steer
    a real cancel_order call at the wrong account, or make a real
    dry_run=False order's cancel branch skip a live re-fetch entirely if the
    node's account was edited to a dry_run one in the meantime (caught by
    review before landing).

    Only ever cancels a real order when `order_id` is on file. The manual
    "Trailing Buy Order Placed" Slack flow sets order_placed=True but never
    captures a broker order id (the user places it directly at Schwab, we
    never see the id) -- for that case, `order_placed and not order_id`,
    this function alerts and leaves the row untouched rather than clearing
    it with a false "resting order cancelled" claim: a real order may still
    be resting with no local record at all, and clearing pending_buys here
    would also break handle_trail_buy_fill_price's stale-button guard, which
    needs this exact row to still exist to accept a later manual fill
    confirmation (caught by review before landing -- the first version of
    this function silently orphaned exactly this case).

    Known residual gap, flagged by a later review, not fixed: for a real
    account with `order_placed=False, order_id=None`, this function assumes
    nothing was ever placed and clears the row -- indistinguishable from "the
    user placed it manually at the broker but never tapped Trailing Buy Order
    Placed," which would silently drop tracking of a real still-resting
    order. `order_placed` is this system's only signal for "the button was
    tapped," so there's no way to detect that specific case from local state
    alone without a real broker order-book check, which this function
    deliberately doesn't do (see automation_principles.md #2 -- fail closed
    only where the ambiguity can actually be detected)."""
    today = datetime.now().strftime('%Y-%m-%d')
    for pb in db.get_pending_buys():
        wl_id = pb.get('wl_id')
        if not wl_id:
            continue
        node = pb['node']
        if not db._is_trailing_buy(node):
            continue
        if pb.get('gap_resize_date') == today:
            # check_gap_resize (run earlier in the same run_loop iteration,
            # active_signals.py) may have JUST replaced this row's resting
            # trailing-buy with a real MARKET order minutes/seconds ago,
            # writing the new order_id back onto this same pending_buys row
            # -- the node is still _is_trailing_buy, so without this guard a
            # daemon restart landing right at the bars_held>=max_hold_hours
            # threshold could have check_entry_abandon cancel that brand-new
            # market order in the very same iteration, falsely posting
            # "trailing buy never bounced ... entry abandoned" when the
            # trigger genuinely cleared moments earlier (found by review).
            continue
        ticker = pb['ticker']
        df_hourly, _ = compute._load_cache(ticker)
        signal_time = datetime.strptime(pb['signal_time'], '%Y-%m-%d %H:%M:%S')
        bars_held = compute._bars_held(df_hourly, signal_time)
        if bars_held < (node.get('max_hold_hours') or float('inf')):
            continue
        account = node.get('account')
        mode = _coverage_mode(account)
        order_id = pb.get('order_id')
        limits = schwab_safety.ACCOUNTS.get(account) if account else None
        if limits is None:
            # Unrecognized/missing account -- can't tell whether a real order
            # might be resting (dry_run status unknown), so can't tell
            # whether clearing this row is safe. Fail closed (automation_
            # principles.md #2): alert, don't touch the row.
            db.log_coverage_event("entry_abandon_timeout", mode, ticker=ticker, node_id=wl_id,
                                   result="unrecognized_account")
            _throttled_entry_abandon_alert(
                wl_id, "unrecognized_account",
                f"⏱️⚠️ *{ticker}* ({account!r} · {mode_tag(account)}) — trailing buy past its "
                f"{node['max_hold_hours']}h hold-time limit, but this account isn't recognized — cannot "
                f"determine whether a real order needs cancelling. Verify manually.")
            continue
        if not limits.dry_run and pb.get('order_placed') and not order_id:
            # Real (non-dry_run) account: order_placed=True with no order_id
            # is the manual "Trailing Buy Order Placed" Slack flow (the user
            # places it directly at Schwab -- we never capture its id), not a
            # dry_run placement (schwab_client's dry_run short-circuit is
            # indistinguishable from this by order_id alone, hence the
            # `not limits.dry_run` guard here). A real order may be resting
            # at the broker with no way to target a cancel_order call at it
            # -- surface it for manual handling instead of silently dropping
            # tracking (found by review: the first version of this function
            # cleared the row here with a false "resting order cancelled"
            # claim, permanently orphaning a real GTC order and breaking
            # handle_trail_buy_fill_price's later stale-button guard, which
            # needs this exact row to still exist to accept a manual fill).
            db.log_coverage_event("entry_abandon_timeout", mode, ticker=ticker, node_id=wl_id,
                                   result="no_order_id_on_file")
            _throttled_entry_abandon_alert(
                wl_id, "no_order_id_on_file",
                f"⏱️⚠️ *{ticker}* ({account} · {mode_tag(account)}) — trailing buy has been resting past "
                f"its {node['max_hold_hours']}h hold-time limit, but no broker order id is on file "
                f"(placed manually) — cannot auto-cancel. Cancel it manually at the broker if it's still "
                f"resting, and tap Skip on the reminder once confirmed.")
            continue
        did_cancel = False
        if order_id and not limits.dry_run:
            try:
                _, status = schwab_client.cancel_order(account, ticker, order_id)
            except Exception as e:
                db.log_coverage_event("entry_abandon_timeout", mode, ticker=ticker, node_id=wl_id,
                                       result="cancel_failed", detail=str(e))
                _throttled_entry_abandon_alert(
                    wl_id, "cancel_failed",
                    f"⚠️ *{ticker}* ({account} · {mode_tag(account)}) — entry-abandon timeout hit "
                    f"({bars_held}/{node['max_hold_hours']}h) but the cancel request itself failed ({e}) "
                    f"— resting order may still be live, verify and cancel manually.")
                continue
            if status == 'FILLED':
                # Raced a real bounce-fill landing the instant we tried to cancel --
                # reconcile it as a genuine fill (automation_principles.md #1: never
                # discard real broker truth), don't abandon it. Deliberately calls
                # _reconcile_buy_fill unconditionally, bypassing the auto_fill_
                # detection_enabled/node_auto_fill_detection_enabled opt-in gate
                # drain_fill_queue respects (flagged by a later review as the one
                # fill path that ignores it) -- the alternative here is dropping
                # tracking of a real, order-id-exact-confirmed fill entirely, which
                # is worse than a real position opening without the opt-in toggle
                # having been set. Not the same risk drain_fill_queue's gate guards
                # against (an ambiguous/best-effort ticker+account match).
                db.log_coverage_event("entry_abandon_timeout", mode, ticker=ticker, node_id=wl_id,
                                       result="raced_fill")
                fill = schwab_client.get_filled_order(account, ticker, 'BUY', order_id=order_id)
                if fill:
                    _reconcile_buy_fill(ticker, fill['price'], fill['quantity'], wl_id=wl_id)
                else:
                    _post_message(f"⚠️ *{ticker}* ({account} · {mode_tag(account)}) — entry-abandon "
                                  f"cancel found status FILLED but the fill lookup itself failed — "
                                  f"verify and reconcile manually.")
                continue
            if status != 'CANCELED':
                # Unconfirmed cancel -- fail closed (automation_principles.md #2):
                # leave the local row in place and retry next poll rather than
                # dropping tracking of a real order that may still be resting.
                db.log_coverage_event("entry_abandon_timeout", mode, ticker=ticker, node_id=wl_id,
                                       result="cancel_unconfirmed")
                continue
            did_cancel = True
        db.clear_pending_buy_by_wl_id(wl_id)
        db.log_coverage_event("entry_abandon_timeout", mode, ticker=ticker, node_id=wl_id,
                               result="abandoned",
                               detail=f"bars_held={bars_held} max={node['max_hold_hours']}")
        # did_cancel distinguishes a real confirmed cancel_order call from
        # every other path that reaches here with nothing real to cancel
        # (dry_run, or order_placed=False/no order ever placed) -- the
        # original message unconditionally claimed "resting order cancelled"
        # even on the dry_run path, where nothing was ever real (found by
        # review; this is a coverage_events-visible daily occurrence today,
        # DIA/SDOW on the dry_run `ira` account).
        cancelled_note = "resting order cancelled" if did_cancel else "no real order existed to cancel"
        _post_message(f"⏱️ *{ticker}* ({account} · {mode_tag(account)}) — trailing buy never bounced "
                      f"within {node['max_hold_hours']}h — entry abandoned, {cancelled_note}. "
                      f"No position opened.")


# ---------------------------------------------------------------------------
# Dry-run fill synthesis
# ---------------------------------------------------------------------------

def update_dry_run_buys():
    """Mirrors paper_trading.update_paper_buys, but for a real mode='live' node
    whose account is dry_run=True (schwab_safety.ACCOUNTS[account].dry_run).
    schwab_client short-circuits before the real broker call for such an
    account (_place_trailing_order/_place_equity_order both return (None, None)
    on dry_run), so the pending_buys row notify_buy_signal created will never
    be confirmed by a real fill event -- it just sits forever (the root cause
    of canaries/other dry_run nodes never showing a closed trade in
    coverage_check.py). Synthesizes the fill against real price data instead,
    writing to the real open_positions/trade_log tables tagged
    is_dry_run_sim=1 so it's never confused with a genuine fill. Called
    unconditionally every poll, same as paper_update_buys -- a dry_run
    trailing buy can bounce-fill any time after the signal fires."""
    for pb in db.get_pending_buys():
        wl_id = pb.get('wl_id')
        if not wl_id:
            # Legacy/unbackfillable row predating the wl_id migration -- the
            # frozen node_json snapshot's 'id' can't be trusted to key
            # update_pending_buy_running_low/clear_pending_buy_by_wl_id (both
            # WHERE wl_id=?, which NULL never matches), which would leave the
            # row stuck and re-fire a synthetic fill every poll forever (Opus
            # review 2026-07-26). Fail closed -- leave it to the manual/legacy
            # ticker-keyed flow instead.
            continue
        node = db.get_watch_list_node_by_id(wl_id)
        if node is None:
            continue
        if node.get('mode') != 'live':
            # A research-mode node's BUY never reaches here today (routed to
            # paper_trading.start_paper_buy instead, which never creates a
            # pending_buys row) -- explicit guard anyway, so a future routing
            # change can't open a real open_positions row alongside the node's
            # own paper_positions row for the same wl_id (Opus review 2026-07-26).
            continue
        account = node.get('account')
        limits = schwab_safety.ACCOUNTS.get(account)
        if not limits or not limits.dry_run:
            continue
        ticker = pb['ticker']
        price, _ = compute._current_price(ticker)
        if price is None:
            continue
        if db._is_trailing_buy(node):
            running_low = min(pb['running_low'] or pb['signal_price'], price)
            trail_buy_pct = node.get('trail_buy_pct') or 0.0
            trigger = running_low * (1 + trail_buy_pct / 100)
            log_poll(f"{ticker} dry_run_update_buys price={price:.4f} running_low={running_low:.4f} "
                     f"trigger={trigger:.4f}")
            if price > running_low and price >= trigger:
                _fill_dry_run_buy(node, pb, price)
            elif running_low != pb['running_low']:
                db.update_pending_buy_running_low(wl_id, running_low)
        else:
            # Market-buy-eligible node: a real market order fills near-immediately,
            # no bounce phase to simulate -- same reasoning as
            # paper_trading.start_paper_market_buy.
            _fill_dry_run_buy(node, pb, price)


def _fill_dry_run_buy(node, pb, price):
    ticker = node['ticker']
    trailing_buy = db._is_trailing_buy(node)
    sizing = buy_order_sizing(node, {'ticker': ticker, 'current_price': price})
    shares = sizing['shares']
    if shares < 1:
        print(f"  [dry-run-sim] {ticker} fill at ${price:.4f} too small to size a share — dropping pending buy")
        db.clear_pending_buy_by_wl_id(node['id'])
        return
    # hold-time origin: fill time for both signal_time and entry_time, not
    # the pending buy's original signal_time -- same fix and rationale as
    # _reconcile_buy_fill (2026-07-31). Harmless for a market-buy node (this
    # function's other caller), since that fill happens near-immediately
    # after the signal anyway.
    fill_time = datetime.now()
    opened = db.open_position(node, pb['signal_price'], fill_time, price, fill_time,
                               shares=shares, is_dry_run_sim=True)
    db.clear_pending_buy_by_wl_id(node['id'])
    if not opened:
        # Already open for this node (e.g. a prior poll's fill already landed) --
        # a silent skip must not be reported as a fill (same contract as every
        # other open_position() caller; Opus review 2026-07-26 caught this one
        # missing the check, which would otherwise re-post a false "would have
        # filled" message and a false coverage row every poll forever).
        print(f"  [dry-run-sim] {ticker} already has an open position — dropping duplicate pending buy")
        return
    db.log_coverage_event("entry_fill", "dry_run", ticker=ticker, node_id=node.get('id'),
                           result="sim_filled", detail=f"shares={shares} price={price:.4f}")
    label = "TRAILING BUY" if trailing_buy else "MARKET BUY"
    _post_message(f"[DRY RUN] would have filled {label} — {ticker}  {shares}sh @ ${price:.4f}")


def check_dry_run_sim_sells(last_seen_bar, dry_run_sell_alerted, load_cache):
    """Sell-side mirror of update_dry_run_buys, following the same pattern as
    paper_trading.check_paper_sells -- an is_dry_run_sim position has no real
    resting order at the broker, so nothing will ever confirm an exit either;
    close it immediately against real price data once check_sell_condition
    fires, instead of routing through notify_sell_signal's Slack-button wait
    (which real/live positions correctly use, since those DO have a real order
    to confirm)."""
    for pos in db.get_open_positions():
        if not pos.get('is_dry_run_sim'):
            continue
        ticker = pos['ticker']
        df_hourly, _ = load_cache(ticker)
        if df_hourly is None or df_hourly.empty:
            continue
        last_bar_ts = df_hourly.index[-1]
        if (pos['id'], last_bar_ts) in dry_run_sell_alerted:
            continue
        at_bar_close = resolve_at_bar_close(pos, last_bar_ts, last_seen_bar)
        if at_bar_close:
            bar = df_hourly.iloc[-1]
            cp, low, high, op = float(bar['Close']), float(bar['Low']), float(bar['High']), float(bar['Open'])
        else:
            cp, _ = compute._current_price(ticker)
            if cp is None:
                continue
            low = high = op = cp
        log_poll(f"{ticker} dry_run_check_sells bar={last_bar_ts} at_bar_close={at_bar_close} "
                 f"cp={cp:.4f} low={low:.4f} high={high:.4f} op={op:.4f}")
        reason, target, just_activated_trailing = compute.check_sell_condition(
            pos, cp, datetime.now(), at_bar_close=at_bar_close, low=low, high=high, open_price=op,
            df_hourly=df_hourly)
        if just_activated_trailing:
            _post_message(f"[DRY RUN] trailing-sell would arm — {ticker}")
        if reason:
            db.close_position(pos['id'], exit_signal_price=cp, exit_price=target,
                               exit_time=datetime.now(), exit_reason=reason)
            db.log_coverage_event("exit_fill", "dry_run", ticker=ticker, position_id=pos['id'],
                                   node_id=pos.get('wl_id'), result=reason, detail=f"price={target:.4f}")
            _post_message(f"[DRY RUN] would have closed — {ticker}  {reason} @ ${target:.4f}")
            dry_run_sell_alerted.add((pos['id'], last_bar_ts))


# ---------------------------------------------------------------------------
# Buy / sell notifications
# ---------------------------------------------------------------------------

def notify_buy_signal(node, sig):
    ticker   = sig['ticker']
    price    = sig['current_price']
    z        = sig['z_score']
    bar_time = sig['last_bar']
    bar_str  = bar_time.strftime('%Y-%m-%d %H:%M')
    arm      = db._tp_or_arm_pct(node)
    sl       = node['stop_loss']
    hold     = node['max_hold_hours']

    hurst_str = f"{sig['hurst']:.3f}" if sig.get('hurst') is not None else "n/a"
    adf_str   = f"{sig['adf_p']:.3f}" if sig.get('adf_p') is not None else "n/a"

    sep = '=' * 62
    print(f"\n{sep}")
    print(f"  BUY SIGNAL  {ticker}  {bar_str}")
    print(f"  Price:  ${price:.4f}   Lower band: ${sig['lower_band']:.4f}   z = {z:.2f}")
    print(f"  Node:   window={node['window']}  Arm={arm}%  SL={sl}%  hold={hold}h")
    print(f"  SMA: ${sig['sma']:.4f}   Std: ${sig['std']:.4f}")
    print(f"  Hurst (100 bars): {hurst_str}   ADF p: {adf_str}")
    _limits = schwab_safety.ACCOUNTS.get(node.get('account'))
    if db.closed_today(ticker):
        if _limits and _limits.account_type == 'cash':
            print(f"  ⚠️🔁 SAME DAY BUY WARNING: {ticker} already sold today — cash may not be settled (T+1)")
        elif not _limits:
            # Missing/unrecognized account -- can't tell if this is a cash
            # account or not, so warn rather than silently assume it's safe
            # (round 5 fixed the wrong-verdict case; this closes the
            # unknown-verdict case the same way, found in round 6).
            print(f"  ⚠️🔁 SAME DAY BUY WARNING: {ticker} already sold today, account "
                  f"'{node.get('account')}' not recognized — cash-settlement status unknown, confirm before entering")
    print(sep)

    trailing_buy = db._is_trailing_buy(node)
    market_buy_eligible = (not trailing_buy) and (ticker in schwab_safety.AUTOMATION_ENABLED_TICKERS)
    auto_placed = False
    order_id = None
    if trailing_buy:
        sizing = buy_order_sizing(node, sig)
        auto_placed, order_id = _attempt_automated_buy(node, sizing)
    elif market_buy_eligible:
        sizing = buy_order_sizing(node, sig)
        auto_placed, order_id = _attempt_automated_market_buy(node, sizing)

    channel, ts = _post_message(
        f"BUY SIGNAL — {ticker}  ${price:.4f}  z={z:.2f}  ({bar_str})",
        _build_buy_blocks(node, sig, auto_placed=auto_placed),
    )

    # Tracked regardless of INTERACTIVE -- a trailing-buy or automated-market-buy
    # order is still pending fill confirmation even in SIM_MODE or webhook-only
    # (non-socket) runs, where there's no button to click but the reminder loop
    # should still nag. Not gated on auto_placed -- a failed/blocked automated
    # placement must still fall back to the existing manual reminder flow
    # instead of silently dropping the signal.
    if trailing_buy or market_buy_eligible:
        db.add_pending_buy(node, sig, channel, ts, order_id=order_id)
        if auto_placed:
            if trailing_buy:
                db.mark_pending_buy_placed_by_wl_id(node['id'])
            else:
                # Market-buy sync-confirm (Part 4, Section 6) -- runs the
                # synchronous fast-confirm poll and, on a hit, opens/tops-up/
                # protects the position directly (via _reconcile_buy_fill),
                # rather than waiting on the async pending_buys reminder flow.
                _sync_confirm_and_protect(ticker, node, order_id)

    if market_buy_eligible and auto_placed:
        print(f"  {ticker} market buy order auto-placed — fill confirmation and SL placement "
              f"handled synchronously above.")
        return

    if cfg.INTERACTIVE:
        chart = _chart_buy(node, sig)
        if chart:
            _upload_chart(chart, f"{ticker}_buy.png", f"BUY — {ticker}  z={z:.2f}")
        print("  Waiting for Slack response (Executed / Skipped).")
        return

    if trailing_buy and auto_placed:
        print(f"  {ticker} trailing buy order auto-placed at the broker — waiting for fill.")
        return

    if trailing_buy:
        print("\nTrailing buy order placed at the broker? No position opens yet -- "
              "report the real fill separately once it happens. (y/n): ", end='', flush=True)
        try:
            resp = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            resp = ''
        if resp == 'y':
            db.mark_pending_buy_placed_by_wl_id(node['id'])
            print(f"  {ticker} order marked placed — no position yet, waiting for fill.")
            _post_message(f"{ticker} trailing buy order placed, waiting for fill.")
        else:
            db.clear_pending_buy_by_wl_id(node['id'])
            print("  Skipped.")
        return

    print("\nDid you execute? Enter price (or Enter to skip): ", end='', flush=True)
    try:
        resp = input().strip()
    except (EOFError, KeyboardInterrupt):
        resp = ''

    if resp:
        try:
            exec_price = float(resp)
            drift_pct  = (exec_price - price) / price * 100
            now        = datetime.now()
            opened     = db.open_position(node, price, bar_time, exec_price, now)
            db.clear_pending_buy_by_wl_id(node['id'])
            if not opened:
                print(f"  [warn] {ticker} already has an open position — ignored duplicate")
                _post_message(f"{ticker} — ALREADY OPEN, this fill was ignored. {_existing_position_note(ticker, wl_id=node['id'])}")
            else:
                note = f"Entered at ${exec_price:.4f}  (drift: {drift_pct:+.2f}%)"
                print(f"  Position opened. {note}")
                _post_message(f"{ticker} position opened: {note}")
        except ValueError:
            print("  Invalid price — position not opened.")
    else:
        db.clear_pending_buy_by_wl_id(node['id'])
        print("  Skipped.")


def notify_limit_fill(node, current_price, lower_band):
    ticker          = node['ticker']
    schwab_sl_pct   = node['stop_loss']
    schwab_sl_price = lower_band * (1 - schwab_sl_pct / 100)
    target_notional = _last_sale_recovery(node)
    shares          = int(target_notional // lower_band)
    now_str = datetime.now().strftime('%H:%M:%S')

    print(f"\n  [LIMIT FILL] {ticker}  price=${current_price:.2f}  trigger=${lower_band:.2f}  {now_str}")
    print(f"  Place stop: ${schwab_sl_price:.2f} (-{schwab_sl_pct}% from trigger)")

    account = node.get('account') or 'unmapped'
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"LIMIT FILLED — {ticker}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            f"✅ *{ticker}* limit filled at `${lower_band:.2f}` — `{shares} shares` (~${target_notional/1000:.0f}k) — `{account}`\n"
            f"🔴 Place Schwab stop: `${schwab_sl_price:.2f}` (-{schwab_sl_pct}% from trigger)"
        )}},
    ]
    _post_message(f"LIMIT FILLED — {ticker} at ${lower_band:.2f}", blocks=blocks)


def notify_sell_signal(pos, reason, current_price, target_price):
    ticker     = pos['ticker']
    ep         = pos['entry_price']
    entry_time = pos['entry_time']
    pct        = (current_price - ep) / ep * 100

    if reason == 'TIME':
        db.log_coverage_event("time_exit_trigger", _coverage_mode(pos.get('account')), ticker=ticker,
                               position_id=pos.get('id'), node_id=pos.get('wl_id'), result="alert_fired",
                               detail=f"target={target_price:.4f}")

    reason_labels = {'TP': 'TAKE PROFIT', 'SL': 'STOP LOSS', 'TIME': 'TIME EXIT', 'TRAIL': 'TRAILING STOP'}

    # Attempt automated execution before ever posting the manual alert -- for
    # TRAIL this reuses the order already resting from the earlier arm event;
    # for TP/SL/TIME this places a real market sell now, mirroring the
    # backtest kernel's own exact-bar-close exit assumption (previously these
    # 3 reasons had NO automated path at all, found 2026-07-27 real: SH's
    # TIME exit sat unmanaged for hours). A short bounded poll (same pattern
    # as check_gap_resize) checks for an immediate fill via the exact
    # order_id -- if confirmed, close now and skip the manual alert entirely;
    # a real fill confirmed by order_id is unambiguous (we know exactly which
    # order it is and its full share count), so no human tap is needed.
    order_id = _attempt_automated_exit_sell(pos, reason, current_price)
    filled = None
    if order_id is not None:
        account = pos.get('account')
        for _ in range(_GAP_FILL_POLL_ATTEMPTS):
            filled = schwab_client.get_filled_order(account, ticker, 'SELL', order_id=order_id)
            if filled is not None:
                break
            time.sleep(_GAP_FILL_POLL_INTERVAL_SECS)

    if filled is not None:
        actual_pnl = (filled['price'] - ep) / ep * 100
        closed = db.close_position(pos['id'], exit_signal_price=current_price, exit_price=filled['price'],
                                    exit_time=datetime.now(), exit_reason=reason)
        if closed:
            db.log_coverage_event("automated_exit_confirmed", _coverage_mode(pos.get('account')), ticker=ticker,
                                   position_id=pos.get('id'), node_id=pos.get('wl_id'), result="closed",
                                   detail=f"reason={reason} price={filled['price']:.4f}")
            _post_message(f"🤖 {ticker} — {reason_labels[reason]} auto-closed at ${filled['price']:.4f}  "
                          f"(P&L: {(filled['price']-ep)/ep*100:+.2f}%)")
            print(f"  Auto-closed via confirmed broker fill @ ${filled['price']:.4f}  (P&L: {actual_pnl:+.2f}%)")
        return

    sep = '=' * 62
    print(f"\n{sep}")
    print(f"  SELL SIGNAL  {ticker}  — {reason_labels[reason]}")
    print(f"  Entry: ${ep:.4f}  →  Current: ${current_price:.4f}  ({pct:+.2f}%)")
    print(f"  Target: ${target_price:.4f}   Node: Arm={db._tp_or_arm_pct(pos)}%  SL={pos['stop_loss']}%  hold={pos['max_hold_hours']}h")
    print(f"  Entered: {entry_time}")
    print(sep)

    channel, ts = _post_message(
        f"SELL SIGNAL — {ticker}  {reason_labels[reason]}  ${current_price:.4f}  ({pct:+.2f}%)",
        _build_sell_blocks(pos, reason, current_price, target_price),
    )

    # Tracks the exit as unresolved until Exited/Skipped -- unlike a placed trailing-buy
    # (waiting on a broker fill we can't detect), a stalled SELL confirmation means an
    # already-open position with real capital sitting unmanaged, arguably more urgent to
    # nag about than the buy side. order_id (None if out of automation scope) lets
    # check_own_sell_fills keep rechecking the exact real order every poll cycle and
    # auto-close the moment it's confirmed FILLED -- the manual tap below is only the
    # fallback path, not the sole way this ever resolves.
    #
    # Re-fetch fresh before reading trail_state -- _attempt_automated_exit_sell
    # (called above, same function) may have just persisted a real update
    # (exit_order_id, on a hold-time-forced force-replace) using its OWN pos
    # snapshot; `pos` here is whatever the CALLER fetched before invoking
    # notify_sell_signal, one step earlier still. Building this write from the
    # stale copy would immediately overwrite that just-made update -- the
    # exact same clobber pattern already fixed 3 times elsewhere this session,
    # reintroduced by the exit_order_id fix itself and caught before landing
    # (2026-07-31).
    fresh_for_state = db.get_position_by_id(pos['id']) or pos
    state = dict(fresh_for_state.get('trail_state') or {})
    state['exit_pending'] = {
        'reason': reason, 'current_price': current_price, 'target_price': target_price,
        'reminder_channel': channel, 'reminder_ts': ts, 'reminder_count': 0,
        'last_reminder_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'order_id': order_id,
    }
    db.update_position_trail_state(pos['id'], state)

    if cfg.INTERACTIVE:
        chart = _chart_sell(pos, current_price)
        if chart:
            _upload_chart(chart, f"{ticker}_sell.png", f"SELL — {ticker}  {reason_labels[reason]}  {pct:+.2f}%")
        print("  Waiting for Slack response (Exited / Skipped).")
        return

    print("\nDid you exit? Enter price (or Enter to skip): ", end='', flush=True)
    try:
        resp = input().strip()
    except (EOFError, KeyboardInterrupt):
        resp = ''

    if resp:
        try:
            exit_price = float(resp)
            drift_pct  = (exit_price - current_price) / current_price * 100
            actual_pnl = (exit_price - ep) / ep * 100
            note = f"Exited at ${exit_price:.4f}  (signal drift: {drift_pct:+.2f}%  P&L: {actual_pnl:+.2f}%)"
            db.close_position(pos['id'], exit_signal_price=current_price, exit_price=exit_price,
                               exit_time=datetime.now(), exit_reason=reason)
            print(f"  Position closed. {note}")
            _post_message(f"{ticker} position closed: {note}")
        except ValueError:
            print("  Invalid price — position kept open.")
    else:
        fresh_for_state = db.get_position_by_id(pos['id']) or pos
        state = dict(fresh_for_state.get('trail_state') or {})
        state.pop('exit_pending', None)
        db.update_position_trail_state(pos['id'], state)
        print("  Skipped — position kept open.")


TRAIL_REMINDER_MINUTES = 15


def _trailing_order_blocks(pos, current_price, reminder_num=0):
    ticker    = pos['ticker']
    account   = pos.get('account') or 'unmapped'
    ep        = pos['entry_price']
    pct       = (current_price - ep) / ep * 100
    shares    = pos.get('shares')
    trail_pct = pos.get('trail_sell_pct')
    order_desc = (
        f"SELL {shares:g} @ {trail_pct:g}% trail" if (shares and trail_pct)
        else "SELL (shares/trail% unavailable — check the node config)"
    )
    # Mandatory for the automated path (_attempt_automated_sell uses an
    # atomic replace specifically so the old SL is never left resting
    # alongside a new trailing-sell -- both live simultaneously for the same
    # shares is an oversell/rejected-order risk, per that function's own
    # docstring). The manual alert never said this at all -- found via
    # arming-logic walkthrough, 2026-07-31: a user following it literally
    # ends up in exactly the state the automated path goes out of its way to
    # prevent.
    cancel_note = (
        f" Cancel the existing stop-loss order ({pos['sl_order_id']}) first."
        if pos.get('sl_order_id') else ""
    )
    header    = f"⚠️ *{ticker}* ({account} · {mode_tag(account)}) — STILL PENDING (reminder #{reminder_num})" if reminder_num else f"🎯 *{ticker}* ({account} · {mode_tag(account)}) — TRAILING ACTIVATED — action needed"
    if reminder_num:
        text = (
            f"{header}\n"
            f"{order_desc}  |  entry `${ep:.2f}`  |  current `${current_price:.2f}`  |  P&L `{pct:+.1f}%`\n"
            f"Trailing stop order not yet confirmed placed at the broker.{cancel_note}"
        )
    else:
        text = (
            f"{header}\n"
            f"{order_desc}  |  entry `${ep:.2f}`  |  current `${current_price:.2f}`  |  P&L `{pct:+.1f}%`\n"
            f"Place the trailing stop order at the broker now.{cancel_note}"
        )
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if cfg.INTERACTIVE:
        value = json.dumps({"position_id": pos['id'], "ticker": ticker})
        blocks.append({
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Order Placed"},
                 "style": "primary", "action_id": "trail_order_placed", "value": value},
            ],
        })
    else:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": "No interactive buttons — confirm the trailing stop order is placed in the terminal running the daemon."}
        ]})
    return blocks


def _supersede_message(channel, ts, ticker):
    if not (cfg.SOCKET_MODE and channel and ts):
        return
    try:
        cfg.bolt_app.client.chat_update(
            channel=channel, ts=ts,
            text=f"{ticker} trailing order reminder — superseded",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"~_{ticker} trailing order reminder — superseded, see newer message below_~"}}],
        )
    except Exception as e:
        print(f"  [slack error] supersede failed: {e}")


def notify_trailing_activated(pos, current_price):
    ticker = pos['ticker']
    auto_placed, exit_order_id = _attempt_automated_sell(pos, current_price)
    if auto_placed:
        channel, ts = _post_message(
            f"🤖 {ticker} trailing stop activated — order auto-placed at the broker",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"🎯 *{ticker}* — TRAILING ACTIVATED — order auto-placed at the broker"}}],
        )
    else:
        blocks = _trailing_order_blocks(pos, current_price, reminder_num=0)
        channel, ts = _post_message(
            f"{ticker} trailing stop activated — place order", blocks=blocks)
    # check_sell_condition already persisted the newly-armed state (trailing,
    # peak) to the DB before calling this function -- pos['trail_state'] here
    # is still the pre-arm in-memory copy the caller passed in. Merging the
    # reminder fields onto that stale dict would silently clobber trailing/
    # peak right after arming, causing check_exit to re-arm on the next bar
    # and _attempt_automated_sell to place a second live trailing-sell order
    # for the same shares (found via Opus review, 2026-07-22). Re-read the
    # position fresh so the merge starts from the real just-armed state.
    fresh = db.get_position_by_id(pos['id']) or pos
    state = dict(fresh.get('trail_state') or {})
    db.log_coverage_event(
        "trailing_arm_state_reread", _coverage_mode(pos.get('account')), ticker=ticker,
        position_id=pos['id'], node_id=fresh.get('wl_id'),
        result="trailing_preserved" if state.get('trailing') else "trailing_missing_regression",
    )
    state['reminder_channel'] = channel
    state['reminder_ts']      = ts
    state['reminder_count']   = 0
    state['last_reminder_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if auto_placed:
        state['order_placed'] = True
        state['exit_order_id'] = exit_order_id
    db.update_position_trail_state(pos['id'], state)


def check_trailing_reminders(open_positions):
    """Nags every TRAIL_REMINDER_MINUTES until the trailing-stop order is confirmed
    placed -- a single one-time alert is too easy to miss, and an unplaced trailing
    stop between polls is a real risk if price moves fast."""
    now = datetime.now()
    for pos in open_positions:
        # A dry_run-sim position's arm event never places a real order (see
        # check_dry_run_sim_sells) -- order_placed can never become True for
        # it, so without this it would nag "confirm order placed" forever
        # for something with nothing real to confirm (same bug class as
        # check_buy_reminders, found live 2026-07-29).
        if pos.get('is_dry_run_sim'):
            continue
        # open_positions is a single snapshot taken once at the top of the
        # poll cycle -- an earlier step in this SAME cycle (_check_position_exit,
        # _scan_pinned_exit_arm) may have already persisted a newer trail_state
        # (e.g. exit_forced_by_hold_time, a fresh exit_pending/order_id, an
        # updated peak). Re-fetching before reading avoids rebuilding this
        # write from that stale copy and clobbering the newer one -- same bug
        # class as the SH stuck-exit incident (2026-07-29/30), found live via
        # a full execution-path walkthrough 2026-07-31 rather than a diff
        # review (this exact function was never touched by that earlier fix).
        # A None re-fetch means another path closed this position earlier in
        # the same cycle -- skip outright rather than falling back to the
        # stale copy, so a just-closed position can't get a spurious reminder.
        pos = db.get_position_by_id(pos['id'])
        if pos is None:
            continue
        state = pos.get('trail_state') or {}
        if not state.get('trailing') or state.get('order_placed'):
            continue
        last_at_str = state.get('last_reminder_at')
        if not last_at_str:
            continue
        last_at = datetime.strptime(last_at_str, '%Y-%m-%d %H:%M:%S')
        if (now - last_at).total_seconds() < TRAIL_REMINDER_MINUTES * 60:
            continue
        cp, _ = compute._current_price(pos['ticker'])
        if cp is None:
            continue
        _supersede_message(state.get('reminder_channel'), state.get('reminder_ts'), pos['ticker'])
        reminder_num = state.get('reminder_count', 0) + 1
        blocks = _trailing_order_blocks(pos, cp, reminder_num=reminder_num)
        channel, ts = _post_message(
            f"{pos['ticker']} trailing order — still pending (reminder #{reminder_num})", blocks=blocks)
        new_state = dict(state)
        new_state['reminder_channel'] = channel
        new_state['reminder_ts']      = ts
        new_state['reminder_count']   = reminder_num
        new_state['last_reminder_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
        db.update_position_trail_state(pos['id'], new_state)


EXIT_REMINDER_MINUTES = 15


def _exit_pending_blocks(pos, exit_pending, reminder_num):
    """Mirrors _trailing_order_blocks for the sell side. A stalled SELL
    confirmation means an already-open position with real capital sitting
    unmanaged -- arguably more urgent than a stalled BUY, so this reuses the
    same 'Exited'/'Skipped' buttons (sell_exited/sell_skipped) as the original
    alert rather than inventing new action_ids."""
    ticker        = pos['ticker']
    account       = pos.get('account') or 'unmapped'
    ep            = pos['entry_price']
    reason        = exit_pending['reason']
    current_price = exit_pending['current_price']
    target_price  = exit_pending['target_price']
    pct           = (current_price - ep) / ep * 100
    reason_labels = {'TP': 'TAKE PROFIT', 'SL': 'STOP LOSS', 'TIME': 'TIME EXIT', 'TRAIL': 'TRAILING STOP'}
    bsp = pos.get('broker_stop_price')

    if reason == 'SL' and bsp:
        status_line = (
            f"Protected by broker stop-loss on file @ `${bsp:.2f}` — should auto-fill there without "
            f"action from you. Confirm here once you see the fill in your account."
        )
    else:
        status_line = (
            f"Position may still be open and unmanaged at the broker. Confirm Exited with the real fill "
            f"price, or Skip if it turns out the exit condition no longer applies."
        )
    text = (
        f"⚠️ *{ticker}* ({account} · {mode_tag(account)}) — EXIT NOT CONFIRMED (reminder #{reminder_num})\n"
        f"{reason_labels[reason]}  |  entry `${ep:.2f}`  |  signal `${current_price:.2f}`  |  P&L `{pct:+.1f}%`\n"
        f"{status_line}"
    )
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if cfg.INTERACTIVE:
        value = json.dumps({
            "type": "sell", "position_id": pos['id'], "ticker": ticker,
            "current_price": current_price, "entry_price": ep, "reason": reason,
        })
        blocks.append({
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Exited"},
                 "style": "primary", "action_id": "sell_exited", "value": value},
                {"type": "button", "text": {"type": "plain_text", "text": "Skipped"},
                 "action_id": "sell_skipped", "value": value},
            ],
        })
    else:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": "No interactive buttons — type the exit price into the terminal running the daemon when filled."}
        ]})
    return blocks


def check_own_sell_fills(open_positions):
    """Every poll cycle, rechecks any unresolved exit_pending that has a known
    order_id (an order WE placed via _attempt_automated_exit_sell/an earlier
    TRAIL arm) -- if Schwab now confirms that exact order FILLED, closes the
    position automatically. Deliberately unconditional, NOT gated behind
    schwab_safety.auto_fill_detection_enabled (that opt-in toggle is for
    detecting a fill on an order we did NOT place ourselves, an inherently
    fuzzier signal) -- once we hold the specific order_id and Schwab confirms
    it FILLED, there's no ambiguity left to gate on. The manual Exited/Skipped
    tap in check_exit_reminders remains as the fallback for tickers outside
    automation scope (order_id is None there) or if this exact-order lookup
    itself ever fails."""
    for pos in open_positions:
        state = pos.get('trail_state') or {}
        exit_pending = state.get('exit_pending')
        if not exit_pending:
            continue
        order_id = exit_pending.get('order_id')
        if order_id is None:
            continue
        account = pos.get('account')
        if not account:
            continue
        ticker = pos['ticker']
        fill = schwab_client.get_filled_order(account, ticker, 'SELL', order_id=order_id)
        if fill is None:
            continue
        actual_pnl = (fill['price'] - pos['entry_price']) / pos['entry_price'] * 100
        closed = db.close_position(pos['id'], exit_signal_price=exit_pending['current_price'],
                                    exit_price=fill['price'], exit_time=datetime.now(),
                                    exit_reason=exit_pending['reason'])
        if not closed:
            # Already closed this same cycle by check_auto_fills (opt-in, same
            # order_id) reading the same stale open_positions snapshot -- avoid a
            # duplicate/misleading Slack post for a close that already happened.
            continue
        db.log_coverage_event("automated_exit_confirmed", _coverage_mode(account), ticker=ticker,
                              position_id=pos.get('id'), node_id=pos.get('wl_id'), result="closed",
                              detail=f"reason={exit_pending['reason']} price={fill['price']:.4f} via_recheck=1")
        _post_message(f"🤖 {ticker} — auto-detected exit fill at ${fill['price']:.4f}  (P&L: {actual_pnl:+.2f}%)")


def check_exit_reminders(open_positions):
    """Nags every EXIT_REMINDER_MINUTES until a fired SELL signal is confirmed
    Exited or Skipped ('4r' in the buy/sell lifecycle numbering) -- mirrors
    check_trailing_reminders' supersede-not-edit-in-place pattern. Without this,
    a stalled SELL confirmation is invisible until the user happens to remember."""
    now = datetime.now()
    for pos in open_positions:
        # Same re-fetch rationale as check_trailing_reminders above -- this
        # loop's open_positions is a stale, once-per-cycle snapshot, and an
        # earlier step in the same poll cycle may have already persisted a
        # newer trail_state (a just-force-replaced exit_pending/order_id,
        # exit_forced_by_hold_time) that this read would otherwise miss and
        # then clobber back to the old value. A None re-fetch means another
        # path closed this position earlier in the same cycle -- skip outright
        # rather than posting a spurious reminder off the stale copy.
        pos = db.get_position_by_id(pos['id'])
        if pos is None:
            continue
        state = pos.get('trail_state') or {}
        exit_pending = state.get('exit_pending')
        if not exit_pending:
            continue
        last_at_str = exit_pending.get('last_reminder_at')
        if not last_at_str:
            continue
        last_at = datetime.strptime(last_at_str, '%Y-%m-%d %H:%M:%S')
        if (now - last_at).total_seconds() < EXIT_REMINDER_MINUTES * 60:
            continue
        _supersede_message(exit_pending.get('reminder_channel'), exit_pending.get('reminder_ts'), pos['ticker'])
        reminder_num = exit_pending.get('reminder_count', 0) + 1
        blocks = _exit_pending_blocks(pos, exit_pending, reminder_num)
        channel, ts = _post_message(
            f"{pos['ticker']} exit — still not confirmed (reminder #{reminder_num})", blocks=blocks)
        new_state = dict(state)
        new_exit_pending = dict(exit_pending)
        new_exit_pending['reminder_channel'] = channel
        new_exit_pending['reminder_ts']      = ts
        new_exit_pending['reminder_count']   = reminder_num
        new_exit_pending['last_reminder_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
        new_state['exit_pending'] = new_exit_pending
        db.update_position_trail_state(pos['id'], new_state)


BUY_REMINDER_MINUTES = 15


def _trailing_buy_status(pending):
    """Best-effort live approximation of the backtest's waiting-state bounce check
    (_simulate_trail_both's running_low/buy_trigger) -- tracks the running low across
    hourly bars since the signal fired and checks whether price has already bounced
    back up by trail_buy_pct%. Only as accurate as the hourly cache (no true intrabar
    low live, same caveat as compute_buy_signal) -- a reasonable signal for reminder
    wording, not a substitute for the real live state machine (still unimplemented,
    tracked in docs/backlog_cache.md).
    running_low is anchored to pending['signal_price'] -- the same real basis
    check_gap_resize uses for the real order's trigger -- not re-derived from
    the hourly cache's first Low. Confirmed live 2026-07-24 (GDXU): the real
    order was placed off a $79.665 signal-price-anchored trigger ($79.90) and
    genuinely filled at $80.805, but the old cache-derived running_low computed
    a meaningfully different $81.14 trigger and wrongly returned met=False,
    silently suppressing a fill reminder for an order that had already filled.
    Hourly bars are still used from here forward to track any further real dip
    before the bounce -- only the anchor was wrong, not the ongoing tracking."""
    node = pending['node']
    trail_buy_pct = (node.get('trail_buy_pct') or 0) / 100.0
    if not trail_buy_pct:
        return None, None
    running_low = float(pending['signal_price'])
    trigger = running_low * (1 + trail_buy_pct)
    df_hourly, _ = compute._load_cache(pending['ticker'])
    if df_hourly is None:
        # met=None (unknown), not False -- check_buy_reminders treats False as
        # "confirmed not met yet, safe to skip nagging" but None as "can't
        # tell, nag anyway." Returning False here would have silently
        # suppressed reminders for every ticker with no cache at all -- caught
        # by Opus review, same failure mode this whole fix exists to close.
        return None, trigger
    signal_time = datetime.strptime(pending['signal_time'], '%Y-%m-%d %H:%M:%S')
    bars = df_hourly[df_hourly.index >= signal_time]
    met = False
    for _, bar in bars.iterrows():
        if bar['Low'] < running_low:
            running_low = float(bar['Low'])
            trigger = running_low * (1 + trail_buy_pct)
        if bar['High'] >= trigger:
            met = True
            break
    return met, trigger


def _pending_buy_blocks(pending, reminder_num):
    ticker = pending['ticker']
    node = pending['node']
    account = node.get('account') or 'unmapped'
    placed = pending['order_placed']
    met, trigger = _trailing_buy_status(pending)

    if placed:
        header = f"⚠️ *{ticker}* ({account} · {mode_tag(account)}) — FILL NOT CONFIRMED (reminder #{reminder_num})"
        trigger_str = f"  |  bounce trigger `${trigger:.2f}`" if trigger is not None else ""
        text = (
            f"{header}\n"
            f"Trailing buy order placed at the broker but not yet confirmed filled{trigger_str}.\n"
            f"Confirm Filled with the real fill price, Missed It if the bounce already passed before the "
            f"order was live, or Cancelled if the order didn't go through."
        )
    else:
        header = f"⚠️ *{ticker}* ({account} · {mode_tag(account)}) — ORDER NOT CONFIRMED PLACED (reminder #{reminder_num})"
        text = (
            f"{header}\n"
            f"BUY signal fired but no confirmation the trailing buy order was placed at the broker.\n"
            f"Confirm once it's resting at the broker, or Skip if you're not taking this trade."
        )
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    if cfg.INTERACTIVE:
        value = json.dumps({"node": node, "signal_price": pending['signal_price'],
                             "signal_time": pending['signal_time']})
        if placed:
            blocks.append({
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Filled"},
                     "style": "primary", "action_id": "trail_buy_filled", "value": value},
                    {"type": "button", "text": {"type": "plain_text", "text": "Missed It"},
                     "action_id": "trail_buy_missed", "value": value},
                    {"type": "button", "text": {"type": "plain_text", "text": "Cancelled"},
                     "action_id": "trail_buy_cancelled", "value": value},
                ],
            })
        else:
            blocks.append({
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Trailing Buy Order Placed"},
                     "style": "primary", "action_id": "trail_buy_order_placed", "value": value},
                    {"type": "button", "text": {"type": "plain_text", "text": "Skipped"},
                     "action_id": "buy_skipped", "value": value},
                ],
            })
    else:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": "No interactive buttons — confirm in the terminal running the daemon."}
        ]})
    return blocks


def check_buy_reminders():
    """Nags every BUY_REMINDER_MINUTES until a trailing-buy is fully resolved
    (Filled or Skipped) -- without this, a stalled trailing-buy at the broker is
    invisible until the user happens to remember to check (the gap flagged in
    docs/operational_limits.md's TrailingBoth lifecycle table, row 3). Unlike the
    sell side's order_placed (which needs no further confirmation once placed),
    the buy side keeps nagging after order_placed=True too -- there's no way to
    detect a live fill, so a placed-but-unconfirmed order still needs a real
    Filled/Skip answer, never silently assumed (_pending_buy_blocks switches
    wording/buttons for this phase)."""
    now = datetime.now()
    for pending in db.get_pending_buys():
        # A dry_run account's pending buy is resolved entirely by
        # update_dry_run_buys' own synthesis, independent of any human
        # action -- nagging "Confirm Filled / Missed It / Cancelled" here is
        # meaningless (there's no real order at the broker to confirm) and
        # was pure noise until the synthesis resolved it on its own anyway.
        # Found live 2026-07-29 (FAZ): 14 reminders over ~2 hours, all before
        # a fill that happened automatically regardless of any of them.
        account = pending['node'].get('account')
        limits = schwab_safety.ACCOUNTS.get(account)
        if limits and limits.dry_run:
            continue
        last_at = datetime.strptime(pending['last_reminder_at'], '%Y-%m-%d %H:%M:%S')
        if (now - last_at).total_seconds() < BUY_REMINDER_MINUTES * 60:
            continue
        if pending['order_placed']:
            # Fill-confirmation phase: nagging every 15min regardless of whether a fill
            # is even plausible yet is noisy (e.g. KORU's wide 12% trail_buy_pct can
            # genuinely take a while). Only start nagging once the bounce trigger has
            # plausibly been hit; met=None (unknown -- e.g. stale/missing cache) still
            # nags, erring toward not silently dropping a real stalled fill.
            met, _ = _trailing_buy_status(pending)
            if met is False:
                continue
        _supersede_message(pending['reminder_channel'], pending['reminder_ts'], pending['ticker'])
        reminder_num = pending['reminder_count'] + 1
        blocks = _pending_buy_blocks(pending, reminder_num)
        channel, ts = _post_message(
            f"{pending['ticker']} trailing buy — still pending (reminder #{reminder_num})", blocks=blocks)
        db.update_pending_buy_reminder(pending['id'], channel, ts, reminder_num)


def _reconcile_fill(node, fill_price, filled_shares, is_gap_correction=False):
    """Post-fill top-up (Part 3, branch C) -- compares the real fill notional
    against target_notional (the conservative worst-case sizing pads in
    buy_order_sizing/check_gap_resize mean a real fill usually comes in under
    budget) and tops up the position with a market buy for the difference. A
    meaningful overspend (rare -- the large-gap case is already prevented by
    check_gap_resize) gets a notify-only, no corrective sell, per Part 3 design.
    Called only from _reconcile_buy_fill, which clears the pending_buys row
    first -- that's the dedup marker preventing a double top-up if both the
    poll and the websocket path notice the same fill.
    Places a real (or dry_run) broker market buy for the top-up shares before
    recording them -- found and fixed 2026-07-21: this previously only wrote
    open_positions.shares/entry_price via db.top_up_position with no broker
    call at all, so the account never actually held the "topped-up" shares
    while every downstream sell order (SL, trailing-sell) sized off the
    inflated share count -- a real oversell/short-sell risk.
    is_gap_correction is passed through from the triggering fill (True only
    when the fill itself came from check_gap_resize's MARKET replacement,
    which runs at _GAP_CHECK_WINDOW, outside _SIGNAL_WINDOWS/_OPEN_CHECK_WINDOWS)
    so the top-up buy isn't wrongly blocked by the signal-window time gate."""
    ticker = node['ticker']
    account = node.get('account')
    target_notional = _last_sale_recovery(node)
    delta = target_notional - (fill_price * filled_shares)
    if delta > fill_price:
        top_up_shares = int(delta // fill_price)
        if top_up_shares > 0:
            try:
                schwab_client.place_equity_buy(account, ticker, top_up_shares, fill_price,
                                                is_gap_correction=is_gap_correction, is_protective=True)
            except schwab_safety.SafetyViolation as e:
                db.log_coverage_event("top_up", _coverage_mode(account), ticker=ticker,
                                       node_id=node.get('id'), result="blocked", detail=str(e))
                _post_message(f"🚫 {ticker} — top-up buy of {top_up_shares} shares blocked: {e} "
                              f"(position stays under target notional by ${delta:,.0f})")
                return
            except Exception as e:
                db.log_coverage_event("top_up", _coverage_mode(account), ticker=ticker,
                                       node_id=node.get('id'), result="failed_unexpectedly", detail=str(e))
                _post_message(f"⚠️ {ticker} — top-up buy of {top_up_shares} shares failed "
                              f"unexpectedly: {e} (position stays under target notional by "
                              f"${delta:,.0f})")
                return
            if db.top_up_position(node['id'], top_up_shares, fill_price):
                db.log_coverage_event("top_up", _coverage_mode(account), ticker=ticker,
                                       node_id=node.get('id'), result="placed", detail=f"shares={top_up_shares} price={fill_price:.4f}")
                _post_message(f"➕ {ticker} — top-up buy {top_up_shares} shares @ ${fill_price:.4f} "
                              f"(fill was under target notional by ${delta:,.0f})")
            else:
                # The real top-up order is already placed at the broker at this
                # point -- if the DB-side update can't find a matching position
                # (wl_id mismatch/NULL), the account now holds more shares than
                # open_positions.shares records, understating every downstream
                # SL/trailing-sell sizing. Must not fail silently.
                db.log_coverage_event("top_up", _coverage_mode(account), ticker=ticker,
                                       node_id=node.get('id'), result="db_update_failed_after_real_order",
                                       detail=f"shares={top_up_shares} price={fill_price:.4f}")
                _post_message(
                    f"🚨 {ticker} — top-up BUY of {top_up_shares} shares @ ${fill_price:.4f} was placed "
                    f"at the broker, but the position record could not be updated (no matching open "
                    f"position for this node) — open_positions.shares is now UNDERSTATED, SL/trailing-sell "
                    f"sizing will be wrong until this is corrected manually."
                )
    elif delta < -fill_price:
        db.log_coverage_event("top_up", _coverage_mode(account), ticker=ticker,
                               node_id=node.get('id'), result="overspent_no_corrective_sell", detail=f"overspend=${-delta:,.0f}")
        _post_message(f"⚠️ {ticker} — fill exceeded target notional by ${-delta:,.0f} "
                      f"(no corrective sell placed)")


def _reconcile_buy_fill(ticker, fill_price, filled_shares, is_gap_correction=False, wl_id=None):
    """Single entry point for a detected BUY fill -- shared by check_auto_fills
    (slow poll path), drain_fill_queue (fast websocket path, Part 3),
    check_gap_resize's fill poll (Part 3), and _sync_confirm_and_protect
    (synchronous fast-confirm path, Part 4). Clears the pending_buys row
    before acting (the existing dedup marker: whichever path notices the fill
    first wins, the other finds nothing to do), opens the real position, then
    tops it up via _reconcile_fill (is_gap_correction passed through so a
    top-up following a gap-correction fill isn't blocked by the signal-window
    gate). Places the resting STOP order for any automated fill (market-buy or
    trailing-buy) in automation scope -- determined here rather than left to
    each caller, so the SL genuinely gets placed regardless of *which* path
    ends up detecting the fill (previously only the synchronous fast-confirm
    path passed place_sl=True explicitly; if that path timed out, the async
    fallback paths below silently never placed a stop at all, contradicting
    the timeout alert's own claim that the fallback would eventually cover it
    -- found and fixed 2026-07-21). Trailing-buy (TrailingBothZScoreBreakout)
    fills were excluded here until 2026-07-24 -- found live that this left
    every real automated TrailingBoth fill with no broker-side stop at all,
    same exposure as a manually-seeded position with no broker_stop_price on
    file. This auto-fill path is the minority case for trailing-buy fills
    (the manual 'Filled' Slack button is the primary workflow, see
    handle_trail_buy_fill_price in signals_handlers.py, which places its own
    SL for the same reason).
    wl_id (when the caller has it -- every caller except drain_fill_queue's
    stream entry point does) disambiguates which node's pending row this real
    broker fill belongs to -- the broker fill event itself only ever carries
    ticker/account, so 2+ concurrent pending buys for the same ticker can't be
    told apart from the fill alone (see docs/backlog_cache.md's wl_id refactor
    entry). Ambiguity is surfaced, never silently guessed (automation_
    principles.md #4)."""
    pendings = [p for p in db.get_pending_buys() if p['ticker'] == ticker]
    if not pendings:
        return
    if wl_id is not None:
        matched = [p for p in pendings if p['node']['id'] == wl_id]
        if len(pendings) > 1:
            # Real disambiguation scenario: 2+ nodes have a resting pending
            # buy for this ticker at once -- did wl_id resolve the right one.
            _mode = _coverage_mode(pendings[0]['node'].get('account'))
            db.log_coverage_event(
                "buy_fill_reconciles_correct_node", _mode, ticker=ticker, node_id=wl_id,
                result="resolved" if matched else "no_match",
                detail=f"{len(pendings)} pending, wl_ids={[p['node']['id'] for p in pendings]}")
        if not matched:
            # The hinted node has no resting pending row (already resolved by
            # another path, or a stale hint) -- reconciling against a
            # *different* node's row here would silently attribute a real
            # fill to the wrong node's account/params. Surface it instead of
            # guessing (automation_principles.md #4).
            _post_message(f"⚠️ {ticker} — fill detected for node wl_id={wl_id} but no matching "
                          f"pending_buys row found (pending wl_ids: {[p['node']['id'] for p in pendings]}) "
                          f"— not reconciled, verify and record manually.")
            return
        pendings = matched
    pendings.sort(key=lambda p: p['created_at'])
    if len(pendings) > 1:
        _post_message(f"⚠️ {ticker} — {len(pendings)} pending buys matched this fill"
                      f"{f' (wl_id={wl_id})' if wl_id is not None else ' (wl_id could not disambiguate)'}"
                      f" — reconciling against the oldest; verify the others manually.")
    pending = pendings[0]
    node = pending['node']
    signal_price = pending['signal_price']
    db.clear_pending_buy_by_wl_id(node['id'])
    # hold-time origin: pass the real fill moment as BOTH signal_time and
    # entry_time, not the pending buy's original (earlier) signal_time --
    # _bars_held (signals_compute.py) counts hold time from pos['signal_time'],
    # and the backtest kernel's real basis for a trailing-buy fill is the FILL
    # bar (backtester.py's _simulate_trail_both/_simulate_trail_buy: `entry_bar
    # = i; held = 0` at the bounce-fill, with the wait itself tracked
    # separately via wait_bars and explicitly excluded from held). Passing the
    # original signal_time here (as before) let the bounce-wait silently eat
    # into the position's hold-time budget, causing premature TIME exits on
    # any trailing-buy fill that took real time to bounce (found via a fresh
    # execution-path walkthrough, 2026-07-31). This is deliberately NOT the
    # same as the manual-catch-up backdating CLAUDE.md documents (a genuinely
    # missed signal, caught up days later, which uses a different flow --
    # handle_entry_price's immediate-entry confirmation) -- that case wants
    # signal_time to reflect the true original dislocation; this case is the
    # normal, expected, strategy-modeled bounce-wait, which the kernel itself
    # never charges against hold time.
    fill_time = datetime.now()
    opened = db.open_position(node, signal_price, fill_time, fill_price, fill_time,
                               shares=filled_shares)
    if not opened:
        return
    drift_pct = (fill_price - signal_price) / signal_price * 100
    db.log_coverage_event("buy_fill_reconciled", _coverage_mode(node.get('account')), ticker=ticker,
                           node_id=node['id'], result="opened",
                           detail=f"shares={filled_shares:g} price={fill_price:.4f} drift={drift_pct:+.2f}%")
    _post_message(f"🤖 {ticker} — auto-detected fill at ${fill_price:.4f}  "
                  f"(drift: {drift_pct:+.2f}%)  {filled_shares:g} shares")
    _reconcile_fill(node, fill_price, filled_shares, is_gap_correction=is_gap_correction)
    if ticker in schwab_safety.AUTOMATION_ENABLED_TICKERS:
        _place_stop_loss_for_position(node, ticker)


_GAP_RESIZE_PAD_PCT = 5.0
_GAP_FILL_POLL_ATTEMPTS = 5
_GAP_FILL_POLL_INTERVAL_SECS = 3


def check_gap_resize():
    """Pre-open overnight-gap check (Part 3, branch B) -- for each still-pending,
    order_placed trailing-buy whose trigger has already cleared overnight (a real
    gap-through, not rare: 19-44% of trading days per the 2026-07-19 policy sim),
    cancels the resting order and replaces it with a plain MARKET order (mirrors
    the backtest kernel's own entry_price=op gap-fill behavior) sized off a live
    quote with a flat 5% pad. If the trigger hasn't cleared, no action -- the
    resting order's original sizing is still a valid bound (running_low is
    non-increasing). Self-contained: polls for the replacement's own fill and
    reconciles it immediately rather than deferring to the next check_auto_fills
    cycle. Persisted per-row via pending_buys.gap_resize_date -- a daemon restart
    inside active_signals._GAP_CHECK_WINDOW resets the caller's in-memory
    once-daily gate, but this function still won't act on the same row twice in
    one day, since the marker survives the restart (fixed 2026-07-27; previously
    relied solely on the caller's in-memory gate and would re-attempt cancel/
    replace on a restarted second invocation)."""
    today = datetime.now().strftime('%Y-%m-%d')
    for pending in db.get_pending_buys():
        if not pending['order_placed']:
            continue
        if pending.get('gap_resize_date') == today:
            continue
        ticker = pending['ticker']
        node = pending['node']
        account = node.get('account')
        if not account:
            # Unlike every other early-exit in this function, a missing account
            # leaves this resting order's overnight gap-resize entirely
            # unattempted with no record and no alert -- found by Opus review,
            # 2026-07-25.
            db.log_coverage_event("gap_resize", "unattributed", ticker=ticker,
                                   node_id=node.get('id'), result="no_account")
            _post_message(f"⚠️ {ticker} gap-check skipped — pending buy has no account on file "
                          f"(resting order's overnight gap wasn't checked)")
            continue
        trail_buy_pct = node.get('trail_buy_pct') or 0.0
        # running_low (added for dry-run-sim tracking) starts equal to signal_price
        # at creation and only moves via intraday polling -- reading it here keeps
        # this in sync with whatever update_dry_run_buys/the real broker have
        # already observed, instead of a hardcoded signal_price that could go
        # stale if this function is ever invoked after some intraday polling did occur.
        running_low = pending['running_low'] or pending['signal_price']
        buy_trigger = running_low * (1 + trail_buy_pct / 100)
        try:
            current_price = schwab_client.get_current_price(ticker)
        except Exception as e:
            db.log_coverage_event("gap_resize", _coverage_mode(account), ticker=ticker,
                                   node_id=node.get('id'), result="price_lookup_failed", detail=str(e))
            _post_message(f"⚠️ {ticker} gap-check price lookup failed: {e}")
            continue
        if current_price < buy_trigger:
            continue

        # From here on we're about to act (replace/place) -- claim the row for
        # today before touching the broker, so a restart mid-attempt can't
        # re-enter and act on it again (see the persisted-guard docstring
        # note above).
        db.mark_gap_resize_attempted(pending['id'], today)

        order_id = pending.get('order_id')
        target_notional = _last_sale_recovery(node)
        padded_price = current_price * (1 + _GAP_RESIZE_PAD_PCT / 100)
        shares = int(target_notional // padded_price)
        _post_message(f"🌅 {ticker} — overnight gap cleared trigger (${current_price:.4f} vs "
                      f"${buy_trigger:.4f}); replacing with a MARKET order for {shares} shares")
        try:
            if order_id:
                # Atomic replace (cancel-old + create-new as a single broker
                # call) instead of a separate cancel_order + place_equity_buy --
                # closes the window where a confirmed cancel could be followed
                # by a failed/blocked new placement, leaving no order resting at
                # all in between (found 2026-07-27, raised directly by the user).
                _, new_order_id = schwab_client.replace_equity_order_with_market(
                    account, ticker, order_id, "BUY", shares, current_price, is_gap_correction=True)
            else:
                _, new_order_id = schwab_client.place_equity_buy(
                    account, ticker, shares, current_price, is_gap_correction=True)
        except schwab_safety.SafetyViolation as e:
            db.log_coverage_event("gap_resize", _coverage_mode(account), ticker=ticker,
                                   node_id=node.get('id'), result="blocked", detail=str(e))
            _post_message(f"🚫 {ticker} gap-correction MARKET order blocked: {e}")
            continue
        except schwab_client.OrderRejected as e:
            db.log_coverage_event("gap_resize", _coverage_mode(account), ticker=ticker,
                                   node_id=node.get('id'), result="rejected", detail=str(e))
            _post_message(f"🚫 {ticker} gap-correction MARKET order was rejected by Schwab: {e}")
            db.clear_pending_buy_by_wl_id(node['id'])
            continue
        except Exception as e:
            db.log_coverage_event("gap_resize", _coverage_mode(account), ticker=ticker,
                                   node_id=node.get('id'), result="replace_failed", detail=str(e))
            _post_message(f"⚠️ {ticker} gap-correction replace failed: {e} — leaving resting order/pending row as-is")
            continue
        db.log_coverage_event("gap_resize", _coverage_mode(account), ticker=ticker,
                               node_id=node.get('id'), result="replaced", detail=f"shares={shares} price={current_price:.4f}")
        db.set_pending_buy_order_id_by_wl_id(node['id'], new_order_id)

        if new_order_id is None:
            # dry_run -- no real fill will ever appear on Schwab's order book
            continue

        for _ in range(_GAP_FILL_POLL_ATTEMPTS):
            fill = schwab_client.get_filled_order(account, ticker, 'BUY', order_id=new_order_id)
            if fill is not None:
                break
            time.sleep(_GAP_FILL_POLL_INTERVAL_SECS)
        else:
            _post_message(f"⚠️ {ticker} gap-correction order placed but fill not confirmed after "
                          f"{_GAP_FILL_POLL_ATTEMPTS * _GAP_FILL_POLL_INTERVAL_SECS}s — "
                          f"will be caught by the next check_auto_fills poll")
            continue

        _reconcile_buy_fill(ticker, fill['price'], fill['quantity'], is_gap_correction=True, wl_id=node['id'])


def drain_fill_queue():
    """Fast-path fill detection (Part 3, branch C) -- pops all pending events off
    schwab_stream's account-activity queue and reconciles each. A no-op if
    schwab_stream was never started or has no events (the slow check_auto_fills
    poll is the always-on fallback, not gated on this).
    Deliberately does NOT trust the stream message's own fill_price/filled_shares
    (found 2026-07-22: an ExecutionActivity message can represent one partial
    execution of a still-filling order -- unverified whether Schwab's
    filledQuantity field is cumulative or per-execution, and multiple partial
    fills can arrive within the same second for a liquid ticker. Locking in
    whichever partial quantity happens to be parsed first would permanently
    under-record real share count, and _reconcile_fill's top-up logic would then
    place a real *second* buy order to "correct" a fill that was never actually
    final -- a genuine double-buy risk, not just a bookkeeping one). The stream
    event is used only as a wake-up signal for *which* ticker to check; the
    actual price/quantity is re-confirmed via the same get_filled_order poll
    check_auto_fills already trusts (status=='FILLED' only, aggregated across
    every executionLeg), giving the order a few seconds to fully settle first
    (automation_principles.md #1 -- reconfirm real state, don't trust a local/
    cached record)."""
    while True:
        try:
            account, ticker, side, _stream_price, _stream_shares, order_id = schwab_stream.FILL_QUEUE.get_nowait()
        except Exception:
            break
        if side != 'BUY':
            continue
        # Resolved by exact order_id match against the real pending_buys row
        # (order_id-exact matching, same precedent as the GDXU stale-fill
        # incident fix), NOT the fuzzy ticker+account db.get_watch_list_node
        # lookup a first version of this gate used -- that lookup's own
        # documented contract is "enrichment/logging, never a gate on a real
        # action" (see the comment a few lines below, at the
        # _reconcile_buy_fill call), so gating the opt-in auto-fill-detection
        # check on it could silently drop the fast path for a genuinely
        # opted-in node whenever the fuzzy match failed to resolve (found by
        # review before landing). Falls back to None (same as before) only
        # when no pending row matches this exact order_id at all.
        # Coerced to int on both sides before comparing -- pending_buys.order_id
        # has INTEGER affinity and is written from schwab-py's extract_order_id
        # (an int), but the stream side is raw JSON (schwab_stream.py); if
        # Schwab ever emits orderId as a numeric string, a bare `==` would
        # silently fail to match and drop the fast path for an opted-in node
        # (fails safe to the slow check_auto_fills poll, but pointless
        # latency -- found by review).
        try:
            _order_id_int = int(order_id) if order_id is not None else None
        except (TypeError, ValueError):
            _order_id_int = None
        _matching_pending = next(
            (p for p in db.get_pending_buys() if p.get('order_id') is not None
             and int(p['order_id']) == _order_id_int), None) if _order_id_int is not None else None
        _node_id = _matching_pending['node']['id'] if _matching_pending else None
        # Same opt-in gate as check_auto_fills (the slow-poll fallback) --
        # without this, the fast websocket path auto-reconciled any real fill
        # regardless of auto_fill_detection_enabled/node_auto_fill_detection_enabled,
        # bypassing the whole point of that toggle (a human must opt a ticker/
        # node in before the daemon auto-records a fill instead of waiting for
        # the manual "Filled" Slack button). Found in the 2026-07-31 audit's
        # still-open list.
        if ticker not in schwab_safety.AUTOMATION_ENABLED_TICKERS:
            db.log_coverage_event("fast_path_fill_reconciliation", _coverage_mode(account), ticker=ticker,
                                   node_id=_node_id, result="outside_automation_scope")
            continue
        if not (schwab_safety.auto_fill_detection_enabled(ticker)
                and schwab_safety.node_auto_fill_detection_enabled(_node_id)):
            db.log_coverage_event("fast_path_fill_reconciliation", _coverage_mode(account), ticker=ticker,
                                   node_id=_node_id, result="auto_fill_detection_disabled")
            continue
        fill = None
        for _ in range(_GAP_FILL_POLL_ATTEMPTS):
            fill = schwab_client.get_filled_order(account, ticker, 'BUY', order_id=order_id)
            if fill is not None:
                break
            time.sleep(_GAP_FILL_POLL_INTERVAL_SECS)
        if fill is None:
            # Order not yet FILLED at the broker -- leave it for the slow
            # check_auto_fills poll rather than acting on an unconfirmed partial.
            db.log_coverage_event("fast_path_fill_reconciliation", _coverage_mode(account), ticker=ticker,
                                   node_id=_node_id, result="stream_event_not_yet_confirmed_filled")
            continue
        db.log_coverage_event("fast_path_fill_reconciliation", _coverage_mode(account), ticker=ticker,
                               node_id=_node_id, result="confirmed_via_poll", detail=f"price={fill['price']:.4f} qty={fill['quantity']:g}")
        # _node_id is only a fuzzy ticker+account hint (get_watch_list_node's
        # documented contract is enrichment/logging, never a gate on a real
        # action) -- passing it as _reconcile_buy_fill's wl_id would let a
        # stale/ambiguous hint block reconciliation of a real, confirmed-FILLED
        # broker fill outright. Kept for the coverage event above only.
        _reconcile_buy_fill(ticker, fill['price'], fill['quantity'])


def check_auto_fills(open_positions):
    """Polls Schwab's order book for pilot-scope tickers with auto-fill-detection
    enabled (schwab_safety.auto_fill_detection_enabled, off by default) and
    auto-records a fill instead of waiting for the human Filled/Exited click.
    Buy side: any pending_buys row with order_placed=True. Sell side: any open
    position with an armed trailing-sell order (trail_state.order_placed) and an
    unresolved exit_pending. Clearing the pending_buys row / exit_pending on a
    hit is itself the dedup marker -- no separate 'already processed' state needed."""
    for pending in db.get_pending_buys():
        ticker = pending['ticker']
        if not pending['order_placed']:
            continue
        if ticker not in schwab_safety.AUTOMATION_ENABLED_TICKERS:
            continue
        node = pending['node']
        if not (schwab_safety.auto_fill_detection_enabled(ticker)
                and schwab_safety.node_auto_fill_detection_enabled(node.get('id'))):
            continue
        account = node.get('account')
        if not account:
            continue
        fill = schwab_client.get_filled_order(account, ticker, 'BUY', order_id=pending.get('order_id'))
        if fill is None:
            continue
        _reconcile_buy_fill(ticker, fill['price'], fill['quantity'], wl_id=node['id'])

    for pos in open_positions:
        ticker = pos['ticker']
        if ticker not in schwab_safety.AUTOMATION_ENABLED_TICKERS:
            continue
        if not (schwab_safety.auto_fill_detection_enabled(ticker)
                and schwab_safety.node_auto_fill_detection_enabled(pos.get('wl_id'))):
            continue
        state = pos.get('trail_state') or {}
        exit_pending = state.get('exit_pending')
        if not (state.get('order_placed') and exit_pending):
            continue
        account = pos.get('account')
        if not account:
            continue
        fill = schwab_client.get_filled_order(account, ticker, 'SELL', order_id=exit_pending.get('order_id'))
        if fill is None:
            continue
        actual_pnl = (fill['price'] - pos['entry_price']) / pos['entry_price'] * 100
        closed = db.close_position(pos['id'], exit_signal_price=exit_pending['current_price'],
                                    exit_price=fill['price'], exit_time=datetime.now(),
                                    exit_reason=exit_pending['reason'])
        if not closed:
            continue
        _post_message(f"🤖 {ticker} — auto-detected exit fill at ${fill['price']:.4f}  (P&L: {actual_pnl:+.2f}%)")


# ---------------------------------------------------------------------------
# Startup report
# ---------------------------------------------------------------------------

def _ticker_block(row):
    """Renders one row from build_reference_table as mrkdwn prose (wraps naturally
    on mobile) instead of a fixed-width table column (unreadable on iPhone).
    Returns a list of blocks (section + optional manual-correction actions)."""
    ticker, version = row['Ticker'], row.get('Version') or ''
    account = 'bro' if (row.get('Account') or '').lower() == 'brokerage' else (row.get('Account') or '')
    account_str = f" — `{account}`" if account else ''
    proximity = row.get('Proximity')

    if row['Next Action'] == 'NO_DATA':
        return [{"type": "section", "text": {"type": "mrkdwn", "text": f"⚫ *{ticker}* `{version}`  NO_DATA"}}]

    phase = row.get('Phase') or ''
    phase_str = f"{phase} " if phase else ''
    now = row['Now']
    trigger = row['Next Trigger $']

    if row['Held']:
        pnl = row.get('PnL %')
        sl = row.get('SL $')
        sl_str = f"  sl `${sl:.2f}`" if sl is not None else "  sl `cancelled (trail order live)`"
        pct_str = lambda v: f"{v:g}%" if v is not None else '?'
        trig_label = row.get('Trigger Label', 'trig')
        pos = row.get('_pos')
        shares_str = f" x `{pos['shares']:g}`" if pos and pos.get('shares') is not None else ''
        entry_str = f"  `${pos['entry_price']:.2f}`{shares_str}" if pos else ''
        armed = bool((pos or {}).get('trail_state', {}).get('trailing'))
        if armed:
            arm_ts_line = ''
        else:
            arm, ts = row.get('Arm%'), row.get('TrailSell%')
            arm_ts_line = f"\narm `{pct_str(arm)}`  ts `{pct_str(ts)}`"
        # No real broker fill/order exists behind this row -- must never read as
        # an actionable real position (same reasoning as the 🧪CANARY tag below,
        # Opus review 2026-07-26 flagged this row was otherwise indistinguishable
        # from a genuine held position).
        sim_tag = ' 🧪DRY-RUN-SIM' if pos and pos.get('is_dry_run_sim') else ''
        text = (
            f"{phase_str}*{ticker}* `{version}`{sim_tag} — {row['Hold']}{account_str}{entry_str}\n"
            f"now `${now:.2f}` {pnl:+.1f}%  {trig_label} `${trigger:.2f}` ({proximity:+.1f}%)\n"
            f"→ _{row['Next Action']}_{sl_str}{arm_ts_line}"
        )
    else:
        overnight = row.get('Overnight %')
        tb, arm, ts = row.get('TrailBuy%'), row.get('Arm%'), row.get('TrailSell%')
        pct_str = lambda v: f"{v:g}%" if v is not None else '?'
        last_sale = row.get('Last Sale $')
        last_sale_str = f"  next buy ~`${last_sale/1000:.0f}k`" if last_sale is not None else ''
        z_trig = row.get('Z Trigger')
        z_trig_str = f"z1 `{z_trig:g}`  " if z_trig is not None else ''
        trig_label = row.get('Trigger Label', 'trig')
        # Not-live rows are visible in the report (2026-07-22 fix) but must
        # never read as an actionable live trigger -- research is the normal
        # state right now (whole watchlist), canary is a synthetic test node
        # not meant to be traded at all (see the "Manually Open" suppression
        # below, automation_principles.md #0/#7).
        mode_tag = ' 🧪CANARY' if (row.get('_node') or {}).get('version') == 'canary' \
            else (' (research)' if row.get('Mode') == 'research' else '')
        text = (
            f"{phase_str}*{ticker}* `{version}`{mode_tag}{account_str}{last_sale_str}\n"
            f"now `${now:.2f}` ({overnight:+.1f}% O/N)  z `{row['Z']:+.2f}`  {trig_label} `${trigger:.2f}` ({proximity:+.1f}%)\n"
            f"→ _{row['Next Action']}_\n"
            f"{z_trig_str}tb `{pct_str(tb)}`  arm `{pct_str(arm)}`  ts `{pct_str(ts)}`"
        )
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    if cfg.INTERACTIVE:
        # 2026-07-22: collapsed from up to 3 separate `actions` blocks per row
        # into 1 (Slack allows up to 5 elements per actions block, we use at
        # most 3) -- with the mode filter fix above making every watchlist row
        # render instead of none, 16 rows x up to 4 blocks each blew past
        # Slack's hard 50-block-per-message limit and the report failed
        # outright (invalid_blocks). This cuts the per-row block count enough
        # to fit the full watchlist in one message again.
        elements = []
        node = row.get('_node')
        if row['Held']:
            pos = row.get('_pos')
            if pos and not pos.get('is_dry_run_sim'):
                value = json.dumps({"position_id": pos['id'], "ticker": ticker, "entry_price": pos['entry_price']})
                elements.append({"type": "button", "text": {"type": "plain_text", "text": f"Manually Close {ticker}"},
                                  "action_id": "manual_close", "value": value})
        # Canary nodes are synthetic test fixtures with deliberately absurd
        # parameters (hair-trigger z-thresholds, unreachable SL) -- never
        # offer a real "Manually Open" button for one (automation_principles.md
        # #0/#7: a new surface, here "research rows are now visible", must not
        # silently inherit an action that was previously unreachable because
        # nothing research-mode ever rendered here before 2026-07-22).
        elif node and node.get('version') != 'canary':
            node_fields = {k: node.get(k) for k in ('id', 'ticker', 'strategy', 'version', 'window',
                                                      'take_profit', 'stop_loss', 'max_hold_hours',
                                                      'trail_sell_pct', 'fixed_sl', 'trail_buy_pct', 'arm_sell_pct',
                                                      'starting_notional', 'account')}
            value = json.dumps({"node": node_fields})
            elements.append({"type": "button", "text": {"type": "plain_text", "text": f"Manually Open {ticker}"},
                              "action_id": "manual_open", "value": value})

        # Per-ticker automation pause/resume -- only shown for tickers actually in
        # the automation pilot scope (see schwab_safety.AUTOMATION_ENABLED_TICKERS),
        # so the other manual-only tickers don't show a button that does nothing.
        if ticker in schwab_safety.AUTOMATION_ENABLED_TICKERS:
            automation_on = schwab_safety.ticker_automation_enabled(ticker)
            elements.append(
                {"type": "button", "text": {"type": "plain_text", "text": f"⏸️ Pause {ticker} Automation"},
                 "style": "danger", "action_id": "pause_ticker_automation", "value": ticker}
                if automation_on else
                {"type": "button", "text": {"type": "plain_text", "text": f"▶️ Resume {ticker} Automation"},
                 "style": "primary", "action_id": "resume_ticker_automation", "value": ticker}
            )

            # Auto-fill-detection toggle -- separate from the placement toggle above and
            # defaults off (see schwab_safety.AUTO_FILL_DETECTION_PATH comment): placement
            # automation is proven via this session's dry-run testing, fill detection isn't
            # exercised against a real fill yet.
            # node-scoped (not ticker-only) -- see schwab_safety.node_auto_fill_detection_enabled's
            # docstring: this was the ticker-only-keying gap the 2026-07-25/26 wl_id refactor
            # missed. Every row here is built from a real watch_list node (build_reference_table),
            # so wl_id is always resolvable; still guarded rather than assumed, since a NULL-wl_id
            # open position (a watch_list row deleted out from under it, e.g. EDC id=15) would
            # never render a row/button here at all and should fail closed, not toggle every node
            # sharing the ticker.
            wl_id = node.get('id') if node else None
            if wl_id is not None:
                fill_detection_on = (schwab_safety.auto_fill_detection_enabled(ticker)
                                      and schwab_safety.node_auto_fill_detection_enabled(wl_id))
                fd_value = json.dumps({"ticker": ticker, "wl_id": wl_id})
                elements.append(
                    {"type": "button", "text": {"type": "plain_text", "text": f"🤖 Disable {ticker} Auto-Fill Detection"},
                     "style": "danger", "action_id": "disable_auto_fill_detection", "value": fd_value}
                    if fill_detection_on else
                    {"type": "button", "text": {"type": "plain_text", "text": f"🤖 Enable {ticker} Auto-Fill Detection"},
                     "action_id": "enable_auto_fill_detection", "value": fd_value}
                )

        if elements:
            blocks.append({"type": "actions", "elements": elements})

    return blocks


def _send_window_alert(label, watchlist):
    """Reuses build_reference_table so this alert shares one source of truth with
    the morning report -- correct per-position trigger (buy/arm/trailing-sell,
    not always the buy-side lower_band). Minimal by design: only tickers within
    5% of their next trigger, rendered as mobile-readable prose, not the full
    watchlist table."""
    ref_rows = build_reference_table(watchlist)
    hot = [r for r in ref_rows if isinstance(r.get('Proximity'), (int, float)) and r['Proximity'] < 5]
    alert_level = "🔶 *HIGH ALERT*" if hot else "✅ algo running, nothing within range"
    header = f"⏱ *Signal window — {label} ET* | {alert_level}"
    if not hot:
        _post_message(header)
        return
    # Same unbounded-block risk as the Morning Report (2026-07-26 fix) -- a
    # broad selloff can push most of a 25+-node watchlist within 5% of trigger
    # at once, so this chunks too instead of assuming "hot rows only" bounds it.
    fixed_blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}, {"type": "divider"}]
    units = [_ticker_block(r) for r in hot]
    _post_chunked(header, fixed_blocks, units)


_REF_TABLE_COLS = [
    'Phase', 'Ticker', 'Hold', 'Next Trigger $', 'Now', 'Proximity', 'Next Action',
    'Version', 'Alpha', 'Z', 'Z Trigger', 'TrailBuy%', 'Arm%', 'TrailSell%', 'Account', 'Last Sale $',
]


def build_reference_table(watchlist):
    """One row per live-mode ticker: buy-trigger info if flat, arm/sell-trigger
    info if held. `Proximity` is signed so negative always means the trigger has
    already been crossed (price fell through a buy/sell-trail trigger, or rose
    through an arm trigger) -- sign convention, not raw distance."""
    # Keyed on wl_id, not ticker -- a ticker-keyed dict would silently mask one
    # node's position/pending row behind another's if 2+ nodes share a ticker
    # (see docs/backlog_cache.md's wl_id refactor entry).
    # Paper positions/pending-buys merged in too (2026-07-28 fix) -- this used
    # to only read open_positions/pending_buys (the real+dry_run tables), so
    # every research-mode node's Phase bubbles rendered flat/all-grey even
    # with a real open paper position (found live: SOXL, 463 shares, entered
    # hours earlier, showed 0/4 bubbles). Real/dry_run rows win on a wl_id
    # collision (shouldn't happen -- a node is either mode='live' with a real
    # position or mode='research' with a paper one, never both -- but real
    # state should never be shadowed by paper state regardless).
    positions = {p['wl_id']: p for p in db.get_open_positions(paper=True)}
    positions.update({p['wl_id']: p for p in db.get_open_positions()})
    pending_buys = {p['node']['id']: p for p in db.get_paper_pending_buys()}
    pending_buys.update({p['node']['id']: p for p in db.get_pending_buys()})
    rows = []
    # 2026-07-22 fix: this used to filter to mode=='live' only -- silently
    # correct while every node really was live, but once the whole watchlist
    # moved to mode='research' (2026-07-20 v5 promotion) it made the entire
    # Morning Report render empty (structure/header only, zero candidate rows)
    # with no error or indication anything was wrong. The report's whole
    # purpose is visibility into watchlist/canary state regardless of mode --
    # show everything, mark mode on each row instead of hiding non-live ones.
    for node in watchlist:
        ticker = node['ticker']
        pos = positions.get(node['id'])
        sig = compute.compute_buy_signal(node)
        account = node.get('account') or ''
        alpha = node.get('alpha')
        last_sale = _last_sale_recovery(node)
        phase = _phase_emoji(pos, pending_buys.get(node['id']))

        if sig is None:
            rows.append({
                'Ticker': ticker, 'Hold': '', 'Next Action': 'NO_DATA', 'Next Trigger $': None,
                'Now': None, 'Proximity': None, 'Version': node.get('version'), 'Alpha': alpha,
                'Z': None, 'Z Trigger': node.get('z_score_threshold'),
                'TrailBuy%': node.get('trail_buy_pct'), 'Arm%': node.get('arm_sell_pct'),
                'TrailSell%': node.get('trail_sell_pct'), 'Account': account, 'Last Sale $': last_sale,
                'Strategy': node['strategy'], 'Held': False, 'Phase': phase, 'Mode': node.get('mode'),
                '_node': node, '_pos': None, '_sig': None,
            })
            continue

        now_price = sig['current_price']
        schwab_sl_pct = node['stop_loss']

        if pos is None:
            pending = pending_buys.get(node['id'])
            trail_buy_pct = node.get('trail_buy_pct')
            if pending is not None:
                # z already crossed, trailing-buy order active -- the bounce-above-
                # running-low trigger is the number that actually matters now, not
                # the (already-cleared, often much farther away) initial z trigger.
                _, tb_trigger = _trailing_buy_status(pending)
                trigger = tb_trigger if tb_trigger is not None else sig['lower_band']
                next_action = 'Waiting Trail-Buy Bounce'
                trigger_label = 'tb-bounce'
            else:
                trigger = sig['lower_band']
                next_action = 'Waiting Buy Trigger'
                trigger_label = 'z-cross'
            rows.append({
                'Ticker': ticker, 'Hold': '',
                'Next Action': next_action, 'Trigger Label': trigger_label,
                'Next Trigger $': trigger, 'Now': now_price,
                'Proximity': (now_price - trigger) / trigger * 100,
                'Version': node.get('version'), 'Alpha': alpha, 'Z': sig['z_score'],
                'Z Trigger': node.get('z_score_threshold'),
                'TrailBuy%': trail_buy_pct, 'Arm%': node.get('arm_sell_pct'),
                'TrailSell%': node.get('trail_sell_pct'), 'Account': account, 'Last Sale $': last_sale,
                'Strategy': node['strategy'], 'Held': False, 'Phase': phase, 'Mode': node.get('mode'),
                'SL $': trigger * (1 - schwab_sl_pct / 100), 'Arm $': trigger * (1 + db._tp_or_arm_pct(node) / 100),
                'Overnight %': (now_price - sig['prev_close']) / sig['prev_close'] * 100,
                'Prev Close': sig['prev_close'], 'Data Date': sig['last_daily_bar'],
                '_node': node, '_pos': None, '_sig': sig,
            })
        else:
            df_hourly_p, _ = compute._load_cache(ticker)
            signal_time = datetime.fromisoformat(pos['signal_time'])
            hours_held = compute._bars_held(df_hourly_p, signal_time)
            hold = f"{hours_held:.0f}h/{pos['max_hold_hours']}h"
            trail_state = pos.get('trail_state') or {}
            arm_pct = pos.get('arm_sell_pct')
            trail_sell_pct = pos.get('trail_sell_pct')
            bsp = pos.get('broker_stop_price')
            # Broker only allows one resting sell-all order per position -- once the
            # trailing-sell order is actually placed (order_placed=True), it replaces
            # the catastrophic stop, so the entry-based SL price is no longer live.
            if trail_state.get('order_placed'):
                sl_price = None
            elif bsp:
                sl_price = bsp
            else:
                sl_price = pos['entry_price'] * (1 - pos['stop_loss'] / 100)

            if trail_state.get('trailing'):
                peak = trail_state.get('peak', pos['entry_price'])
                trail_pct = (trail_sell_pct or 3.0) / 100.0
                trigger = peak * (1 - trail_pct)
                if trail_state.get('order_placed'):
                    next_action = f"Waiting Sell {trail_sell_pct:g}% Fill" if trail_sell_pct else 'Waiting Sell Fill'
                else:
                    next_action = f"Pending Sell {trail_sell_pct:g}%" if trail_sell_pct else 'Pending Sell'
                proximity = (now_price - trigger) / trigger * 100
                trigger_label = 'trail-sell'
            else:
                # Two triggers are simultaneously live here: SL protects right now,
                # Arm is the next threshold that swaps SL for the trailing sell.
                trigger = pos['entry_price'] * (1 + db._tp_or_arm_pct(pos) / 100.0)
                next_action = f"Arm {arm_pct:g}%" if arm_pct else 'Arm'
                proximity = (trigger - now_price) / trigger * 100
                trigger_label = 'arm'

            rows.append({
                'Ticker': ticker, 'Hold': hold, 'Next Action': next_action, 'Trigger Label': trigger_label,
                'Next Trigger $': trigger, 'Now': now_price, 'Proximity': proximity,
                'Version': pos.get('version'), 'Alpha': alpha, 'Z': sig['z_score'],
                'Z Trigger': node.get('z_score_threshold'),
                'TrailBuy%': pos.get('trail_buy_pct'), 'Arm%': arm_pct,
                'TrailSell%': trail_sell_pct, 'Account': account, 'Last Sale $': last_sale,
                'Strategy': pos.get('strategy', node['strategy']), 'Held': True, 'Phase': phase,
                'Mode': node.get('mode'),
                'SL $': sl_price, 'PnL %': (now_price - pos['entry_price']) / pos['entry_price'] * 100,
                '_node': node, '_pos': pos, '_sig': sig,
            })
    return rows


def format_reference_table(rows):
    def fmt(col, v):
        if v is None:
            return ''
        if col == 'Next Trigger $':
            return f"${v:.2f}"
        if col == 'Now':
            return f"${v:.2f}"
        if col == 'Proximity':
            return f"{v:+.1f}%"
        if col == 'Alpha':
            return f"{v:+.0f}"
        if col == 'Z':
            return f"{v:+.2f}"
        if col == 'Z Trigger':
            return f"{v:g}"
        if col in ('TrailBuy%', 'Arm%', 'TrailSell%'):
            return f"{v:g}"
        if col == 'Last Sale $':
            return f"${v/1000:.0f}k"
        return str(v)

    cells = [[fmt(c, r.get(c)) for c in _REF_TABLE_COLS] for r in rows]
    widths = [max(len(col), *(len(row[i]) for row in cells)) if cells else len(col)
              for i, col in enumerate(_REF_TABLE_COLS)]
    lines = [' '.join(col.ljust(widths[i]) for i, col in enumerate(_REF_TABLE_COLS))]
    for row in cells:
        lines.append(' '.join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return '\n'.join(lines)


_STRATEGY_LABELS = {
    'ZScoreBreakout':             ('BUY (bar-close)', 'At signal close: edit staged limit → market and submit'),
    'TrendFilteredZScore':        ('BUY (bar-close)', 'At signal close: edit staged limit → market and submit'),
    'TrailingExitZScoreBreakout': ('BUY (bar-close, trailing exit)', 'At signal close: edit staged limit → market and submit'),
    'LimitOrderZScoreBreakout':   ('BUY (limit)', 'Pre-market: stage limit order at trigger price (absurdly low); confirm fill intrabar'),
    'TrailingBuyZScoreBreakout':  ('BUY (bar-close, trailing entry)', 'At signal close: place a trailing buy order at trail_buy_pct% — broker handles fill timing'),
    'TrailingBothZScoreBreakout': ('BUY (bar-close, trailing entry+exit)', 'At signal close: place a trailing buy order at trail_buy_pct% — broker handles fill timing'),
}


def send_coverage_report(check_date=None):
    """Slack-callable version of scripts/coverage_check.py -- piece #7 of the
    2026-07-24 coverage-system reframe. Runs the same daily expected-vs-actual
    check live (not just reading whatever a prior manual/cron run of
    coverage_check.py already recorded) so a request always reflects current
    state, then posts a compact mobile-readable summary: every still-
    unexplained deviation first (the actionable fact per automation_
    principles.md #16's "no unexplained failure" contract), then a per-scenario
    pass/fail line. Piggybacks on the existing reference-report delivery
    (_post_message), not a separate mechanism.

    Renders status from run_check's own live return value, not a re-query of
    coverage_deviations -- a scenario the same run just found met must not be
    reported as a deviation (found by Opus review, 2026-07-25: re-querying
    stale rows made the report contradict the check it had just run). Also
    refuses to run on a weekend, since a trade_lifecycle expectation like
    "no closed trade today" is trivially, permanently false on a day the
    market never opened -- the button being one tap away (unlike the CLI
    tool, normally run deliberately at end of day) turned that into a real
    risk of manufacturing permanent false-positive deviation rows (confirmed
    live: this bug produced exactly that on a Saturday before this fix)."""
    check_date = check_date or datetime.now().strftime('%Y-%m-%d')
    # Widened from weekend-only to the full NYSE calendar 2026-07-30 (Opus
    # review) -- a weekday market holiday has the identical "trivially,
    # permanently false" failure mode this guard exists to prevent.
    if not _coverage_is_trading_day(check_date):
        return _post_message(f"Coverage Report — {check_date} is not a trading day (weekend or market holiday), no trading day to check.")

    try:
        db.ensure_tables()
        results = _coverage_run_check(check_date)
    except Exception as e:
        return _post_message(f"⚠️ Coverage Report failed to run: {e}")

    # scenario_key alone is not a unique key -- two active scenario_expectations
    # rows can share one scenario_key when disambiguated by node_id/mode (e.g.
    # the same designed scenario run against two different nodes on purpose).
    # Keying by scenario_key alone would let explaining one row's deviation
    # silently mask the other's (found by Opus review, 2026-07-25).
    def _key(row_or_result):
        return (row_or_result['scenario_key'], row_or_result.get('ticker') or '',
                row_or_result.get('node_id'), row_or_result.get('mode') or '')

    reasons = {_key(d): d['reason'] for d in db.get_deviations(check_date=check_date)}

    lines = [f"*Coverage Report — {check_date}*"]
    # ticket_eligible defaults True (absent for 'met'/'skipped' rows, and for
    # every 'deviated' row except the 'informational'-frequency ones -- see
    # scripts/coverage_check.py::run_check, 2026-07-30) -- an informational
    # miss never records a coverage_deviations row, so it must never render
    # as UNEXPLAINED here either, or the report would contradict its own
    # no-ticket-minted behavior.
    unexplained = [r for r in results if r['status'] == 'deviated'
                   and r.get('ticket_eligible', True) and not reasons.get(_key(r))]
    if unexplained:
        lines.append(f":red_circle: {len(unexplained)} UNEXPLAINED deviation(s):")
        for r in unexplained:
            lines.append(f"  • {r['scenario_key']} ({r['ticker'] or 'n/a'}): {r['summary']}")
    else:
        lines.append(":white_check_mark: No unexplained deviations.")

    lines.append("")
    for r in results:
        if r['status'] == 'met':
            status = "✓"
        elif r['status'] == 'skipped':
            status = "?  not checked"
        elif not r.get('ticket_eligible', True):
            status = "✗ (informational, no ticket)"
        elif reasons.get(_key(r)):
            status = "✗ (explained)"
        else:
            status = "✗ UNEXPLAINED"
        lines.append(f"{status}  {r['scenario_key']}  ({r['ticker'] or ''})")

    return _post_message("\n".join(lines))


def _next_trading_day(after_date):
    """Small lookahead (10 calendar days is comfortably more than the longest
    real holiday gap) rather than importing active_signals'/schwab_safety's
    own _NYSE_CAL module-level instances, which would be a circular import
    (active_signals imports this module)."""
    import pandas_market_calendars as mcal
    from datetime import timedelta
    cal = mcal.get_calendar('NYSE')
    start = datetime.strptime(after_date, '%Y-%m-%d') + timedelta(days=1)
    sched = cal.schedule(start_date=start.strftime('%Y-%m-%d'),
                          end_date=(start + timedelta(days=10)).strftime('%Y-%m-%d'))
    return sched.index[0].strftime('%Y-%m-%d')


def _position_trigger_summary(pos):
    """Plain-text description of what would close this position next, from
    its own real config columns -- not a prediction of *whether* it fires,
    just what to watch for. Assumes a long entry (every live strategy here
    buys the dip), matching strategies.py's own SL/arm/trail direction."""
    entry = pos['entry_price']
    parts = [f"entry ${entry:.2f} ({pos['entry_time']})"]
    if pos.get('fixed_sl'):
        sl_price = entry * (1 - pos['fixed_sl'] / 100)
        parts.append(f"SL @ ${sl_price:.2f} ({pos['fixed_sl']}%)")
    trail_state = pos.get('trail_state') or {}
    if trail_state.get('trailing'):
        peak = trail_state.get('peak')
        trail_pct = pos.get('trail_sell_pct')
        if peak and trail_pct:
            trail_price = peak * (1 - trail_pct / 100)
            parts.append(f"ARMED, trailing {trail_pct}% off peak ${peak:.2f} -> stop ${trail_price:.2f}")
        else:
            parts.append("ARMED, trailing")
    elif pos.get('arm_sell_pct'):
        arm_price = entry * (1 + pos['arm_sell_pct'] / 100)
        parts.append(f"arms @ ${arm_price:.2f} ({pos['arm_sell_pct']}%) then trails {pos.get('trail_sell_pct') or '?'}%")
    if pos.get('max_hold_hours'):
        parts.append(f"forced TIME exit after {pos['max_hold_hours']}h from entry")
    return " | ".join(parts)


def build_tomorrow_plan(next_date=None):
    """Writes signals_db.daily_plan rows for the next trading day and returns
    the formatted text -- the 'reset the whole thing for the next day' half
    of the nightly cycle (2026-08-01, user's explicit design). Three
    categories, matching how the user actually reviews the account:
      - canary: a same-day copy of the static scenario_expectations rows
        (deterministic by design, doesn't depend on today's activity).
      - live: real (is_dry_run_sim=0) open positions carrying into tomorrow,
        with their real SL/arm/trail/TIME triggers.
      - paper: paper_positions carrying into tomorrow, same trigger shape.
    A position that hasn't opened yet has no plan row -- this only plans
    around what's already on the books, not predicted new entries (mean
    reversion signals aren't predictable a day ahead)."""
    check_date = datetime.now().strftime('%Y-%m-%d')
    next_date = next_date or _next_trading_day(check_date)
    db.clear_daily_plan(next_date)

    lines = [f"*Tomorrow's Plan — {next_date}*"]

    canary_scenarios = [s for s in db.get_scenario_expectations(active_only=True)
                         if (s['scenario_key'] or '').startswith('canary_')]
    lines.append(f"\n_Canary_ ({len(canary_scenarios)} scenarios):")
    for s in canary_scenarios:
        db.add_daily_plan_row(next_date, 'canary', s['expected_outcome'],
                               ticker=s['ticker'], node_id=s.get('node_id'))
        lines.append(f"  • {s['ticker']}: {s['expected_outcome']}")

    for category, paper in (('live', False), ('paper', True)):
        positions = [p for p in db.get_open_positions(paper=paper) if not p.get('is_dry_run_sim')]
        lines.append(f"\n_{category.capitalize()}_ ({len(positions)} open position(s) carrying in):")
        if not positions:
            lines.append("  (none)")
        for p in positions:
            summary = _position_trigger_summary(p)
            db.add_daily_plan_row(next_date, category, summary,
                                   ticker=p['ticker'], node_id=p.get('wl_id'))
            lines.append(f"  • {p['ticker']}: {summary}")

    return "\n".join(lines)


def build_eod_scenario_review(check_date=None):
    """The 'review what happened today, explain it, across live/canary/paper'
    half of the nightly cycle (2026-08-01, user's explicit design, after 5
    prior sessions of this not sticking as a repeatable habit -- codified
    here as real code + a CLAUDE.md session command, not just conversation).

    Canary reuses coverage_check.py's existing scenario_expectations-based
    check (already the right shape). Live/paper have no per-ticker designed
    expectation the way canary does -- a real/paper position's entry depends
    on today's actual z-score crossing, not a predictable schedule -- so
    these sections report real activity (opened/closed today, still open)
    rather than expected-vs-actual, diffed against yesterday's daily_plan
    row for that ticker when one exists (so a position planned to carry
    overnight that instead closed, or vice versa, is visible)."""
    check_date = check_date or datetime.now().strftime('%Y-%m-%d')
    if not _coverage_is_trading_day(check_date):
        return _post_message(f"EOD Scenario Review — {check_date} is not a trading day, nothing to review.")

    lines = [f"*EOD Scenario Review — {check_date}*"]

    # Readiness headline (2026-08-01, user's actual question: "how close are
    # we" to trading material money) -- a single go/no-go-style number up
    # top, detail below. Computed live from scripts/coverage_registry.py
    # (already tracks real coverage_events/coverage_deviations/fake_broker
    # proof per branch, no separate tracking needed) so "how close" is never
    # staler than one trading day, and every day's post to slack_message_log
    # doubles as a durable trend history without a new table.
    # 2026-08-01 Opus review finding: this list is unbounded (grows with
    # REGISTRY) and used to render immediately below the headline, ahead of
    # the live/paper activity and tomorrow's plan -- against real data the
    # whole message ran ~7KB/69 lines, and if Slack ever truncates a long
    # single-text post (as it did to the Morning Report via a hard block
    # limit, 2026-07-23), what gets cut is the tail: the only actionable
    # content. Capped here and the detailed listing moved below the
    # canary/live/paper/plan sections so a truncation risks losing the least
    # essential part first.
    _READINESS_DETAIL_CAP = 10
    untested = []
    try:
        from scripts.coverage_registry import REGISTRY, compute_status
        counts = {}
        for r in REGISTRY:
            status, detail = compute_status(r)
            counts[status] = counts.get(status, 0) + 1
            if status in ('not-instrumented', 'wired-never-fired', 'deviation-unexplained', 'live-attempt-failed'):
                untested.append((r['id'], status, detail))
        verified = counts.get('verified-live', 0)
        total = len(REGISTRY)
        pct = 100 * verified / total if total else 0
        lines.append(f":large_yellow_circle: *Readiness: {verified}/{total} ({pct:.0f}%) critical "
                      f"code paths verified-live.* {len(untested)} still need real proof before scaling up.")
        lines.append(f"_Code-path coverage_: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    except Exception as e:
        lines.append(f"\n_Code-path coverage_: ⚠️ failed to compute: {e}")

    # Keyed by (category, ticker), not ticker alone -- 2026-08-01 Opus review
    # finding: a ticker-only key collapses canary/live/paper plan rows for the
    # same ticker (e.g. FAZ is both a canary scenario ticker and a real open
    # live position), so one category's plan row silently shadows another's.
    # Moved above the canary block (2nd review finding: canary daily_plan rows
    # were written every day but never read back -- exactly what let the
    # JNUG staleness bug from earlier tonight go unnoticed) so the canary
    # section below can use it too.
    prior_plan = {(row['category'], row['ticker']): row for row in db.get_daily_plan(check_date)}

    lines.append("\n_Canary_ (see Coverage Report for full detail):")
    try:
        all_results = _coverage_run_check(check_date)
        # 2026-08-01 2nd Opus review finding: this section used to report
        # EVERY daily/informational scenario (including non-canary control
        # scenarios like reconciliation_mismatch), while build_tomorrow_plan's
        # own "_Canary_" count below filters to scenario_key.startswith
        # ('canary_') -- two sections under the same heading disagreeing on
        # scope with no explanation. Split explicitly: canary_results feeds
        # the count that matches the plan's scope; any non-canary control
        # scenario still gets its own line so nothing silently drops from
        # visibility, just not folded into the canary total.
        canary_results = [r for r in all_results if (r['scenario_key'] or '').startswith('canary_')]
        other_results = [r for r in all_results if not (r['scenario_key'] or '').startswith('canary_')]
        met = sum(1 for r in canary_results if r['status'] == 'met')
        # 2026-08-01 Opus review finding: 'informational'-tier misses are
        # never ticket_eligible, so the old met+deviated split silently
        # excluded them from both counts. 2nd-review finding: a snoozed
        # scenario (status='skipped') has the same problem -- also counted
        # explicitly now so the four buckets always sum to the total.
        deviated = [r for r in canary_results if r['status'] == 'deviated' and r.get('ticket_eligible', True)]
        informational_misses = [r for r in canary_results
                                 if r['status'] == 'deviated' and not r.get('ticket_eligible', True)]
        skipped = [r for r in canary_results if r['status'] == 'skipped']
        lines.append(f"  {met} met / {len(deviated)} deviation(s) / {len(informational_misses)} informational / "
                      f"{len(skipped)} snoozed of {len(canary_results)}")
        for r in deviated:
            lines.append(f"    ✗ {r['scenario_key']} ({r['ticker'] or 'n/a'}): {r['summary']}")
        for r in other_results:
            status_glyph = "✓" if r['status'] == 'met' else ("~" if r['status'] == 'skipped' else "✗")
            lines.append(f"  {status_glyph} [control] {r['scenario_key']}: {r.get('summary', r['status'])}")

        # 2026-08-01 2nd Opus review finding: actually read back today's
        # canary daily_plan rows (written this morning/last EOD run) and flag
        # when the frozen plan text no longer matches the LIVE
        # scenario_expectations text for that same ticker -- catches a config/
        # expectation change that happened after the plan was built (exactly
        # what let JNUG's stale "E mirrored" text sit undetected in the
        # 2026-08-03 plan earlier tonight).
        live_expected = {(s['scenario_key'], s['ticker']): s['expected_outcome']
                          for s in db.get_scenario_expectations(active_only=True)}
        stale_plans = []
        for r in canary_results:
            plan_row = prior_plan.get(('canary', r['ticker']))
            if plan_row is None:
                continue
            live_text = live_expected.get((r['scenario_key'], r['ticker']))
            if live_text is not None and plan_row['expected_outcome'] != live_text:
                stale_plans.append(r['ticker'])
        if stale_plans:
            lines.append(f"  ⚠️ {len(stale_plans)} canary plan row(s) stale vs. current "
                          f"scenario_expectations (config changed since the plan was built): "
                          f"{', '.join(stale_plans)}")
    except Exception as e:
        lines.append(f"  ⚠️ canary check failed to run: {e}")

    try:
        for category, paper in (('live', False), ('paper', True)):
            closed = [t for t in db.get_trades_closed_on_date(check_date, paper=paper) if not t.get('is_dry_run_sim')]
            # 2026-08-01 2nd Opus review finding: unlike its two neighbors
            # (closed/still_open), this wasn't is_dry_run_sim-filtered -- a
            # dry-run-sim entry today could mask a real carried-in position's
            # unplanned close as a routine "new entry today", hiding exactly
            # the anomalous case that annotation exists to surface.
            opened_today = {t['ticker'] for t in db.get_trades_opened_on_date(check_date, paper=paper)
                             if not t.get('is_dry_run_sim')}
            still_open = [p for p in db.get_open_positions(paper=paper)
                          if not p.get('is_dry_run_sim') and p['ticker'] not in {t['ticker'] for t in closed}]
            lines.append(f"\n_{category.capitalize()}_:")
            if not closed and not still_open:
                lines.append("  (no activity today)")
            for t in closed:
                planned = prior_plan.get((category, t['ticker']))
                if planned:
                    note = " (per plan)"
                elif t['ticker'] in opened_today:
                    note = " (no plan row -- new entry today)"
                else:
                    # 2026-08-01 Opus review finding (PLAUSIBLE): this is the
                    # most anomalous case -- a position that carried in from a
                    # prior day, was never on a plan, and closed today -- and
                    # it used to render with no annotation at all, identical
                    # to a routine planned close.
                    note = " (no plan row -- carried in unplanned)"
                pnl = t.get('pnl_pct')
                pnl_str = f"{pnl:.2f}%" if pnl is not None else "n/a"
                lines.append(f"  ✓ {t['ticker']} closed {t['exit_reason']} pnl={pnl_str}{note}")
            for p in still_open:
                planned = prior_plan.get((category, p['ticker']))
                note = " (per plan, carrying overnight)" if planned else " (no plan row for today)"
                lines.append(f"  ~ {p['ticker']} still open{note}: {_position_trigger_summary(p)}")
    except Exception as e:
        lines.append(f"\n⚠️ live/paper activity section failed: {e}")

    try:
        # 2026-08-01 Opus review finding: the review is FOR check_date, but the
        # plan being built is for the NEXT trading day -- the old call passed
        # check_date straight through as next_date, so the plan overwrote
        # itself under today's date every single day and the whole
        # plan-vs-actual diff (tomorrow's review reading today's plan) never
        # had anything to find. _next_trading_day was dead code on this path.
        lines.append("\n" + build_tomorrow_plan(_next_trading_day(check_date)))
    except Exception as e:
        lines.append(f"\n⚠️ tomorrow's plan failed to build: {e}")

    if untested:
        shown = untested[:_READINESS_DETAIL_CAP]
        lines.append(f"\n_{len(untested)} blocking readiness (top {len(shown)}, "
                      f"see `scripts/coverage_registry.py` for the rest):_")
        for rid, status, detail in shown:
            lines.append(f"  ✗ [{status}] {rid}: {detail}")

    return _post_message("\n".join(lines))


def build_phased_monitors_report(check_date):
    """End-of-day plain-text report for the two monitor-only, detection-first
    checks built 2026-07-29 -- schwab_safety._log_pre_action_state_verification
    and schwab_safety.record_node_streak (the node-level circuit breaker).
    Both are deliberately pure logging/alerting with their tolerance/blocking
    policy explicitly deferred until real data accumulates -- this is that
    data. check_date is a 'YYYY-MM-DD' string, compared against
    date(ts, 'localtime') (same convention as coverage_check.py's
    _check_coverage_event, avoiding the UTC-vs-ET offset bug class).

    Log-only by design (2026-07-30 user call): run_loop prints the returned
    string to stdout at the daily EOD slot -- captured in
    logs/active_signals.log the same way every other daemon print already is
    -- rather than posting to Slack, since this is meant as an after-the-fact
    review artifact, not a daily notification."""
    lines = [f"=== Phased monitors report: {check_date} ==="]

    with db._conn() as c:
        pav_rows = [dict(r) for r in c.execute(
            "SELECT * FROM coverage_events WHERE scenario_key = 'pre_action_state_verification' "
            "AND date(ts, 'localtime') = ? ORDER BY ts", (check_date,)
        ).fetchall()]
    lines.append("\n-- pre_action_state_verification --")
    if not pav_rows:
        lines.append("No events (no real BUY/SELL was considered).")
    else:
        counts = {}
        for r in pav_rows:
            counts[r['result']] = counts.get(r['result'], 0) + 1
        lines.append(f"{len(pav_rows)} total: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        for r in pav_rows:
            if r['result'] != 'match':
                lines.append(f"  [{r['ts']}] {r['ticker']} node={r['node_id']} result={r['result']} -- {r['detail']}")

    with db._conn() as c:
        trip_rows = [dict(r) for r in c.execute(
            "SELECT * FROM coverage_events WHERE scenario_key = 'node_circuit_breaker_tripped' "
            "AND date(ts, 'localtime') = ? ORDER BY ts", (check_date,)
        ).fetchall()]
    lines.append("\n-- node_circuit_breaker_tripped --")
    if not trip_rows:
        lines.append("No trips.")
    else:
        for r in trip_rows:
            lines.append(f"  [{r['ts']}] {r['ticker']} node={r['node_id']} mode={r['mode']} -- {r['detail']}")

    lines.append("\n-- current live streak state (not date-scoped) --")
    if not schwab_safety.NODE_BREAKER_PATH.exists():
        lines.append("No breaker state file yet.")
    else:
        try:
            state = json.loads(schwab_safety.NODE_BREAKER_PATH.read_text())
            if not isinstance(state, dict):
                raise ValueError(f"expected a dict, got {type(state).__name__}")
        except (json.JSONDecodeError, OSError, ValueError) as e:
            state = None
            lines.append(f"Could not read breaker state file: {e}")
        if state is not None:
            any_nonzero = False
            for node_id, node_state in state.items():
                node = db.get_watch_list_node_by_id(int(node_id))
                label = f"{node['ticker']} ({node.get('account')})" if node else f"node id={node_id} (not found)"
                for kind in ("order_failures", "reconciliation_mismatches"):
                    streak = node_state.get(f"{kind}_streak", 0)
                    if streak:
                        any_nonzero = True
                        flag = " [TRIPPED]" if node_state.get(f"{kind}_tripped") else ""
                        lines.append(f"  {label}: {kind} streak={streak}/{schwab_safety.NODE_BREAKER_THRESHOLD}{flag}")
            if not any_nonzero:
                lines.append("Every tracked node is at a clean 0 streak on both counters.")

    return "\n".join(lines)


def send_reference_report(watchlist):
    """One source of truth (build_reference_table) rendered as mobile-readable
    prose per ticker -- flat and held both shown with their real next trigger,
    grouped: held positions first, then buy candidates sorted by proximity."""
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    rows = build_reference_table(watchlist)

    def sort_key(r):
        p = r.get('Proximity')
        return p if isinstance(p, (int, float)) else float('inf')

    held_rows = sorted([r for r in rows if r['Held']], key=sort_key)
    flat_rows = sorted([r for r in rows if not r['Held']], key=sort_key)

    fixed_blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Morning Report — {now_str}"}},
    ]
    stopped = schwab_safety.kill_switch_engaged()
    fixed_blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"{'🛑 Automated engine STOPPED' if stopped else '▶️ Automated engine running'}"}]})
    if cfg.INTERACTIVE:
        fixed_blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "▶️ Start Engine"}, "style": "primary",
             "action_id": "start_engine"} if stopped else
            {"type": "button", "text": {"type": "plain_text", "text": "🛑 Stop Engine"}, "style": "danger",
             "action_id": "stop_engine"},
        ]})
    if cfg.INTERACTIVE:
        fixed_blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "🔄 Resend Report"}, "action_id": "resend_ref_table"},
            {"type": "button", "text": {"type": "plain_text", "text": "🧭 Coverage Report"}, "action_id": "send_coverage_report"},
        ]})

    # Account/mode/ticker reference -- one line per (account, mode-category) so
    # at a glance it's clear which accounts have real money on the line vs.
    # dry_run/research-only, before scrolling into the per-ticker detail below.
    fixed_blocks.append({"type": "divider"})
    groups: dict = {}
    for node in watchlist:
        account = node.get('account') or 'unmapped'
        category = 'RESEARCH' if node.get('mode') != 'live' else mode_tag(account)
        groups.setdefault((account, category), []).append(node['ticker'])
    summary_lines = [
        f"*{account}* — {category}: {', '.join(sorted(set(tickers)))}"
        for (account, category), tickers in sorted(groups.items())
    ]
    fixed_blocks.append({"type": "section", "text": {"type": "mrkdwn",
        "text": "\n".join(summary_lines) if summary_lines else "No watchlist nodes."}})

    # Each row's own blocks (section + optional actions) are one atomic unit --
    # 25 nodes and growing already broke a fixed per-row block-count budget
    # twice (2026-07-22, 2026-07-29), so this report chunks across multiple
    # Slack messages (threaded under the first) instead of trying to keep
    # shrinking the per-row block count to fit one message forever.
    units = []
    if held_rows:
        units.append([{"type": "header", "text": {"type": "plain_text", "text": "Open Positions"}}])
        for r in held_rows:
            units.append(_ticker_block(r))
    else:
        units.append([{"type": "context", "elements": [{"type": "mrkdwn", "text": "No open positions."}]}])

    units.append([{"type": "divider"}])
    units.append([{"type": "header", "text": {"type": "plain_text", "text": "Buy Candidates"}}])
    for r in flat_rows:
        units.append(_ticker_block(r))
        proximity = r.get('Proximity')
        if isinstance(proximity, (int, float)) and proximity < 5:
            chart = _chart_buy(r['_node'], r['_sig'])
            if chart:
                _upload_chart(chart, f"{r['Ticker']}_morning.png", f"{r['Ticker']} `{r['Version']}`  z={r['Z']:+.2f}")

    # Console output
    def _tag(r):
        # Version alone doesn't distinguish live from research -- two nodes
        # for the same ticker can share one version (e.g. DPST's deliberate
        # live+research pairing, both version='v5') and render as visually
        # identical lines with no way to tell which one is real money. Unlike
        # the Slack block builder (_ticker_block), this console print
        # previously showed Version only -- found live 2026-07-30 (user
        # expected 4 identifiable live nodes, only 3 were visually
        # distinguishable from their research siblings).
        extra = [x for x in (r.get('Mode'), r.get('Account')) if x]
        return f"{r['Version']} ({'/'.join(extra)})" if extra else (r['Version'] or '')

    print(f"Morning Report — {now_str}")
    if held_rows:
        print("  Open positions:")
        for r in held_rows:
            print(f"    {r['Ticker']:<6} {_tag(r)}  hold={r['Hold']}  now=${r['Now']:.2f}  {r['Next Action']}")
    for r in flat_rows:
        if r['Next Action'] == 'NO_DATA':
            print(f"  {r['Ticker']:<6} {_tag(r)}  NO_DATA  [{r['Strategy']}]")
        else:
            emoji = _proximity_emoji(r['Proximity'])
            print(f"  {emoji} {r['Ticker']:<6} {_tag(r)}  now=${r['Now']:>7.2f}  trigger=${r['Next Trigger $']:>7.2f}  ({r['Proximity']:+.1f}%)  z={r['Z']:>+5.2f}  [{r['Strategy']}]")

    channel, ts = _post_chunked(f"Morning Report — {now_str}", fixed_blocks, units)
    # "live" unconditionally, not _coverage_mode(account) -- this report isn't
    # scoped to any one account's dry_run flag, and _coverage_mode(None) always
    # falls back to "dry_run", which would make this scenario permanently
    # unable to render as verified-live even after a real successful post
    # (found by Opus review, 2026-07-27).
    db.log_coverage_event("morning_report_delivery", "live", result="sent" if (channel and ts) else "no_delivery_confirmation",
                           detail=f"held={len(held_rows)} candidates={len(flat_rows)}")
    return channel, ts
