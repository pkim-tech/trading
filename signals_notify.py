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
import paper_trading
import schwab_safety
import schwab_client
import schwab_stream
from signals_charts import _chart_buy, _chart_sell, _upload_chart
from signals_blocks import (
    _post_message, _post_chunked, _build_buy_blocks, _build_sell_blocks,
    # Relocated to signals_blocks 2026-08-14 (it was the odd block-builder
    # still living here while every sibling builder lived there). Re-imported
    # under its original name so existing call sites in this module,
    # active_signals.py and the tests keep working unchanged -- the same
    # re-export-for-backward-compat convention active_signals.py already uses.
    # NOTE: _ticker_block was NOT relocated (2026-08-15 merge) -- it carries
    # Tranche 3's origin-column fix for bugs #54/#63-64 and stays defined
    # directly below in this module until that fix is ported over properly;
    # see docs/backlog_cache.md for the follow-up.
    _trailing_order_blocks,
)
from signals_helpers import (
    _proximity_emoji, _existing_position_note, _last_sale_recovery, _phase_emoji,
    buy_order_sizing, effectively_dry_run, has_capital_at_stake, log_poll, mode_tag,
    resolve_at_bar_close, should_alert_live, stop_status, MAX_RUNNING_LOW_DROP_PCT,
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
    schwab_client Slack-posts the BLOCKED/DRY RUN message either way UNLESS
    the node is below CAPITAL_AT_STAKE_THRESHOLD (2026-08-13 noise-reduction
    gate -- see signals_blocks._post_message's node_id param), in which case
    it's suppressed there but still logged via schwab_safety.check_order's
    own log_coverage_event calls for the 6 SafetyViolation reasons that
    previously had no record at all. This function just decides which button
    set the caller should render. Returns (True, order_id) on success --
    order_id is None in dry_run."""
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
            account, ticker, sizing['shares'], sizing['price'], sizing['trail_buy_pct'],
            node_dry_run=(node.get('state') != 'live'), node_id=node.get('id'))
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
    if node is None or node.get('state') == 'paper':
        db.log_coverage_event("automated_sell_mode_skip", _coverage_mode(pos.get('account')), ticker=ticker,
                               position_id=pos.get('id'), node_id=pos.get('wl_id'), result="skipped",
                               detail=f"node_state={node.get('state') if node else None!r}")
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
    if sl_order_id:
        # Bug #4 pre-replace check. Wrapped so a broker fetch failure (or any
        # other fault in a purely advisory check) can NEVER prevent a real
        # exit order from being placed.
        try:
            _verify_resting_before_replace(pos, node, account, ticker, sl_order_id, "stop-loss")
        except Exception as e:
            print(f"  [replace_check] {ticker}: pre-replace verification failed, proceeding: {e}")
    try:
        if sl_order_id:
            # Atomic replace (cancel-old + create-new as a single broker call)
            # instead of a separate cancel_order + place_trailing_sell -- closes
            # the window where a confirmed cancel could be followed by a failed/
            # blocked new placement, leaving nothing resting at the broker in
            # between (found 2026-07-27, raised directly by the user).
            _, exit_order_id = schwab_client.replace_order_with_trailing_sell(
                account, ticker, sl_order_id, shares, current_price, trail_sell_pct,
                node_dry_run=(node.get('state') != 'live'), node_id=node.get('id'))
        else:
            _, exit_order_id = schwab_client.place_trailing_sell(account, ticker, shares, current_price,
                                                                   trail_sell_pct, node_dry_run=(node.get('state') != 'live'),
                                                                   node_id=node.get('id'))
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
                f"🚨 *{ticker}* ({account} · {mode_tag(account, node)}) UNPROTECTED — {price_note}\n"
                f"(auto trailing-sell replace of stop-loss {sl_order_id} failed: {e})"
            )
        elif not isinstance(e, schwab_safety.SafetyViolation):
            _post_message(f"⚠️ {ticker} automated trailing-sell placement failed unexpectedly: {e} — falling back to manual")
        return False, None
    db.log_coverage_event("automated_sell_execution", _mode, ticker=ticker, position_id=pos.get('id'),
                           node_id=pos.get('wl_id'), result="placed",
                           detail=f"shares={shares} trail_sell_pct={trail_sell_pct}")
    if exit_order_id is not None:
        # sl_order_id (open_positions column, distinct from trail_state.exit_order_id)
        # always tracks whatever real order is currently resting -- point it at the
        # new trailing-sell so any later reader (a fresh replace attempt via this
        # same function on re-arm, the manual "cancel the existing stop-loss order"
        # reminder text, the TRAIL+hold_time_forced fallback in
        # _attempt_automated_exit_sell, check_sl_order_fills' independent poll) sees
        # what's actually resting instead of a dead order id (REPLACE case) or
        # nothing at all (fresh-placement case). Originally gated on `sl_order_id`
        # already being truthy (only rewriting on a REPLACE) -- found 2026-08-07,
        # same paired-review pass as check_sl_order_fills: when no prior sl_order_id
        # existed (e.g. the entry-time SL placement itself had failed), the fresh
        # trailing-sell's id lived ONLY in trail_state.exit_order_id, so
        # check_sl_order_fills (keyed on open_positions.sl_order_id) and the
        # missing_sl reconciliation check could never see it -- the exact same
        # undetected-fill exposure as the original LABD incident, just reached via a
        # missing-entry-SL precondition instead of an early-fill one. Confirmed safe
        # to write unconditionally: the missing_sl reconciliation check
        # (check_live_state_reconciliation) only reads sl_order_id when
        # `not state.get('trailing')`, and trailing is already True by the time this
        # function is called, so no false missing_sl alert results.
        # `exit_order_id is not None` guard kept from the original REPLACE-only fix:
        # extract_order_id can legitimately return None on a real success (e.g. a
        # missing Location header) -- without the guard, a real successful
        # replace/placement with an unextractable id would erase the previous
        # (valid, still-real) sl_order_id instead of just leaving it unset.
        db.set_sl_order_id_by_position(pos['id'], exit_order_id)
        # The stop-loss this price described is gone -- replaced by the
        # trailing-sell above (a no-op on the fresh-placement path, where there
        # was no SL price to begin with). Left uncleared, stop_status() would
        # report 'known' off a dead price (Opus review, 2026-08-01: a real SL
        # alert firing after this point would falsely say "broker stop on file,
        # no action needed" for a position actually protected by an unconfirmed
        # resting order, not the stop it describes).
        db.set_broker_stop_price_by_position(pos['id'], None)
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
    if node is None or node.get('state') == 'paper':
        db.log_coverage_event("automated_sell_mode_skip", _coverage_mode(pos.get('account')), ticker=ticker,
                               position_id=pos.get('id'), node_id=pos.get('wl_id'), result="skipped",
                               detail=f"node_state={node.get('state') if node else None!r} reason={reason}")
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
    if resting_order_id:
        # Bug #4 pre-replace check -- see _attempt_automated_sell's matching
        # call. Same non-blocking contract: advisory only, never gates the exit.
        try:
            _verify_resting_before_replace(pos, node, account, ticker, resting_order_id,
                                            resting_order_label)
        except Exception as e:
            print(f"  [replace_check] {ticker}: pre-replace verification failed, proceeding: {e}")
    try:
        if resting_order_id:
            # Atomic replace instead of cancel_order + place_equity_sell -- same
            # rationale as _attempt_automated_sell's TRAIL-side fix: closes the
            # window where a confirmed cancel could be followed by a failed/
            # blocked new placement, leaving nothing resting at the broker in
            # between (found 2026-07-27, raised directly by the user).
            _, order_id = schwab_client.replace_equity_order_with_market(
                account, ticker, resting_order_id, "SELL", shares, current_price,
                node_dry_run=(node.get('state') != 'live'), node_id=node.get('id'))
        else:
            _, order_id = schwab_client.place_equity_sell(account, ticker, shares, current_price,
                                                            node_dry_run=(node.get('state') != 'live'),
                                                            node_id=node.get('id'))
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
                f"🚨 *{ticker}* ({account} · {mode_tag(account, node)}) UNPROTECTED — {price_note}\n"
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
    if resting_order_id and resting_order_id == pos.get('sl_order_id'):
        # The real stop-loss order broker_stop_price describes is gone
        # regardless of whether the new order_id was extractable (unlike the
        # sl_order_id repoint above, which specifically needs a valid new id
        # to avoid pointing at nothing -- this just needs to stop claiming an
        # already-replaced stop is still resting). Opus review, 2026-08-01:
        # left uncleared, stop_status() would report 'known' off a dead
        # price, and the SL alert/reminder would falsely say "no action
        # needed" for a position now protected only by an unconfirmed
        # market-sell exit.
        db.set_broker_stop_price_by_position(pos['id'], None)
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


def _throttled(store, key, seconds):
    """Shared cooldown gate for the module's repost-suppression alerts.
    Returns True if the caller should fire NOW (and records the firing), False
    if `key` is still inside its cooldown window.

    Consolidates four separately hand-rolled copies of the identical
    get/compare/set dance (_RECONCILE_ALERTED, _RECONCILE_FETCH_FAIL_ALERTED,
    _STALE_PRICE_ALERTED, _ENTRY_ABANDON_ALERTED) -- _throttled_entry_abandon_
    alert's own docstring already pointed at _RECONCILE_ALERTED as "the right
    pattern for exactly this", so this generalizes that one rather than
    inventing a new shape.

    The store is passed in rather than shared globally on purpose: the four
    call sites use genuinely different key spaces (per-position-id+kind,
    per-account, per-position-id, per-wl_id+kind), so one dict would make an
    account name and a position id collidable in principle, and several test
    modules reset exactly one domain's cooldown (e.g.
    tests/test_live_state_reconciliation.py's _RECONCILE_ALERTED.clear())
    without wanting to clear the others.

    Records the firing BEFORE the caller posts, deliberately: a Slack post
    that then raises must still consume its cooldown, otherwise a persistent
    post failure retries unbounded every poll cycle -- the exact alert-storm
    shape _RECONCILE_FETCH_FAIL_ALERTED was added to prevent."""
    now = time.time()
    if now - store.get(key, 0) < seconds:
        return False
    store[key] = now
    return True


_RECONCILE_ALERTED: dict[str, float] = {}
_RECONCILE_COOLDOWN_SECS = 900  # 15 min -- matches the reminder-nag/section-alert cadence elsewhere

# Per-ACCOUNT (not per-position) cooldown for the fetch-failure alert below --
# a broker outage is one fact affecting every position on that account
# simultaneously, not N independent ones. Reuses _RECONCILE_COOLDOWN_SECS'
# cadence. Added 2026-08-10 after paired review (Fable independent-cold +
# Opus independent-cold + Opus contextual, all three converged) found the
# original version posted, unbounded, every poll cycle per position -- with
# 11 live soxl_ira nodes that's ~11 messages/5min indefinitely during any
# sustained outage, exactly the alert-fatigue shape the 2026-08-08 capital-
# at-stake redesign existed to eliminate.
_RECONCILE_FETCH_FAIL_ALERTED: dict[str, float] = {}


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


# _effectively_dry_run: signals_helpers.effectively_dry_run is the single
# shared implementation (also used by paper_trading._overlay_mode, which
# can't import this module directly). Aliased here under this module's own
# established name so every existing call site in this file (and active_
# signals.py's import of it) needs no further changes.
_effectively_dry_run = effectively_dry_run


def _coverage_mode(account, node=None):
    """'dry_run'/'live' for coverage_events logging, from the real per-account
    flag (and per-node override, if a node is given) -- falls back to
    'dry_run' (the safe/conservative label) if the account isn't recognized
    rather than raising, since logging must never interfere with the real
    control-flow it's observing. Deliberately does NOT delegate the
    unrecognized-account case to _effectively_dry_run -- that helper's own
    fallback (limits is None -> False/"not dry-run") exists so stop_status
    can fall through to a scope check, the opposite of what coverage logging
    needs here (found 2026-08-06, Opus review of the mode/dry_run->state
    collapse: this docstring's stated fallback had silently inverted)."""
    if schwab_safety.ACCOUNTS.get(account) is None:
        return "dry_run"
    return "dry_run" if _effectively_dry_run(account, node) else "live"


def _verify_resting_before_replace(pos, node, account, ticker, resting_order_id, label):
    """Pre-replace sanity check for bug #4 (2026-08-15).

    _attempt_automated_sell and _attempt_automated_exit_sell both take whatever
    sits in pos['sl_order_id'] (or trail_state.exit_order_id) and atomically
    replace it the moment their own exit condition fires -- with no check that
    the order actually resting at the broker is the one the algo thinks it is.
    That is precisely the case automation_principles.md #5 exists for, and it
    was never applied to this mechanism. It bit for real on 2026-08-14: a human
    placed a manual (mispriced) stop, and the daemon's own SL logic later
    replaced it with no way for anyone to know that had happened.

    DETECTION-ONLY, AND DELIBERATELY NON-BLOCKING. This never refuses the
    replace. A mismatch means our record of the resting order is wrong -- but
    the position still needs to exit, and refusing would strand it with a
    stale/unknown order resting and no way out, which is strictly worse than
    the bug being fixed. So it alerts loudly and lets the replace proceed,
    matching the "always alert distinctly, never silently reconcile" rule from
    the 2026-07-xx design decision (docs/deep_backlog.md ~4478) rather than
    inventing a new blocking gate on the exit path.

    Wrapped by its caller so nothing here -- a broker fetch failure included --
    can break a real exit."""
    expected_shares = pos.get('shares')

    # Provenance FIRST, before any broker call (2026-08-15 rebuttal round).
    # It is a property of `pos`, not of the order, and both reviewers caught
    # that leaving it below the match-lookup made it unreachable in the exact
    # case it was built for: a human who hand-places a replacement stop mints a
    # NEW broker id our DB never captured, so `match is None` returned early and
    # the daemon replaced the human's order in silence. _open_orders can also
    # raise (the caller swallows that), which would suppress this alert too.
    if pos.get('provenance') == 'manual':
        db.log_coverage_event("replace_target_mismatch", _coverage_mode(account), ticker=ticker,
                               position_id=pos.get('id'), node_id=node.get('id'),
                               result="manual_order_replaced",
                               detail=f"{label} id={resting_order_id} on a provenance='manual' position")
        _post_message(
            f"ℹ️ *{ticker}* ({account} · {mode_tag(account, node)}) — replacing a MANUALLY-placed {label} "
            f"order ({resting_order_id}) with the algo's own exit\n(this position was reconciled by hand; "
            f"the replace is expected, but the manual order is now gone)"
        )

    orders = schwab_safety._open_orders(account)
    # Scope to THIS ticker's resting SELLs before matching -- _open_orders is
    # account-wide, both sides, all symbols, and soxl_ira alone holds 8+ nodes.
    # Without this, the id-miss fallback below could adopt a different ticker's
    # stop entirely.
    resting_sells = [
        o for o in orders
        if any(leg.get('instruction') == 'SELL'
                and leg.get('instrument', {}).get('symbol') == ticker
                for leg in o.get('orderLegCollection', []))
    ]
    match = next((o for o in resting_sells if str(o.get('orderId')) == str(resting_order_id)), None)
    if match is None:
        # Reuse Stage C's matcher rather than giving up: a broker-side replace
        # mints a new id, and there are three confirmed ways our stored id goes
        # stale while a real stop keeps resting (extract_order_id legitimately
        # returning None on success; the hold-time-forced TRAIL path never
        # syncing sl_order_id; a human replacing the stop). Deliberately does
        # NOT suppress the id-miss alert below -- "your recorded id is dead" is
        # itself worth saying -- but when a substitute IS found we check it
        # rather than returning blind.
        _substitute = _match_resting_order(resting_sells, None)
        if _substitute is not None:
            db.log_coverage_event("replace_target_mismatch", _coverage_mode(account), ticker=ticker,
                                   position_id=pos.get('id'), node_id=node.get('id'),
                                   result="resting_order_id_stale",
                                   detail=f"{label} id={resting_order_id} not resting; a substitute "
                                          f"stop {_substitute.get('orderId')} is")
            _post_message(
                f"⚠️ *{ticker}* ({account} · {mode_tag(account, node)}) — the recorded {label} order "
                f"{resting_order_id} is NOT resting, but another stop ({_substitute.get('orderId')}) is\n"
                f"(our order id is stale — verifying against the substitute; the replace proceeds)"
            )
            match = _substitute

    if match is None:
        db.log_coverage_event("replace_target_mismatch", _coverage_mode(account), ticker=ticker,
                               position_id=pos.get('id'), node_id=node.get('id'),
                               result="resting_order_not_found",
                               detail=f"{label} id={resting_order_id} not among {len(orders)} open orders")
        _post_message(
            f"⚠️ *{ticker}* ({account} · {mode_tag(account, node)}) — replacing {label} order "
            f"{resting_order_id}, but that order is NOT resting at the broker right now "
            f"(it may have already filled or been cancelled)\n(proceeding with the replace anyway so "
            f"the exit isn't stranded — verify the position's real state after)"
        )
        return

    # Stop-shape and stop-PRICE checks (2026-08-15 rebuttal round). The first
    # version verified id-existence and quantity only, so the literal
    # 2026-08-14 defect -- a human's MISPRICED stop -- passed every branch in
    # silence unless provenance happened to be 'manual', which it isn't when
    # someone places a stop directly at Schwab (the column defaults to
    # 'daemon'). And this is NOT redundant with Stage C: Stage C's price
    # comparison is gated on `not state.get('trailing')`, so it stops looking
    # the moment a position arms -- which is exactly when _attempt_automated_sell
    # fires this replace. For an armed position this is the ONLY place a
    # mispriced stop can be caught.
    #
    # Both scoped to the stop-loss label. A trailing-sell's stopPrice is an
    # offset-derived moving level, so comparing it against a fixed SL would
    # false-alarm on every hold-time-forced TRAIL replace.
    if label == 'stop-loss':
        _type = (match.get('orderType') or '').upper()
        if _type and _type not in ('STOP', 'STOP_LIMIT', 'TRAILING_STOP'):
            db.log_coverage_event("replace_target_mismatch", _coverage_mode(account), ticker=ticker,
                                   position_id=pos.get('id'), node_id=node.get('id'),
                                   result="not_a_stop_order",
                                   detail=f"id={resting_order_id} is orderType={_type}, not a stop")
            _post_message(
                f"⚠️ *{ticker}* ({account} · {mode_tag(account, node)}) — the order recorded as this "
                f"position's stop-loss ({resting_order_id}) is a {_type}, not a stop\n"
                f"(this is what an accidental manual limit-sell looks like — the replace proceeds, but "
                f"verify what that order actually was)"
            )
        _expected_stop = _expected_sl_price(pos)
        _real_stop = match.get('stopPrice')
        if (_expected_stop is not None and _real_stop is not None
                and abs(float(_real_stop) - _expected_stop) > _RECONCILE_SL_PRICE_TOLERANCE):
            db.log_coverage_event("replace_target_mismatch", _coverage_mode(account), ticker=ticker,
                                   position_id=pos.get('id'), node_id=node.get('id'),
                                   result="stop_price_mismatch",
                                   detail=f"id={resting_order_id} rests at {float(_real_stop):.4f}, "
                                          f"algo expects {_expected_stop:.4f}")
            _post_message(
                f"⚠️ *{ticker}* ({account} · {mode_tag(account, node)}) — the stop-loss being replaced rests "
                f"at ${float(_real_stop):.4f} but the algo's own stop for this position is "
                f"${_expected_stop:.4f}\n(it was not protecting where the algo believed — the replace "
                f"proceeds and corrects it; verify whether it was placed or edited by hand)"
            )

    real_qty = _resting_order_quantity(match)
    if real_qty is not None and expected_shares is not None and float(real_qty) != float(expected_shares):
        db.log_coverage_event("replace_target_mismatch", _coverage_mode(account), ticker=ticker,
                               position_id=pos.get('id'), node_id=node.get('id'),
                               result="quantity_mismatch",
                               detail=f"{label} id={resting_order_id} covers {real_qty:g}, position holds {expected_shares:g}")
        _post_message(
            f"⚠️ *{ticker}* ({account} · {mode_tag(account, node)}) — the {label} order being replaced covers "
            f"{float(real_qty):g} shares but the position holds {expected_shares:g}\n"
            f"(proceeding with the replace, which will be sized to {expected_shares:g} — verify no "
            f"unexpected fill or manual trade explains the gap)"
        )



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
    if not _throttled(_RECONCILE_ALERTED, f"{pos['id']}:{kind}", _RECONCILE_COOLDOWN_SECS):
        return True
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
    node = db.get_watch_list_node_by_id(pos.get('wl_id'))
    if not _throttled(_STALE_PRICE_ALERTED, str(pos['id']), _STALE_PRICE_COOLDOWN_SECS):
        return
    _post_message(
        f"⚠️ *{ticker}* ({account} · {mode_tag(account, node)}) exit check skipped this poll — no fresh price available\n"
        f"(`_current_price` returned None: stale/missing same-day data; position remains open, "
        f"unmonitored until the next successful refresh)"
    )


_RECONCILE_FETCH_RETRIES = 3
_RECONCILE_FETCH_RETRY_DELAY_SECS = 3

# Grace window before a position with NO sl_order_id at all is flagged
# unprotected (Stage B, 2026-08-15). Must comfortably exceed
# _place_stop_loss_for_position's own retry/confirm envelope, or normal
# placement latency false-alarms every time: worst case there is
# _SL_FAST_CONFIRM_ATTEMPTS(5) x _SL_FAST_CONFIRM_INTERVAL_SECS(2) = 10s of
# fast-confirm plus _SL_PLACEMENT_RETRY_ATTEMPTS(3) x
# _SL_PLACEMENT_RETRY_DELAY_SECS(2) = 6s of retry, ~16s total. 5 minutes is
# ~19x that -- deliberately generous, since the cost of a late alert here is
# minutes while the cost of a false one is alert fatigue on the single
# loudest "you are unprotected" signal this system has.
_RECONCILE_MISSING_SL_GRACE_SECS = 300

# Tolerance for comparing the resting stop's real broker price against the
# price the algo would compute (Stage C). Half a cent -- tight enough to catch
# a genuinely wrong anchor (the 2026-07-31 signal_price-vs-entry_price bug
# moved stops by whole percent), loose enough to absorb the broker's own
# tick/rounding of a price we submitted with more precision than it accepts.
_RECONCILE_SL_PRICE_TOLERANCE = 0.005


def _expected_sl_price(pos):
    """The stop price _place_stop_loss_for_position would compute for this
    position, recomputed here from the position's OWN persisted config rather
    than the live watch_list node -- the node's params can be edited after
    entry, and the exit params baked onto the open_positions row at entry time
    are what the algo's own check_exit actually uses (see CLAUDE.md's EDC
    entry). Anchored to entry_price, matching that function exactly (the
    2026-07-31 fix: signal_price is materially different for the trailing-buy
    strategies and anchoring there placed real stops above market). Returns
    None when the position has no usable SL config, so callers skip the
    comparison rather than compare against a fabricated number."""
    try:
        sl_pct = pos.get('fixed_sl') if strategies.uses_fixed_sl(pos.get('strategy')) else pos.get('stop_loss')
    except Exception:
        return None
    if not sl_pct or not pos.get('entry_price'):
        return None
    return pos['entry_price'] * (1 - sl_pct / 100)


def _resting_order_quantity(o):
    """Reads a resting order's share count LEG-FIRST, matching this codebase's
    established convention for raw broker-order JSON (schwab_client.
    get_real_orders uses `leg.get("quantity")` alongside `o.get("stopPrice")`,
    an explicit order-vs-leg split; schwab_safety sums leg quantities in both
    of its own readers).

    This matters beyond style. `orders` here is UNNORMALIZED broker JSON from
    schwab_safety._open_orders, and tests/fake_broker.py's _make_order emits NO
    top-level `quantity` at all -- only orderLegCollection[0]['quantity']. So
    an order-level-only read is a silent no-op against the project's own
    designated regression fixture, i.e. exactly the "a per-call mock hides what
    fake_broker was built to catch" hazard CLAUDE.md warns about. Found by the
    cold reviewer and confirmed by the contextual one, 2026-08-15.

    The order-level fallback is kept because real Schwab responses do carry a
    top-level `quantity` -- but it's the fallback, not the primary, so the
    fixture exercises the same path production does."""
    for leg in o.get('orderLegCollection') or []:
        if leg.get('instruction') == 'SELL' and leg.get('quantity') is not None:
            return leg['quantity']
    return o.get('quantity')


def _match_resting_order(resting_sells, sl_order_id):
    """Picks the resting SELL order this position's sl_order_id actually points
    at. Falls back to the sole resting order when the id doesn't match anything
    (a stop that was replaced at the broker keeps protecting the position under
    a NEW id -- schwab_client's replace path mints one -- so refusing to
    compare would blind the price/quantity checks in exactly the case where a
    replace went wrong).

    The fallback is restricted to genuinely STOP-shaped orders. An earlier
    version fell back to ANY sole resting SELL, which both reviewers flagged
    (2026-08-15): a manual limit SELL -- e.g. the accidental limit-sell that
    actually closed the position in the incident this work fixes, or a
    deliberate skim -- would be adopted as "the stop", and since a LIMIT order
    carries no stopPrice the price check would silently skip while the
    quantity check still fired against it, producing a spurious "partially
    unprotected"/"OVERSELL RISK" alert about an order that was never a stop.

    Returns None when 2+ stops rest and none matches by id: guessing which is
    'the' stop there would be inventing an answer, and the shares/duplicate-
    order guards elsewhere already cover that shape."""
    if sl_order_id is not None:
        for o in resting_sells:
            if str(o.get('orderId')) == str(sl_order_id):
                return o
    stops = [o for o in resting_sells
             if (o.get('orderType') or '').upper() in ('STOP', 'STOP_LIMIT', 'TRAILING_STOP')]
    if len(stops) == 1:
        return stops[0]
    return None


def _past_sl_grace(pos, now=None):
    """True once a position is old enough that a missing stop can't still be
    normal placement latency (see _RECONCILE_MISSING_SL_GRACE_SECS). now= is
    injectable rather than read from the clock so tests can exercise both sides
    of the window -- fake_broker has no clock control and freezegun isn't
    installed (mirrors check_intraday_risk_review(now=None)'s pattern).
    Unparseable/absent entry_time returns True: a position we can't date is
    already anomalous, and defaulting to silence there would recreate exactly
    the class of gap this branch exists to close."""
    entry_time = pos.get('entry_time')
    if not entry_time:
        return True
    if not hasattr(entry_time, 'strftime'):
        try:
            entry_time = datetime.strptime(str(entry_time), '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return True
    return ((now or datetime.now()) - entry_time).total_seconds() >= _RECONCILE_MISSING_SL_GRACE_SECS


def check_live_state_reconciliation(open_positions, now=None):
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
    never assumes the broker is wrong. A fetch failure retries up to
    _RECONCILE_FETCH_RETRIES total attempts, _RECONCILE_FETCH_RETRY_DELAY_SECS
    apart (2026-08-10, user's call -- a single transient failure shouldn't
    skip a position's check silently); only after all attempts fail does it
    log 'failed_after_retries' and alert (a real broker-connectivity problem
    is itself worth knowing about, not just quietly missing this position for
    the cycle) and skip that position for this cycle. The alert itself is
    cooldown-gated per account (_RECONCILE_FETCH_FAIL_ALERTED, see its own
    comment) and wrapped so a Slack-post failure can't abort the rest of this
    loop. Once an account has exhausted its retries once in a given call,
    every later position on that SAME account this cycle skips straight to
    the failure path with no further retry/sleep -- a broker-wide outage
    would otherwise cost every open position its own full retry-and-sleep
    sequence (found by all 3 paired-review passes: up to 6s x N positions
    stacked ahead of the pinned entry/exit scans this check was moved in
    front of, see its call site in active_signals.py). Nothing here blocks or
    gates a real order, so there's no fail-closed obligation the way
    schwab_safety.check_order has -- this alerts, it doesn't refuse
    anything."""
    _accounts_down_this_cycle: set[str] = set()
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
        _node = db.get_watch_list_node_by_id(_node_id)
        # Retry a transient fetch failure a few seconds apart (2026-08-10,
        # user's call) before giving up -- a single flaky call used to skip
        # the whole position for the cycle silently; 3 failures in a row is
        # a real signal worth an alert (a broker-connectivity problem, not
        # noise), not just quietly missing this position's check.
        real_shares = orders = None
        last_exc = None
        if account in _accounts_down_this_cycle:
            # Already exhausted retries once for this account this cycle --
            # don't pay another 6s of sleep to reconfirm what's already known.
            last_exc = RuntimeError(f"account '{account}' already failed reconciliation fetch this cycle")
        else:
            for attempt in range(1, _RECONCILE_FETCH_RETRIES + 1):
                try:
                    real_shares = schwab_client.get_real_position(account, ticker)
                    orders = schwab_safety._open_orders(account)
                    last_exc = None
                    break
                except Exception as e:
                    last_exc = e
                    if attempt < _RECONCILE_FETCH_RETRIES:
                        time.sleep(_RECONCILE_FETCH_RETRY_DELAY_SECS)
            if last_exc is not None:
                _accounts_down_this_cycle.add(account)
        if last_exc is not None:
            db.log_coverage_event(
                "reconciliation_fetch_failed", _coverage_mode(account),
                ticker=ticker, position_id=pos.get('id'), node_id=_node_id,
                result="failed_after_retries",
                detail=f"{_RECONCILE_FETCH_RETRIES} attempts, last error: {last_exc}"
            )
            if _throttled(_RECONCILE_FETCH_FAIL_ALERTED, account, _RECONCILE_COOLDOWN_SECS):
                try:
                    _post_message(
                        f"⚠️ *{ticker}* ({account} · {mode_tag(account, _node)}) live-state reconciliation "
                        f"fetch failed {_RECONCILE_FETCH_RETRIES}x in a row this cycle — could not verify "
                        f"broker state\n(`{last_exc}`)"
                    )
                except Exception as post_exc:
                    print(f"  [reconcile] {ticker}: fetch-failure alert itself failed to post: {post_exc}")
            print(f"  [reconcile] {ticker}: fetch failed {_RECONCILE_FETCH_RETRIES}x, skipping this cycle: {last_exc}")
            continue

        mismatch_found = False
        expected_shares = pos.get('shares')
        # The CORE row's own share count, deliberately kept un-widened by the
        # add-on-leg patch below. The two numbers answer different questions
        # and must not be conflated (both reviewers independently caught the
        # first version of Stage C doing exactly that, 2026-08-15):
        #   expected_shares -> "what does the BROKER hold for this ticker" =
        #     core + leg, since a real leg is a second real fill on the same
        #     ticker/account.
        #   core_shares     -> "what does the core position's own resting STOP
        #     cover" = core only, because _place_stop_loss_for_position sizes
        #     off db.get_open_position_by_wl_id(...)['shares'] and the leg
        #     carries its OWN separate stop (_place_stop_loss_for_addon_leg /
        #     set_addon_leg_sl_order_id).
        # Comparing the core stop's quantity against the widened number is
        # structurally guaranteed to mismatch while any leg is open -- a
        # permanent false "partially unprotected" alert that also feeds the
        # reconciliation_mismatches streak and trips the node circuit breaker
        # within ~3 polls on a completely correct state.
        core_shares = pos.get('shares')
        # Add-on leg patch (Part 6.3, docs/plans/real_order_execution_drought_
        # addon.md) -- REQUIRED, not optional: with a real leg open, the
        # broker legitimately holds 2x what open_positions.shares says (core
        # + leg, same ticker/account, two separate real fills). Without this,
        # every poll after a real add-on fires would false-positive a
        # "shares mismatch" and feed the reconciliation_mismatches streak.
        if expected_shares is not None:
            _open_leg = db.get_open_addon_leg_by_parent(pos['id'])
            if _open_leg is not None:
                expected_shares = expected_shares + _open_leg.get('shares', 0)
        if expected_shares is not None and real_shares != expected_shares:
            mismatch_found |= _alert_reconcile_mismatch(
                pos, "shares",
                f"⚠️ *{ticker}* ({account} · {mode_tag(account, _node)}) live-state mismatch: `open_positions` tracks "
                f"{expected_shares:g} shares, broker shows {real_shares:g} — broker is ground "
                f"truth; suggested fix: verify no unexpected fill/manual trade explains the gap, "
                f"then correct `open_positions.shares` to {real_shares:g}"
            )

        state = pos.get('trail_state') or {}
        resting_sells = [
            o for o in orders
            if any(leg.get('instruction') == 'SELL'
                    and leg.get('instrument', {}).get('symbol') == ticker
                    for leg in o.get('orderLegCollection', []))
        ]
        has_sell_order = bool(resting_sells)
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
                f"⚠️ *{ticker}* ({account} · {mode_tag(account, _node)}) live-state mismatch: armed (trailing stop active) "
                f"but no trailing-sell order was ever confirmed placed — "
                f"{'a resting SELL order was found (likely the original stop-loss, still intact)' if has_sell_order else 'NO resting SELL order was found at all'}; "
                f"suggested fix: check the broker directly and either manually place a trailing-sell "
                f"for {expected_shares:g} shares or confirm the existing resting order is adequate"
            )
        elif state.get('trailing') and state.get('order_placed') and not has_sell_order:
            mismatch_found |= _alert_reconcile_mismatch(
                pos, "missing_trailing_sell",
                f"⚠️ *{ticker}* ({account} · {mode_tag(account, _node)}) live-state mismatch: trailing-sell marked placed but "
                f"no resting SELL order found at the broker — position may be unprotected; "
                f"suggested fix: place a trailing-sell order for {expected_shares:g} shares now"
            )
        elif not state.get('trailing') and pos.get('sl_order_id') and not has_sell_order:
            mismatch_found |= _alert_reconcile_mismatch(
                pos, "missing_sl",
                f"⚠️ *{ticker}* ({account} · {mode_tag(account, _node)}) live-state mismatch: SL order id {pos['sl_order_id']} "
                f"is recorded but no resting SELL order found at the broker — position may be "
                f"unprotected; suggested fix: place a stop-loss order for {expected_shares:g} shares now"
            )
        elif (not state.get('trailing') and not pos.get('sl_order_id')
                and not has_sell_order and _past_sl_grace(pos, now)):
            # Stage B widening, 2026-08-15. The branch above is gated on
            # sl_order_id ALREADY being truthy, so it structurally cannot see
            # the strictly worse case: a position that never got a stop at
            # all. That's not hypothetical -- it's the literal SOXS/2026-08-14
            # condition (fill never reconciled -> _place_stop_loss_for_position
            # never called -> no sl_order_id, no broker order, no alert from
            # anywhere). Grace-gated off entry_time so a normal placement
            # in flight doesn't false-alarm (see
            # _RECONCILE_MISSING_SL_GRACE_SECS). Alert-only, like every other
            # branch here -- never places the missing stop itself
            # (automation_principles.md #5).
            #
            # _expected_sl_price can legitimately return None (its own
            # docstring promises callers will skip rather than compare against
            # a fabricated number) -- most reachably when the position's
            # effective sl_pct is falsy, which is EXACTLY the state that
            # reaches this branch, since _place_stop_loss_for_position's
            # `if not sl_pct: return` means such a position never gets a stop
            # at all. Formatting it directly (`f"${None:.2f}"`) raises
            # TypeError inside this per-position loop, which _guarded catches
            # only at whole-function granularity -- so every LATER position
            # would silently lose its reconciliation check that cycle, and the
            # loudest alert in the system would never post. Found by both
            # reviewers independently, 2026-08-15; the price is omitted from
            # the message rather than faked.
            #
            # Breaker interaction, deliberate: _alert_reconcile_mismatch
            # returns True even when the 15-min Slack cooldown suppresses the
            # post (its docstring: the streak "should reflect true state, not
            # alert-cooldown noise"), so a persistently unprotected position
            # keeps feeding record_node_streak and will trip the monitor-only
            # node circuit breaker. That is the intended reading of a real
            # unprotected position -- and record_node_streak never pauses
            # automation itself, it only alerts once via just_tripped.
            _never_had_sl_price = _expected_sl_price(pos)
            mismatch_found |= _alert_reconcile_mismatch(
                pos, "never_had_sl",
                f"🚨 *{ticker}* ({account} · {mode_tag(account, _node)}) UNPROTECTED — no stop-loss was ever "
                f"recorded for this position and none is resting at the broker\n"
                f"(open since {pos.get('entry_time')}, {expected_shares:g} shares; suggested fix: place a "
                f"stop-loss SELL for {expected_shares:g} shares"
                f"{f' at ~${_never_had_sl_price:.2f}' if _never_had_sl_price is not None else ''} now, "
                f"or confirm the position was already exited)"
            )
        if has_sell_order and not state.get('trailing') and pos.get('sl_order_id'):
            # Stage C, 2026-08-15: until now "a SELL order is resting" was the
            # entire protection check -- neither its price nor its quantity was
            # ever compared against what the algo would actually compute. Both
            # have real precedent for going wrong: the 2026-07-31 SL-anchor bug
            # placed real stops off signal_price instead of entry_price (whole
            # percent off, sometimes above market), and a top-up that lands
            # after the stop is placed leaves the stop covering fewer shares
            # than the position holds. Detection-only by explicit design --
            # this must NOT auto-replace, or it reintroduces exactly the
            # silent-override behavior of bug #4 that this same session is
            # fixing elsewhere.
            _expected_price = _expected_sl_price(pos)
            _resting = _match_resting_order(resting_sells, pos.get('sl_order_id'))
            if _resting is not None:
                _real_price = _resting.get('stopPrice')
                _real_qty = _resting_order_quantity(_resting)
                if (_expected_price is not None and _real_price is not None
                        and abs(float(_real_price) - _expected_price) > _RECONCILE_SL_PRICE_TOLERANCE):
                    mismatch_found |= _alert_reconcile_mismatch(
                        pos, "sl_price_mismatch",
                        f"⚠️ *{ticker}* ({account} · {mode_tag(account, _node)}) live-state mismatch: resting stop is at "
                        f"${float(_real_price):.4f} but the algo's own stop for this position is "
                        f"${_expected_price:.4f} — the broker order does not protect where the algo "
                        f"thinks it does; suggested fix: verify whether this stop was placed/edited "
                        f"manually, then re-place it at ${_expected_price:.4f} if not deliberate"
                    )
                # core_shares, NOT expected_shares -- see core_shares' own
                # comment above. The core stop covers the core leg only; an
                # open add-on leg has its own separate stop, so comparing
                # against the leg-widened number would alarm every poll while
                # any leg is open.
                if (_real_qty is not None and core_shares is not None
                        and float(_real_qty) != float(core_shares)):
                    mismatch_found |= _alert_reconcile_mismatch(
                        pos, "sl_quantity_mismatch",
                        f"⚠️ *{ticker}* ({account} · {mode_tag(account, _node)}) live-state mismatch: resting stop covers "
                        f"{float(_real_qty):g} shares but the core position holds {core_shares:g} — "
                        f"{'partially unprotected' if float(_real_qty) < float(core_shares) else 'OVERSELL RISK: the stop would sell more than is held'}"
                        f"; suggested fix: cancel and re-place the stop for {core_shares:g} shares"
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
        _, order_id = schwab_client.place_equity_buy(account, ticker, sizing['shares'], sizing['price'],
                                                       node_dry_run=(node.get('state') != 'live'),
                                                       node_id=node.get('id'))
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
    # place_stop_loss short-circuits and returns (None, None) for a dry_run
    # account, with no exception -- so "placement succeeded" (no exception,
    # sl_placement result="placed"/"placed_on_retry") is true for BOTH a
    # real success AND a dry-run no-op. Recording broker_stop_price for a
    # dry-run account would falsely claim a real broker stop exists (found
    # while wiring this write, 2026-08-01 -- would have contradicted
    # stop_status's dedicated 'dry-run' branch, itself added after an Opus
    # review caught the first version of this feature rendering a false
    # placement-failure alarm for every dry-run account).
    _dry_run_account = _effectively_dry_run(account, node)
    sl_pct = pos.get('fixed_sl') if strategies.uses_fixed_sl(pos['strategy']) else pos['stop_loss']
    if not sl_pct:
        return
    stop_price = pos['entry_price'] * (1 - sl_pct / 100)
    shares = int(pos['shares'])
    try:
        _, sl_order_id = schwab_client.place_stop_loss(account, ticker, shares, stop_price,
                                                         node_dry_run=(node.get('state') != 'live'),
                                                         node_id=node.get('id'))
    except schwab_safety.SafetyViolation as e:
        # Not retried -- a policy block (kill switch, paused automation, an
        # existing SL/SELL order already resting) won't resolve differently
        # on a bare retry, matching _submit_order_with_retry's established
        # convention elsewhere in this module.
        db.log_coverage_event("sl_placement", _coverage_mode(account), ticker=ticker, position_id=pos.get('id'),
                               node_id=node.get('id'), result="blocked", detail=str(e))
        _post_message(
            f"🚨 *{ticker}* ({account} · {mode_tag(account, node)}) UNPROTECTED — place stop-loss SELL {shares} @ ~${stop_price:.2f}\n"
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
                    _, market_order_id = schwab_client.place_equity_sell(account, ticker, shares, current_price,
                                                                           node_dry_run=(node.get('state') != 'live'),
                                                                           node_id=node.get('id'))
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
                    f"🤖 *{ticker}* ({account} · {mode_tag(account, node)}) SL already breached by the time it could be "
                    f"placed — market SELL {shares} submitted @ ~${current_price:.2f} instead "
                    f"(target stop was ${stop_price:.2f})"
                )
                return
            try:
                _, sl_order_id = schwab_client.place_stop_loss(account, ticker, shares, stop_price,
                                                         node_dry_run=(node.get('state') != 'live'),
                                                         node_id=node.get('id'))
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
            # broker_stop_price is written unconditionally (unlike sl_order_id
            # just below, gated on extract_order_id succeeding) -- placement
            # is already confirmed successful at this point (no exception,
            # coverage event above logged "placed_on_retry"), so the price is
            # a known fact regardless of whether the order id happened to be
            # extractable. Gating both writes on the same sl_order_id check
            # was itself a bug (Opus review, 2026-08-01): a real success with
            # an unextractable id would have suppressed broker_stop_price too,
            # rendering the new 'automation-pending' alert as a false
            # placement-failure claim for a genuinely-protected position.
            if not _dry_run_account:
                db.set_broker_stop_price_by_position(pos['id'], stop_price)
            if sl_order_id is not None:
                db.set_sl_order_id_by_position(pos['id'], sl_order_id)
            return
        db.log_coverage_event("sl_placement", _coverage_mode(account), ticker=ticker, position_id=pos.get('id'),
                               node_id=node.get('id'), result="failed_unexpectedly", detail=str(last_error))
        _post_message(
            f"🚨 *{ticker}* ({account} · {mode_tag(account, node)}) UNPROTECTED — place stop-loss SELL {shares} @ ~${stop_price:.2f}\n"
            f"(stop-loss placement failed after {_SL_PLACEMENT_RETRY_ATTEMPTS} attempts: {last_error})"
        )
        return
    db.log_coverage_event("sl_placement", _coverage_mode(account), ticker=ticker, position_id=pos.get('id'),
                           node_id=node.get('id'), result="placed", detail=f"stop_price={stop_price:.4f}")
    if not _dry_run_account:
        db.set_broker_stop_price_by_position(pos['id'], stop_price)
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
    _reconcile_buy_fill(ticker, fill['price'], fill['quantity'], wl_id=node['id'], account=account)


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
    not lower running_low by more than MAX_RUNNING_LOW_DROP_PCT in one step
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
        if node is None or node.get('state') == 'paper':
            continue
        account = node.get('account')
        limits = schwab_safety.ACCOUNTS.get(account)
        if not limits or _effectively_dry_run(account, node):
            continue  # dry_run accounts/nodes are already covered by update_dry_run_buys
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
        floor = current * (1 - MAX_RUNNING_LOW_DROP_PCT / 100)
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
    if not _throttled(_ENTRY_ABANDON_ALERTED, f"{wl_id}:{kind}",
                       _ENTRY_ABANDON_ALERT_COOLDOWN_SECS):
        return
    _post_message(text)


def check_entry_abandon():
    """Live equivalent of the backtest kernel's entry-abandon timeout
    (backtester.py's `wait_bars >= max_hours_to_hold` in
    `_simulate_trail_buy`/`_simulate_trail_both`, all four resolutions):
    without this, a trailing buy that never bounces has no timeout live at
    all -- the real broker order rests GOOD_TILL_CANCEL forever
    (schwab_client._build_trailing_order), and schwab_safety._has_open_order/
    _open_buy_tickers_in_account then permanently block every other real
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
                f"⏱️⚠️ *{ticker}* ({account!r} · {mode_tag(account, node)}) — trailing buy past its "
                f"{node['max_hold_hours']}h hold-time limit, but this account isn't recognized — cannot "
                f"determine whether a real order needs cancelling. Verify manually.")
            continue
        if not _effectively_dry_run(account, node) and pb.get('order_placed') and not order_id:
            # Real (non-dry_run) account/node: order_placed=True with no
            # order_id is the manual "Trailing Buy Order Placed" Slack flow
            # (the user places it directly at Schwab -- we never capture its
            # id), not a dry_run placement (schwab_client's dry_run short-
            # circuit is indistinguishable from this by order_id alone,
            # hence the `_effectively_dry_run` guard here -- must check the
            # per-node override too, not just the account, or a node-level-
            # forced-dry-run entry on a real account is misread as an
            # untracked manual order). A real order may be resting
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
                f"⏱️⚠️ *{ticker}* ({account} · {mode_tag(account, node)}) — trailing buy has been resting past "
                f"its {node['max_hold_hours']}h hold-time limit, but no broker order id is on file "
                f"(placed manually) — cannot auto-cancel. Cancel it manually at the broker if it's still "
                f"resting, and tap Skip on the reminder once confirmed.")
            continue
        did_cancel = False
        if order_id and not _effectively_dry_run(account, node):
            try:
                _, status = schwab_client.cancel_order(account, ticker, order_id)
            except Exception as e:
                db.log_coverage_event("entry_abandon_timeout", mode, ticker=ticker, node_id=wl_id,
                                       result="cancel_failed", detail=str(e))
                _throttled_entry_abandon_alert(
                    wl_id, "cancel_failed",
                    f"⚠️ *{ticker}* ({account} · {mode_tag(account, node)}) — entry-abandon timeout hit "
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
                    _reconcile_buy_fill(ticker, fill['price'], fill['quantity'], wl_id=wl_id, account=account)
                else:
                    _post_message(f"⚠️ *{ticker}* ({account} · {mode_tag(account, node)}) — entry-abandon "
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
        _post_message(f"⏱️ *{ticker}* ({account} · {mode_tag(account, node)}) — trailing buy never bounced "
                      f"within {node['max_hold_hours']}h — entry abandoned, {cancelled_note}. "
                      f"No position opened.")


# ---------------------------------------------------------------------------
# Dry-run fill synthesis
# ---------------------------------------------------------------------------

def update_dry_run_buys():
    """Mirrors paper_trading.update_paper_buys, but for a node whose real
    order attempts are effectively simulated (_effectively_dry_run: either
    the node's own state=='dry_run', or its account's trading_enabled is
    False). schwab_client short-circuits before the real broker call in
    either case (_place_trailing_order/_place_equity_order both return
    (None, None)), so the pending_buys row notify_buy_signal created will never
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
        if node.get('state') == 'paper':
            # A research-mode node's BUY never reaches here today (routed to
            # paper_trading.start_paper_buy instead, which never creates a
            # pending_buys row) -- explicit guard anyway, so a future routing
            # change can't open a real open_positions row alongside the node's
            # own paper_positions row for the same wl_id (Opus review 2026-07-26).
            continue
        account = node.get('account')
        limits = schwab_safety.ACCOUNTS.get(account)
        if limits is None or not _effectively_dry_run(account, node):
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
    _post_message(f"[DRY RUN] would have filled {label} — {ticker}  {shares}sh @ ${price:.4f}", node_id=node.get('id'))


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
            _post_message(f"[DRY RUN] trailing-sell would arm — {ticker}", node_id=pos.get('wl_id'))
        if reason:
            # exit_bar_time passed directly (not via trail_state's exit_decision_bar
            # stash active_signals.py's real exit paths use) -- a dry-run-sim close is
            # synchronous, decided and closed in the same call, so there's no async
            # fill-confirmation gap for a later close_position() call to need it from.
            db.close_position(pos['id'], exit_signal_price=cp, exit_price=target,
                               exit_time=datetime.now(), exit_reason=reason, exit_bar_time=last_bar_ts)
            db.log_coverage_event("exit_fill", "dry_run", ticker=ticker, position_id=pos['id'],
                                   node_id=pos.get('wl_id'), result=reason, detail=f"price={target:.4f}")
            _post_message(f"[DRY RUN] would have closed — {ticker}  {reason} @ ${target:.4f}", node_id=pos.get('wl_id'))
            dry_run_sell_alerted.add((pos['id'], last_bar_ts))
            try:
                close_addon_leg_real_if_open(pos, target, reason, datetime.now())
            except Exception as e:
                print(f"  [warn] {ticker} — unexpected error in close_addon_leg_real_if_open: {e}")


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
        if _limits and _limits.cash_settlement_type == 'cash':
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

    # Sub-capital-at-stake nodes get zero real-time Slack (2026-08-08 user
    # call) -- both dry_run AND small-notional live nodes (e.g. soxl_ira's
    # $500-$2,500 nodes) are below the bar; every event here is still fully
    # logged (coverage_events, pending_buys) for EOD-only review regardless.
    # Tracking below (pending_buys, automated placement) is unaffected --
    # only the Slack post itself is skipped.
    if not should_alert_live(node):
        channel, ts = None, None
    else:
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


def notify_drought_buy_signal(node, decision):
    """Real drought-overlay entry order placement -- modelled on
    notify_buy_signal (Part 4.2, docs/plans/real_order_execution_drought_addon.md),
    reusing its own dispatch/sizing/fill-tracking machinery rather than a
    parallel implementation. Drought entry mirrors BOTH core mechanisms,
    dispatching on db._is_trailing_buy(node) exactly as notify_buy_signal
    does -- because drought reuses the node's own strategy, the entry order
    shape must follow the node's own entry_timing/strategy, never hardcoded.

    decision is evaluate_drought_entry's real-mode return value (paper=False)
    -- {'price','shares','confirm_days','vol_gate','vol_pctile','gap_start'}.
    Sizing uses buy_order_sizing's own worst-case-pad formula (matching every
    other real entry), NOT decision['shares'] directly (that's the naive
    starting_notional//price figure evaluate_drought_entry returns for the
    eligibility check itself, sizing-agnostic per D4)."""
    ticker = node['ticker']
    price = decision['price']
    # sig-like dict, reusing signals_blocks._build_buy_blocks so drought
    # doesn't need a parallel block builder (Part 4.2) -- drought has no real
    # z-score/lower_band (it's a checkpoint-bar/time-based entry, not a
    # z-score breakout), so z_score/lower_band are placeholders: lower_band=
    # price (a reasonable SL-price-display approximation -- the REAL broker
    # stop is anchored to pos['entry_price'] by _place_stop_loss_for_position
    # regardless of what's shown here), z_score/hurst/adf_p unused by any
    # downstream handler (verified by reading signals_handlers.py -- the
    # button JSON value's z_score/lower_band fields are never read back).
    sig_like = {'ticker': ticker, 'current_price': price, 'last_bar': datetime.now(),
                'lower_band': price, 'z_score': 0.0, 'hurst': None, 'adf_p': None}

    trailing_buy = db._is_trailing_buy(node)
    auto_placed = False
    order_id = None
    # D4: flat starting_notional, matching evaluate_drought_entry's own
    # sizing basis (generate_drought_trades is sizing-agnostic).
    target_notional = node.get('starting_notional') or 50000
    sizing = buy_order_sizing(node, sig_like, target_notional=target_notional)
    if trailing_buy:
        auto_placed, order_id = _attempt_automated_buy(node, sizing)
    elif ticker in schwab_safety.AUTOMATION_ENABLED_TICKERS:
        auto_placed, order_id = _attempt_automated_market_buy(node, sizing)

    # Sub-capital-at-stake nodes get zero real-time Slack (2026-08-08 user
    # call, same gate as notify_buy_signal) -- this call site was missed
    # when that redesign landed, found by 2026-08-09 paired review: 11 real
    # soxl_ira nodes ($500-$2,500) and 3 dry_run brokerage canary-drought
    # nodes have drought_overlay_enabled=1, all below the capital-at-stake
    # bar, and were getting real-time posts regardless. Tracking below
    # (pending_buy, automated placement) is unaffected -- only the Slack
    # post itself is skipped, matching notify_buy_signal's contract.
    if not should_alert_live(node):
        channel, ts = None, None
    else:
        channel, ts = _post_message(
            f"🌵 DROUGHT ENTRY SIGNAL — {ticker}  ${price:.4f}  "
            f"(confirm_days={decision['confirm_days']})",
            _build_buy_blocks(node, sig_like, auto_placed=auto_placed),
        )
    db.add_pending_buy(node, sig_like, channel, ts, order_id=order_id, position_source='drought_overlay',
                        drought_confirm_days=decision['confirm_days'], drought_vol_gate=decision['vol_gate'],
                        drought_gap_start=decision['gap_start'], drought_vol_pctile=decision['vol_pctile'])
    if auto_placed:
        if trailing_buy:
            db.mark_pending_buy_placed_by_wl_id(node['id'])
        else:
            _sync_confirm_and_protect(ticker, node, order_id)


def check_drought_entry(node):
    """Real-mode drought-overlay entry scan (Part 4.4). Exact mode-symmetry
    with paper_trading.check_paper_drought_entry (which returns early on
    mode=='live') -- this is its real-mode sibling, called from the SAME
    wiring sites (active_signals.py), never replacing the paper call (a
    research node's behavior must be unchanged)."""
    if node.get('state') == 'paper':
        return
    if not node.get('drought_overlay_enabled'):
        return
    if node['ticker'] not in schwab_safety.AUTOMATION_ENABLED_TICKERS:
        return
    if not schwab_safety.node_automation_enabled(node['id']):
        return
    if node.get('daily_sync_halted_at'):
        return
    decision = paper_trading.evaluate_drought_entry(node, paper=False)
    if decision is None:
        return
    # The once-per-gap dedup guard's own decision-layer event (Grid row
    # 'drought_entry') -- distinct from drought_entry_placement below, which
    # is the real order-placement outcome. Found 2026-08-13: this real path
    # computed the identical shared decision (evaluate_drought_entry) as
    # paper_trading.check_paper_drought_entry, but only paper's caller ever
    # logged "drought_entry" (paper_trading.py's own call) -- the real side
    # silently skipped straight to drought_entry_placement, so the Grid's
    # drought_entry row could never accumulate real fake_venue_proof (no
    # fake_broker test can assert an event this real code never logs).
    db.log_coverage_event("drought_entry", _coverage_mode(node.get('account')), ticker=node['ticker'],
                           node_id=node['id'], result="signalled",
                           detail=f"confirm_days={decision['confirm_days']} vol_pctile={decision['vol_pctile']}")
    notify_drought_buy_signal(node, decision)
    db.log_coverage_event("drought_entry_placement", _coverage_mode(node.get('account')), ticker=node['ticker'],
                           node_id=node['id'], result="signalled",
                           detail=f"confirm_days={decision['confirm_days']} vol_pctile={decision['vol_pctile']}")


def check_drought_handoff(node):
    """Real-mode drought HANDOFF (Part 5). Closes an open real drought-overlay
    position (or cancels a still-resting drought entry order) the moment this
    node's own core signal fires again -- exact mode-symmetry with
    paper_trading.check_paper_drought_handoff, real's three-state twin
    (paper's HANDOFF is a synchronous DB write; real has a resting-order
    cancel race and an unconfirmed-fill window paper never has). Mirrors
    check_entry_abandon's cancel logic for Case A rather than writing fresh,
    and _attempt_automated_exit_sell for Case B rather than a parallel exit
    path (docs/plans/real_order_execution_drought_addon.md 5.1-5.4)."""
    if node.get('state') == 'paper':
        return
    if not node.get('drought_overlay_enabled'):
        return
    wl_id, ticker = node['id'], node['ticker']
    pending = db.get_drought_pending_buy(wl_id)
    pos = db.get_drought_overlay_position(wl_id, paper=False)
    if pending is None and pos is None:
        return
    sig = compute.compute_buy_signal(node)
    # Verbatim guard from check_paper_drought_handoff's CRITICAL 2026-08-09
    # fix -- compute_buy_signal returns a real dict (signal='HOLD') on almost
    # every poll; None only on a genuine data failure, never as "no signal."
    if sig is None or sig['signal'] != 'BUY':
        return
    account = node.get('account')
    limits = schwab_safety.ACCOUNTS.get(account) if account else None
    mode = _coverage_mode(account)

    # Case A: drought entry order still resting, unfilled.
    if pending is not None:
        order_id = pending.get('order_id')
        if limits is not None and not _effectively_dry_run(account, node) and pending.get('order_placed') and not order_id:
            # Manual placement, no id on file -- alert for manual cancel, do
            # NOT clear the row (mirrors check_entry_abandon's identical case).
            db.log_coverage_event("drought_handoff_cancel", mode, ticker=ticker, node_id=wl_id,
                                   result="no_order_id_on_file")
            _post_message(f"⚠️ *{ticker}* ({account} · {mode_tag(account, node)}) — core signal fired again but "
                          f"the resting drought entry order has no broker id on file (placed manually) — "
                          f"cannot auto-cancel. Cancel it manually if still resting.")
            return
        if order_id and limits is not None and not _effectively_dry_run(account, node):
            try:
                _, status = schwab_client.cancel_order(account, ticker, order_id)
            except Exception as e:
                db.log_coverage_event("drought_handoff_cancel", mode, ticker=ticker, node_id=wl_id,
                                       result="cancel_failed", detail=str(e))
                _post_message(f"⚠️ *{ticker}* ({account} · {mode_tag(account, node)}) — drought HANDOFF cancel "
                              f"request failed ({e}) — resting order may still be live, verify manually.")
                return
            if status == 'FILLED':
                # Raced a real fill landing the instant we tried to cancel --
                # reconcile it as a genuine drought fill (never discard real
                # broker truth), then fall through to Case B this same poll.
                db.log_coverage_event("drought_handoff_cancel", mode, ticker=ticker, node_id=wl_id,
                                       result="raced_fill")
                fill = schwab_client.get_filled_order(account, ticker, 'BUY', order_id=order_id)
                if fill is None:
                    _post_message(f"⚠️ *{ticker}* ({account} · {mode_tag(account, node)}) — drought HANDOFF cancel "
                                  f"found status FILLED but the fill lookup itself failed — verify and "
                                  f"reconcile manually.")
                    return
                _reconcile_buy_fill(ticker, fill['price'], fill['quantity'], wl_id=wl_id, account=account)
                pos = db.get_drought_overlay_position(wl_id, paper=False)
                if pos is None:
                    return
                # falls through to Case B below
            elif status != 'CANCELED':
                # Unconfirmed cancel -- fail closed: leave the local row in
                # place and retry next poll, never discard tracking of a real
                # order that may still be resting.
                db.log_coverage_event("drought_handoff_cancel", mode, ticker=ticker, node_id=wl_id,
                                       result="cancel_unconfirmed")
                return
            else:
                db.clear_pending_buy_by_wl_id(wl_id)
                db.log_coverage_event("drought_handoff_cancel", mode, ticker=ticker, node_id=wl_id,
                                       result="cancelled_resting_entry")
                _post_message(f"🔁 *{ticker}* ({account} · {mode_tag(account, node)}) — core signal fired again, "
                              f"resting drought entry order cancelled before it filled.")
                return
        else:
            # dry_run account, or order_id is None with nothing real ever
            # placed -- nothing real to cancel, safe to clear.
            db.clear_pending_buy_by_wl_id(wl_id)
            db.log_coverage_event("drought_handoff_cancel", mode, ticker=ticker, node_id=wl_id,
                                   result="cancelled_resting_entry_no_real_order")
            return

    if pos is None:
        return

    # Case B: drought position filled and open -- real market SELL,
    # reason='HANDOFF'. Strongly prefer _attempt_automated_exit_sell over a
    # parallel exit path -- it already resolves the resting order id, uses
    # the atomic replace_equity_order_with_market, repoints sl_order_id,
    # clears broker_stop_price.
    price = sig['current_price']
    order_id = _attempt_automated_exit_sell(pos, 'HANDOFF', price)
    if order_id is None:
        db.log_coverage_event("drought_handoff_exit_placement", mode, ticker=ticker, node_id=wl_id,
                               result="failed_or_blocked")
        _post_message(f"⚠️ *{ticker}* ({account} · {mode_tag(account, node)}) — core signal fired again but the "
                      f"real drought HANDOFF exit order could not be placed automatically — verify and "
                      f"close the drought position manually.")
        return
    # The DB row must NOT close until the fill is confirmed -- follow
    # notify_sell_signal's own poll pattern. Only close on a confirmed fill;
    # otherwise persist trail_state['exit_pending'] and let
    # check_own_sell_fills/check_auto_fills close it on a later poll. This is
    # the key structural difference from paper (paper's HANDOFF is a
    # synchronous DB write with no fill-confirmation step).
    filled = None
    account_for_poll = account
    for _ in range(_GAP_FILL_POLL_ATTEMPTS):
        filled = schwab_client.get_filled_order(account_for_poll, ticker, 'SELL', order_id=order_id)
        if filled is not None:
            break
        time.sleep(_GAP_FILL_POLL_INTERVAL_SECS)
    if filled is None:
        fresh = db.get_position_by_id(pos['id']) or pos
        state = dict(fresh.get('trail_state') or {})
        state['exit_pending'] = {'reason': 'HANDOFF', 'order_id': order_id,
                                  'placed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        db.update_position_trail_state(pos['id'], state)
        db.log_coverage_event("drought_handoff_exit_placement", mode, ticker=ticker, node_id=wl_id,
                               result="placed_unconfirmed", detail=f"order_id={order_id}")
        _post_message(f"🔁 *{ticker}* ({account} · {mode_tag(account, node)}) — drought HANDOFF exit order placed, "
                      f"waiting for fill confirmation.")
        return
    db.close_position(pos['id'], exit_signal_price=price, exit_price=filled['price'], exit_time=datetime.now(),
                       exit_reason='HANDOFF', paper=False)
    db.log_coverage_event("drought_handoff", mode, ticker=ticker, node_id=wl_id, result="closed",
                           detail=f"price={filled['price']:.4f}")
    _post_message(f"🔁 *{ticker}* ({account} · {mode_tag(account, node)}) — drought HANDOFF: closed @ "
                  f"${filled['price']:.4f}, core signal active again")


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


def _exit_order_resting(pos, reason, order_id):
    """Tri-state: True (a real order is confirmed genuinely still open/
    resting at the broker), False (broker confirms it's not -- terminal in
    any sense: rejected, canceled, expired, filled, or replaced), None (no
    order_id to check, or the status check itself was unconfirmed). Callers
    must treat None the same as False for alert purposes -- fail toward the
    cautious "may be unmanaged" message, not the reassuring one.

    Fixes a real gap an Opus review found in the first version of this
    change, 2026-08-01: a stored order_id's mere presence used to be trusted
    as proof of a resting order, indistinguishable from a REJECTED one (a
    real failure mode -- see LABD's rejected stop, 2026-07-31) -- now
    actually checked via schwab_client.get_order_status. Gated on
    schwab_safety._OPEN_ORDER_STATUSES_EXCLUDED (the same 5-status "not
    genuinely open" set the resting-order duplicate guard already uses), not
    schwab_client._ORDER_TERMINAL_BAD_STATUSES (only 3 statuses, built for a
    different question -- right-after-placement rejection detection, where
    FILLED is deliberately handled as its own separate case rather than
    folded into "bad"). Using the narrower set here would have reported
    'confirmed resting' for an order that had actually already FILLED or
    been REPLACED (a second review round caught this before it shipped).

    A second review round also found the original TRAIL fallback (trusting
    trail_state['order_placed'] when order_id is None, meant to cover
    _attempt_automated_sell's exit_order_id legitimately being None on a
    real automated placement success) was unsafe as written: order_placed is
    ALSO set by signals_handlers.handle_trail_order_placed, a MANUAL Slack
    button tap with no broker verification at all -- there's no stored
    provenance distinguishing which path set it, so trusting it here would
    reintroduce the exact 'trust an unverified flag instead of the broker'
    pattern this function exists to eliminate, just through a different
    door. Removed rather than fixed with a schema change under time
    pressure -- the narrow sub-case it targeted (unextractable order id on a
    real automated TRAIL placement) reverts to the pre-2026-08-01 cautious
    text, same as before this session; see docs/backlog_cache.md."""
    if order_id is None:
        return None
    account = pos.get('account')
    status = schwab_client.get_order_status(account, order_id)
    if status is None:
        return None
    return status not in schwab_safety._OPEN_ORDER_STATUSES_EXCLUDED


def notify_sell_signal(pos, reason, current_price, target_price):
    ticker     = pos['ticker']
    ep         = pos['entry_price']
    entry_time = pos['entry_time']
    pct        = (current_price - ep) / ep * 100

    if reason == 'TIME':
        # Split 2026-08-13: hold-time expiry while never-armed and hold-time expiry
        # while armed (exit_forced_by_hold_time) are two genuinely different code
        # paths that used to share one scenario_key, masking that the armed
        # sub-case had zero live confirmation under current code (SH's only closed
        # trade predates the 2026-08-01 exit_reason labeling fix).
        hold_time_armed = bool((pos.get('trail_state') or {}).get('exit_forced_by_hold_time'))
        time_scenario_key = "time_exit_trigger_armed" if hold_time_armed else "time_exit_trigger_unarmed"
        db.log_coverage_event(time_scenario_key, _coverage_mode(pos.get('account')), ticker=ticker,
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
        # Lockstep leg close BEFORE db.close_position -- see that call's own
        # comment in check_dry_run_sim_sells for why (CRITICAL fix). A leg
        # already closed by a racing path is a safe no-op (get_open_addon_
        # leg_by_parent returns None); a genuine concurrent-close race
        # against the SAME leg is a narrow, accepted residual risk, same
        # class as _submit_replace_with_retry's documented ambiguity --
        # not fully solved here.
        try:
            close_addon_leg_real_if_open(pos, filled['price'], reason, datetime.now())
        except Exception as e:
            print(f"  [warn] {ticker} — unexpected error in close_addon_leg_real_if_open: {e}")
        closed = db.close_position(pos['id'], exit_signal_price=current_price, exit_price=filled['price'],
                                    exit_time=datetime.now(), exit_reason=reason)
        if closed:
            db.log_coverage_event("automated_exit_confirmed", _coverage_mode(pos.get('account')), ticker=ticker,
                                   position_id=pos.get('id'), node_id=pos.get('wl_id'), result="closed",
                                   detail=f"reason={reason} price={filled['price']:.4f}")
            # _node is None fails toward ALERTING, not muting -- a deleted/
            # unresolvable node behind a still-real, still-open position
            # (the documented EDC/UDOW pattern: node removed or reconfigured
            # while a real position stays open) must not silently lose its
            # only alerts. Found by paired Opus review, 2026-08-08.
            _node = db.get_watch_list_node_by_id(pos.get('wl_id'))
            if _node is None or has_capital_at_stake(_node):
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

    resting = _exit_order_resting(pos, reason, order_id)

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
    existing_exit_pending = state.get('exit_pending')

    # Routine-wait suppression (2026-08-02): a TRAIL exit with a confirmed-
    # still-resting automated order is expected behavior -- the broker will
    # fill it on its own, there's no action to take, and notify_trailing_
    # activated already told the user this order is live at arm time. Posting
    # a second "SELL SIGNAL" alert here that just repeats "still resting, no
    # action needed" trains the user to ignore these.
    #
    # _trail_alert_should_post_now guards against a real gap a cold review
    # caught 2026-08-02: check_exit_reminders only runs 9:00-16:00
    # (active_signals._reminders_active) -- suppressing this near that
    # window's close, or outside it entirely, could leave a real open
    # position with ZERO Slack visibility until reminders resume at 9:00 the
    # next trading day (confirmed: up to ~17.75h), instead of the intended
    # ~45min-max escalation window. Fails toward posting.
    #
    # already_tracked guards a second real gap the same review found: if this
    # exact still-resting TRAIL exit already has an exit_pending row (this is
    # a re-fire on a new bar close, sell_alerted dedups per-bar not
    # across-bars, so the SELL condition gets rechecked and this function
    # re-entered every hour it stays unresolved), overwriting exit_pending
    # below would reset reminder_count/last_reminder_at back to 0 every hour,
    # silently discarding whatever escalation progress check_exit_reminders
    # had already made -- in the worst case (a reminder cadence that divides
    # evenly into the bar interval) preventing the reminder_num>=3 escalation
    # from ever completing. When already tracked, skip touching trail_state
    # entirely and let check_exit_reminders own the escalation clock.
    already_tracked = (
        existing_exit_pending is not None
        and existing_exit_pending.get('reason') == reason
        and existing_exit_pending.get('order_id') == order_id
    )
    suppress = reason == 'TRAIL' and resting is True and not _trail_alert_should_post_now()
    if suppress and already_tracked:
        print(f"  TRAIL exit routine (already tracked, resting) -- alert suppressed, state preserved")
        return
    # Capital-at-stake gate (2026-08-08, extended after the user's explicit
    # confirmation: "I'm not going to do anything anyway on these smaller
    # positions" -- automation is the real protection for a sub-threshold
    # node regardless, so this manual-confirmation alert is pointless noise
    # for it, same as everything else muted tonight). Checked BEFORE the
    # routine-wait suppress logic below, which exists to solve a different
    # problem (don't go silent near the reminder-window cutoff) that only
    # matters for a position the user will actually act on.
    # _node is None fails toward ALERTING, not muting -- see the identical
    # rationale above (auto-closed branch) and the paired Opus review finding
    # that landed it, 2026-08-08.
    _node = db.get_watch_list_node_by_id(pos.get('wl_id'))
    alert_eligible = _node is None or has_capital_at_stake(_node)
    if not alert_eligible:
        channel, ts = None, None
    elif suppress:
        channel, ts = None, None
        print(f"  TRAIL exit routine (order already resting) -- alert suppressed")
    else:
        channel, ts = _post_message(
            f"SELL SIGNAL — {ticker}  {reason_labels[reason]}  ${current_price:.4f}  ({pct:+.2f}%)",
            _build_sell_blocks(pos, reason, current_price, target_price, resting_confirmed=resting is True),
        )

    # Tracks the exit as unresolved until Exited/Skipped -- unlike a placed trailing-buy
    # (waiting on a broker fill we can't detect), a stalled SELL confirmation means an
    # already-open position with real capital sitting unmanaged, arguably more urgent to
    # nag about than the buy side. order_id (None if out of automation scope) lets
    # check_own_sell_fills keep rechecking the exact real order every poll cycle and
    # auto-close the moment it's confirmed FILLED -- the manual tap below is only the
    # fallback path, not the sole way this ever resolves.
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
            try:
                close_addon_leg_real_if_open(pos, exit_price, reason, datetime.now())
            except Exception as e:
                print(f"  [warn] {ticker} — unexpected error in close_addon_leg_real_if_open: {e}")
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
    # Capital-at-stake gate (2026-08-08, extended to the manual-placement
    # branch after the user's explicit confirmation: "I'm not going to do
    # anything anyway on these smaller positions" -- automation is the real
    # protection for a sub-threshold node regardless, so even the "place a
    # real order" prompt is pointless noise for it). Node's static
    # definition (has_capital_at_stake), not the position's own dollar
    # exposure -- one consistent definition of "capital at stake"
    # everywhere. dry_run is covered too (effectively_dry_run inside
    # has_capital_at_stake always returns False for it).
    # _node is None fails toward ALERTING, not muting -- see notify_sell_
    # signal's identical fix and rationale, 2026-08-08 paired Opus review.
    _node = db.get_watch_list_node_by_id(pos.get('wl_id'))
    if not (_node is None or has_capital_at_stake(_node)):
        channel, ts = None, None
    elif auto_placed:
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
    # Real add-on-at-arm trigger (Part 6.1) -- called at the very END, wrapped
    # in a never-re-raising try/except (precedented reasoning at
    # paper_trading.py's own check_paper_addon_trigger call site: an
    # independent review found running the follow-on before the core event's
    # own observability could lose the core arm's coverage event/alert
    # entirely). Re-reads the position fresh internally rather than trusting
    # `pos` (the caller's pre-arm snapshot) or `fresh` (this function's own
    # copy, now possibly stale after the trail_state write just above).
    try:
        check_addon_trigger_real(db.get_position_by_id(pos['id']) or fresh, current_price)
    except Exception as e:
        print(f"  [warn] {ticker} — unexpected error in check_addon_trigger_real: {e}")


def check_addon_trigger_real(pos, current_price):
    """Real margin add-on-at-arm entry (Part 6, docs/plans/
    real_order_execution_drought_addon.md). Places a real MARKET BUY for
    exactly pos['shares'] against the core position's own resting protective
    SELL -- schwab_safety.check_order's is_addon_leg exemption is what makes
    this reachable at all (see check_order's docstring for the five verified
    preconditions). Every guard here duplicates part of that gate deliberately
    -- a clean Slack message + a named coverage event for the common
    skip/refuse cases, with check_order as the hard, non-bypassable backstop
    regardless of what happens here."""
    if pos is None:
        return
    node = db.get_watch_list_node_by_id(pos.get('wl_id'))
    if node is None or node.get('state') == 'paper':
        return
    if not node.get('addon_enabled'):
        return
    if pos.get('position_source') != 'core':
        return
    if db.get_open_addon_leg_by_parent(pos['id']) is not None:
        return
    ticker = pos['ticker']
    if ticker not in schwab_safety.AUTOMATION_ENABLED_TICKERS:
        return
    if not schwab_safety.node_automation_enabled(pos.get('wl_id')):
        return
    account = pos.get('account')
    _mode = _coverage_mode(account)
    limits = schwab_safety.ACCOUNTS.get(account)
    if limits is None or not limits.margin_capable:
        db.log_coverage_event("addon_entry_placement", _mode, ticker=ticker, position_id=pos.get('id'),
                               node_id=pos.get('wl_id'), result="blocked_non_margin_account",
                               detail=f"account={account!r}")
        _post_message(f"⚠️ *{ticker}* ({account} · {mode_tag(account, node)}) — add-on skipped: "
                      f"'{account}' is not margin-capable")
        return
    shares = int(pos['shares'])
    if shares < 1:
        return
    if pos.get('is_dry_run_sim'):
        # Mirrors update_dry_run_buys/_fill_dry_run_buy's synthesis
        # convention -- a dry_run-sim parent has no real broker order to
        # mirror, so the leg is synthesized filled immediately against real
        # price data, same as the parent's own entry/exit.
        leg_id = db.open_addon_leg(pos, shares=shares, entry_price=current_price, entry_time=datetime.now(),
                                    paper=False, is_dry_run_sim=True, entry_order_id=None, entry_status='filled')
        db.log_coverage_event("addon_entry_fill", _mode, ticker=ticker, position_id=pos.get('id'),
                               node_id=pos.get('wl_id'), result="dry_run_sim_filled",
                               detail=f"leg_id={leg_id} shares={shares} price={current_price:.4f}")
        _post_message(f"🧪 *{ticker}* ({account} · {mode_tag(account, node)}) — add-on leg synthesized (dry_run_sim): "
                      f"{shares}sh @ ${current_price:.4f}")
        return
    try:
        _, order_id = schwab_client.place_equity_buy(account, ticker, shares, current_price, is_addon_leg=True,
                                                       node_dry_run=(node.get('state') != 'live'),
                                                       node_id=pos.get('wl_id'))
    except schwab_safety.SafetyViolation as e:
        db.log_coverage_event("addon_entry_placement", _mode, ticker=ticker, position_id=pos.get('id'),
                               node_id=pos.get('wl_id'), result="blocked", detail=str(e))
        return
    except Exception as e:
        db.log_coverage_event("addon_entry_placement", _mode, ticker=ticker, position_id=pos.get('id'),
                               node_id=pos.get('wl_id'), result="failed_unexpectedly", detail=str(e))
        _post_message(f"⚠️ *{ticker}* ({account} · {mode_tag(account, node)}) — add-on leg placement failed "
                      f"unexpectedly: {e}")
        return
    if order_id is None:
        # dry_run ACCOUNT (distinct from is_dry_run_sim above, which is a
        # per-position tag) -- schwab_client's approve_and_record already
        # ran every real check_order guard (including is_addon_leg's five
        # preconditions) and only short-circuited the actual broker call, so
        # this is a genuine successful dry-run, not a failure (a real
        # failure would have raised above, never returned (None, None)). A
        # dry_run order produces no real fill event to ever poll for, so it
        # must be synthesized immediately -- found by contextual Opus review
        # before this shipped: without this, the leg is written
        # entry_status='placed' with entry_order_id=None, can never confirm
        # filled, and check_addon_leg_reconciliation's timeout branch itself
        # requires entry_order_id to attempt a cancel -- the leg is
        # permanently stuck. This also blocks Part 12's mandatory full
        # dry_run rehearsal cycle on 'brokerage' (margin + dry_run) from ever
        # exercising fill confirmation, D3 stop placement, or lockstep exit.
        leg_id = db.open_addon_leg(pos, shares=shares, entry_price=current_price, entry_time=datetime.now(),
                                    paper=False, entry_order_id=None, entry_status='filled')
        db.log_coverage_event("addon_entry_fill", _mode, ticker=ticker, position_id=pos.get('id'),
                               node_id=pos.get('wl_id'), result="dry_run_filled",
                               detail=f"leg_id={leg_id} shares={shares} price={current_price:.4f}")
        _place_stop_loss_for_addon_leg(leg_id, pos, node)
        return
    leg_id = db.open_addon_leg(pos, shares=shares, entry_price=current_price, entry_time=datetime.now(),
                                paper=False, entry_order_id=order_id, entry_status='placed')
    db.log_coverage_event("addon_entry_placement", _mode, ticker=ticker, position_id=pos.get('id'),
                           node_id=pos.get('wl_id'), result="placed",
                           detail=f"leg_id={leg_id} order_id={order_id} shares={shares}")
    _post_message(f"➕ *{ticker}* ({account} · {mode_tag(account, node)}) — ADD-ON leg order placed: "
                  f"{shares}sh @ ~${current_price:.4f}")
    # Fill confirmation (Part 6.2) -- market order, fills near-immediately.
    # Follows _sync_confirm_and_protect's pattern: short synchronous poll,
    # then confirm. Does NOT route through _reconcile_buy_fill (pending_buys-
    # driven, would try to open an open_positions row -- exactly what the
    # separate addon_legs table exists to prevent).
    filled = None
    for _ in range(_SL_FAST_CONFIRM_ATTEMPTS):
        filled = schwab_client.get_filled_order(account, ticker, 'BUY', order_id=order_id)
        if filled is not None:
            break
        time.sleep(_SL_FAST_CONFIRM_INTERVAL_SECS)
    if filled is None:
        db.log_coverage_event("addon_entry_fill", _mode, ticker=ticker, position_id=pos.get('id'),
                               node_id=pos.get('wl_id'), result="unconfirmed", detail=f"leg_id={leg_id}")
        _post_message(f"⏱️ *{ticker}* ({account} · {mode_tag(account, node)}) — add-on leg order placed but not "
                      f"yet confirmed filled — check_addon_leg_reconciliation will retry.")
        return
    db.set_addon_leg_entry_filled(leg_id, filled['price'])
    db.log_coverage_event("addon_entry_fill", _mode, ticker=ticker, position_id=pos.get('id'),
                           node_id=pos.get('wl_id'), result="filled",
                           detail=f"leg_id={leg_id} price={filled['price']:.4f}")
    _post_message(f"✅ *{ticker}* ({account} · {mode_tag(account, node)}) — add-on leg filled: "
                  f"{shares}sh @ ${filled['price']:.4f}")
    # D3: the leg gets its own broker-side protective stop, anchored to the
    # PARENT's entry_price * (1 - sl_pct/100) -- the leg has no independent
    # exit rule, it closes in lockstep with the parent (Part 7).
    _place_stop_loss_for_addon_leg(leg_id, pos, node)


def _place_stop_loss_for_addon_leg(leg_id, parent_pos, node):
    """D3: places a real resting STOP for the add-on leg, anchored to the
    PARENT's entry_price (not the leg's own fill price -- the leg has no
    independent exit rule/model, its only job is to track the parent's real
    stop level so an unstopped margin position is never the failure mode).
    Mirrors _place_stop_loss_for_position's retry/fallback shape but against
    addon_legs instead of open_positions."""
    ticker = parent_pos['ticker']
    account = parent_pos.get('account')
    _mode = _coverage_mode(account)
    sl_pct = parent_pos.get('fixed_sl') if strategies.uses_fixed_sl(parent_pos['strategy']) else parent_pos.get('stop_loss')
    if not sl_pct:
        return
    stop_price = parent_pos['entry_price'] * (1 - sl_pct / 100)
    leg = db.get_open_addon_leg_by_parent(parent_pos['id'])
    if leg is None or not leg.get('shares'):
        return
    try:
        _, order_id = schwab_client.place_stop_loss(account, ticker, int(leg['shares']), stop_price,
                                                      is_addon_leg=True, node_dry_run=(node.get('state') != 'live'),
                                                      node_id=node.get('id'))
    except Exception as e:
        db.log_coverage_event("sl_placement", _mode, ticker=ticker, position_id=parent_pos.get('id'),
                               node_id=parent_pos.get('wl_id'), result="failed", detail=f"addon_leg={leg_id}: {e}")
        _post_message(f"🚨 *{ticker}* ({account} · {mode_tag(account, node)}) ADD-ON LEG UNPROTECTED — "
                      f"stop-loss placement failed: {e} (place a stop-loss SELL {int(leg['shares'])} shares "
                      f"@ ~${stop_price:.2f} manually)")
        return
    # order_id is None for a genuine dry_run account (schwab_client already
    # ran every real guard and only short-circuited the broker call, same
    # reasoning as check_addon_trigger_real's dry_run branch) -- recording
    # broker_stop_price in that case would falsely claim a real broker stop
    # exists, the same gap _place_stop_loss_for_position guards against
    # (fixed 2026-08-01, mirrored here).
    db.set_addon_leg_sl_order_id(leg_id, order_id, broker_stop_price=stop_price if order_id is not None else None)
    db.log_coverage_event("sl_placement", _mode, ticker=ticker, position_id=parent_pos.get('id'),
                           node_id=parent_pos.get('wl_id'),
                           result="placed" if order_id is not None else "placed_dry_run",
                           detail=f"addon_leg={leg_id}")


_ADDON_LEG_ENTRY_TIMEOUT_MINUTES = 10


def check_addon_leg_reconciliation(open_positions):
    """New reconciliation sweep for real addon legs (Part 6.4), called each
    poll alongside check_own_sell_fills/check_auto_fills. Two independent
    checks, both pure detection/best-effort -- never a blocking gate:
    (1) a leg still entry_status='placed' past a timeout: poll for a late
    fill, else cancel and mark abandoned (mirrors check_entry_abandon's
    cancel-with-confirmation pattern).
    (2) an open leg whose parent position no longer exists but whose
    parent_trade_log_id's trade_log row is already closed: the real lockstep
    close (Part 7) was missed somewhere -- ALERT LOUDLY, do NOT auto-close at
    a guessed price (pure observation, matching reconcile_daily_track_nodes'
    stance)."""
    for leg in db.get_open_addon_legs(paper=False):
        ticker = leg['ticker']
        account = leg.get('account')
        mode = _coverage_mode(account)
        if leg.get('entry_status') == 'placed' and leg.get('entry_order_id'):
            placed_at = datetime.strptime(leg['entry_time'], '%Y-%m-%d %H:%M:%S')
            if (datetime.now() - placed_at).total_seconds() < _ADDON_LEG_ENTRY_TIMEOUT_MINUTES * 60:
                continue
            fill = schwab_client.get_filled_order(account, ticker, 'BUY', order_id=leg['entry_order_id'])
            if fill is not None:
                db.set_addon_leg_entry_filled(leg['id'], fill['price'])
                db.log_coverage_event("addon_entry_fill", mode, ticker=ticker, node_id=leg.get('wl_id'),
                                       result="filled_late_reconcile", detail=f"leg_id={leg['id']}")
                # HIGH fix (found by cold Opus review before this shipped):
                # the synchronous fast-confirm path in check_addon_trigger_
                # real is the ONLY other place a leg's D3 protective stop is
                # placed -- a fill confirmed only here (after that ~10s
                # window already gave up) would otherwise leave a real,
                # filled, margin leg with no stop and no further attempt to
                # add one.
                _parent_pos = db.get_position_by_id(leg['parent_position_id']) if leg.get('parent_position_id') else None
                _node = db.get_watch_list_node_by_id(leg.get('wl_id'))
                if _parent_pos is not None and _node is not None:
                    _place_stop_loss_for_addon_leg(leg['id'], _parent_pos, _node)
                continue
            limits = schwab_safety.ACCOUNTS.get(account)
            _leg_node = db.get_watch_list_node_by_id(leg.get('wl_id'))
            if limits is not None and not _effectively_dry_run(account, _leg_node):
                try:
                    _, status = schwab_client.cancel_order(account, ticker, leg['entry_order_id'])
                except Exception as e:
                    db.log_coverage_event("addon_leg_reconciliation", mode, ticker=ticker, node_id=leg.get('wl_id'),
                                           result="cancel_failed", detail=f"leg_id={leg['id']}: {e}")
                    continue
                if status == 'FILLED':
                    fill = schwab_client.get_filled_order(account, ticker, 'BUY', order_id=leg['entry_order_id'])
                    if fill is not None:
                        db.set_addon_leg_entry_filled(leg['id'], fill['price'])
                    continue
                if status != 'CANCELED':
                    db.log_coverage_event("addon_leg_reconciliation", mode, ticker=ticker, node_id=leg.get('wl_id'),
                                           result="cancel_unconfirmed", detail=f"leg_id={leg['id']}")
                    continue
            # close_addon_leg (not set_addon_leg_entry_filled) -- must mark
            # status='closed', not just entry_status='abandoned', or
            # get_open_addon_legs/get_open_addon_leg_by_parent would keep
            # seeing this row as open forever, permanently blocking a future
            # add-on for the same parent (found by this file's own fake_broker
            # test: the leg stayed in get_open_addon_legs() after the first
            # version of this fix, Part 8 review-before-ship).
            db.close_addon_leg(leg['id'], leg['entry_price'], datetime.now(), 'ABANDONED')
            db.log_coverage_event("addon_leg_reconciliation", mode, ticker=ticker, node_id=leg.get('wl_id'),
                                   result="abandoned", detail=f"leg_id={leg['id']}")
            _post_message(f"⏱️ *{ticker}* ({account} · {mode_tag(account, _leg_node)}) — add-on leg entry order "
                          f"({leg['id']}) never filled past {_ADDON_LEG_ENTRY_TIMEOUT_MINUTES}min — cancelled, "
                          f"marked abandoned. Never sold shares never bought.")
            continue

        # Same gap check_sl_order_fills exists to close for the core
        # position, one level down: a leg's own protective stop
        # (_place_stop_loss_for_addon_leg, leg['sl_order_id']) rests
        # continuously at the broker, independent of the parent's lockstep
        # exit signal -- it can fill on its own before that signal is ever
        # computed. The exit_order_id branch just below only covers an
        # order WE placed in response to an already-computed lockstep exit;
        # nothing else polls sl_order_id directly (found 2026-08-07, same
        # review pass as check_sl_order_fills -- identical shape to the
        # LABD incident, one level down).
        if (leg.get('status') == 'open' and not leg.get('exit_order_id')
                and leg.get('sl_order_id')):
            fill = schwab_client.get_filled_order(account, ticker, 'SELL', order_id=leg['sl_order_id'])
            if fill is not None:
                db.close_addon_leg(leg['id'], fill['price'], datetime.now(), 'SL_RECONCILED')
                db.log_coverage_event("addon_exit_fill", mode, ticker=ticker, node_id=leg.get('wl_id'),
                                       result="sl_closed_reconcile",
                                       detail=f"leg_id={leg['id']} price={fill['price']:.4f}")
                _leg_node = db.get_watch_list_node_by_id(leg.get('wl_id'))
                _post_message(f"🤖 *{ticker}* ({account} · {mode_tag(account, _leg_node)}) — add-on leg "
                              f"({leg['id']}) protective stop filled at ${fill['price']:.4f}, closed via "
                              f"reconciliation poll")
                continue

        # A real exit SELL was placed (close_addon_leg_real_if_open) but
        # wasn't confirmed within that call's short poll window -- pick up
        # the confirmation here on a later poll (HIGH fix, cold Opus review
        # before this shipped: exit_order_id used to be recorded nowhere,
        # so an unconfirmed exit had zero further tracking at all).
        if leg.get('exit_order_id') and leg.get('status') == 'open':
            fill = schwab_client.get_filled_order(account, ticker, 'SELL', order_id=leg['exit_order_id'])
            if fill is not None:
                db.close_addon_leg(leg['id'], fill['price'], datetime.now(), 'RECONCILED_EXIT')
                db.log_coverage_event("addon_exit_fill", mode, ticker=ticker, node_id=leg.get('wl_id'),
                                       result="closed_late_reconcile",
                                       detail=f"leg_id={leg['id']} price={fill['price']:.4f}")
            continue

        if leg.get('parent_position_id') is not None:
            _parent_still_open = db.get_position_by_id(leg['parent_position_id']) is not None
            if _parent_still_open:
                continue
            with db._conn() as c:
                _parent_closed = c.execute(
                    "SELECT 1 FROM trade_log WHERE id=? AND exit_time IS NOT NULL",
                    (leg['parent_trade_log_id'],)
                ).fetchone() is not None
            if _parent_closed:
                db.log_coverage_event("addon_leg_reconciliation", mode, ticker=ticker, node_id=leg.get('wl_id'),
                                       result="orphaned_leg_parent_closed", detail=f"leg_id={leg['id']}")
                _leg_node = db.get_watch_list_node_by_id(leg.get('wl_id'))
                _post_message(f"🚨 *{ticker}* ({account} · {mode_tag(account, _leg_node)}) — add-on leg ({leg['id']}) "
                              f"is still open but its parent core position has already closed — the real "
                              f"lockstep close was missed. Verify and close the leg manually; NOT auto-closed.")


def close_addon_leg_real_if_open(pos, exit_price, exit_reason, exit_time):
    """Real mirror of paper_trading.close_paper_addon_leg_if_open (Part 7.1) --
    places a real order first, then records the close. Never raises -- every
    one of the 7 real call sites (Part 7.2) wraps this in a never-re-raising
    try/except, called AFTER the core exit's own coverage event/alert.

    Divergence to document: paper closes the leg at the parent's EXACT exit
    price/reason. Real closes at the leg's own real fill price (slippage will
    differ) and at the exit_reason THIS call was given -- log both so
    reconciliation attributes the gap to slippage, not a logic bug."""
    if pos is None:
        return
    leg = db.get_open_addon_leg_by_parent(pos['id'])
    if leg is None:
        return
    ticker = pos['ticker']
    account = pos.get('account')
    mode = _coverage_mode(account)
    limits = schwab_safety.ACCOUNTS.get(account) if account else None
    node = db.get_watch_list_node_by_id(pos.get('wl_id'))

    if leg.get('entry_status') == 'placed':
        # Still resting, unfilled -- cancel it, never sell shares never
        # bought. A race to FILLED falls through to a real SELL instead.
        order_id = leg.get('entry_order_id')
        if limits is None:
            db.log_coverage_event("addon_exit_placement", mode, ticker=ticker, node_id=leg.get('wl_id'),
                                   result="unrecognized_account", detail=f"leg_id={leg['id']}")
            _post_message(f"⚠️ *{ticker}* ({account!r}) — add-on leg ({leg['id']}) core position closed but "
                          f"the account isn't recognized — cannot determine whether a real entry order needs "
                          f"cancelling. Verify manually.")
            return
        if order_id and not _effectively_dry_run(account, node):
            try:
                _, status = schwab_client.cancel_order(account, ticker, order_id)
            except Exception as e:
                db.log_coverage_event("addon_exit_placement", mode, ticker=ticker, node_id=leg.get('wl_id'),
                                       result="cancel_failed", detail=f"leg_id={leg['id']}: {e}")
                return
            if status == 'FILLED':
                fill = schwab_client.get_filled_order(account, ticker, 'BUY', order_id=order_id)
                if fill is not None:
                    db.set_addon_leg_entry_filled(leg['id'], fill['price'])
                    leg = dict(leg)
                    leg['entry_status'] = 'filled'
                    leg['entry_price'] = fill['price']
                # falls through to the real SELL branch below
            elif status != 'CANCELED':
                db.log_coverage_event("addon_exit_placement", mode, ticker=ticker, node_id=leg.get('wl_id'),
                                       result="cancel_unconfirmed", detail=f"leg_id={leg['id']}")
                return
            else:
                db.close_addon_leg(leg['id'], leg['entry_price'], exit_time, 'ABANDONED')
                db.log_coverage_event("addon_exit_placement", mode, ticker=ticker, node_id=leg.get('wl_id'),
                                       result="cancelled_unfilled_leg", detail=f"leg_id={leg['id']}")
                return
        else:
            db.close_addon_leg(leg['id'], leg['entry_price'], exit_time, 'ABANDONED')
            db.log_coverage_event("addon_exit_placement", mode, ticker=ticker, node_id=leg.get('wl_id'),
                                   result="cancelled_unfilled_leg_no_real_order", detail=f"leg_id={leg['id']}")
            return

    # Leg is filled -- place/replace the real exit SELL, lockstep with the
    # parent core position's own exit.
    shares = int(leg['shares'])
    resting_order_id = leg.get('sl_order_id')
    # is_addon_leg=True: this is the LEG's own SELL, not the parent's --
    # without it, check_order's ordinary resting-SELL dup guard sees the
    # parent core position's own resting protective order (a real, separate,
    # wanted order) as a duplicate and refuses this placement (CRITICAL,
    # found by cold Opus review before this shipped).
    try:
        if resting_order_id:
            _, order_id = schwab_client.replace_equity_order_with_market(
                account, ticker, resting_order_id, "SELL", shares, exit_price, is_addon_leg=True,
                node_dry_run=(node.get('state') != 'live') if node else True, node_id=leg.get('wl_id'))
        else:
            _, order_id = schwab_client.place_equity_sell(account, ticker, shares, exit_price, is_addon_leg=True,
                                                            node_dry_run=(node.get('state') != 'live') if node else True,
                                                            node_id=leg.get('wl_id'))
    except Exception as e:
        db.log_coverage_event("addon_exit_placement", mode, ticker=ticker, node_id=leg.get('wl_id'),
                               result="failed", detail=f"leg_id={leg['id']}: {e}")
        _post_message(f"🚨 *{ticker}* ({account} · {mode_tag(account, node)}) — add-on leg ({leg['id']}) exit SELL "
                      f"failed: {e} — verify and close manually.")
        return
    if order_id is None:
        # dry_run account (or an is_dry_run_sim leg, which is always on a
        # dry_run account) -- a genuine successful dry-run produces no real
        # fill event to ever poll for, so it must be synthesized immediately
        # at the given exit_price, mirroring the entry-side fix above (found
        # by contextual Opus review before this shipped: without this, an
        # is_dry_run_sim/dry_run-account leg polls get_filled_order(order_id=
        # None) forever and stays open permanently).
        db.close_addon_leg(leg['id'], exit_price, exit_time, exit_reason)
        db.log_coverage_event("addon_exit_fill", mode, ticker=ticker, node_id=leg.get('wl_id'),
                               result="dry_run_closed",
                               detail=f"leg_id={leg['id']} price={exit_price:.4f} reason={exit_reason}")
        return
    filled = None
    for _ in range(_GAP_FILL_POLL_ATTEMPTS):
        filled = schwab_client.get_filled_order(account, ticker, 'SELL', order_id=order_id)
        if filled is not None:
            break
        time.sleep(_GAP_FILL_POLL_INTERVAL_SECS)
    if filled is None:
        # Persist order_id so check_addon_leg_reconciliation can revisit and
        # close it on a later poll -- HIGH fix (cold Opus review before this
        # shipped): set_addon_leg_exit_order_id existed but was never
        # actually called anywhere, so an exit order unconfirmed within this
        # short poll window had zero further tracking -- a real SELL live at
        # the broker with no local record, and the leg row stuck open.
        db.set_addon_leg_exit_order_id(leg['id'], order_id)
        db.log_coverage_event("addon_exit_placement", mode, ticker=ticker, node_id=leg.get('wl_id'),
                               result="placed_unconfirmed", detail=f"leg_id={leg['id']} order_id={order_id}")
        _post_message(f"🔁 *{ticker}* ({account} · {mode_tag(account, node)}) — add-on leg ({leg['id']}) exit order "
                      f"placed, waiting for fill confirmation.")
        return
    db.close_addon_leg(leg['id'], filled['price'], exit_time, exit_reason)
    db.log_coverage_event("addon_exit_fill", mode, ticker=ticker, node_id=leg.get('wl_id'), result="closed",
                           detail=f"leg_id={leg['id']} leg_price={filled['price']:.4f} reason={exit_reason} "
                                  f"parent_exit_price={exit_price:.4f}")
    _post_message(f"✅ *{ticker}* ({account} · {mode_tag(account, node)}) — add-on leg ({leg['id']}) closed @ "
                  f"${filled['price']:.4f} ({exit_reason}, lockstep with parent)")


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
        # Sub-capital-at-stake nodes get zero real-time Slack, including this
        # reminder loop -- found missing by paired Opus review, 2026-08-08.
        # _node is None fails toward ALERTING (same rationale as
        # notify_trailing_activated's identical fix), not muting.
        _node = db.get_watch_list_node_by_id(pos.get('wl_id'))
        if _node is not None and not has_capital_at_stake(_node):
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


def _trail_alert_should_post_now(now=None):
    """Whether time-of-day makes it unsafe to suppress the routine TRAIL
    still-resting alert. check_exit_reminders (the escalation path this
    suppression relies on) only runs 9:00-16:00 (active_signals.
    _reminders_active) -- outside that window, or too close to its 16:00
    cutoff for reminder_num to reach the 3-cycle escalation threshold
    (3 x EXIT_REMINDER_MINUTES) before it goes quiet for the night,
    suppressing here would leave a real open position with zero Slack
    visibility until reminders resume at 9:00 the next trading day (found by
    review, 2026-08-02: up to ~17.75h of silence). Fails toward posting."""
    now = now or datetime.now()
    if not (9, 0) <= (now.hour, now.minute) <= (16, 0):
        return True
    minutes_until_cutoff = (16 * 60) - (now.hour * 60 + now.minute)
    return minutes_until_cutoff < 3 * EXIT_REMINDER_MINUTES


def _exit_pending_blocks(pos, exit_pending, reminder_num):
    """Mirrors _trailing_order_blocks for the sell side. A stalled SELL
    confirmation means an already-open position with real capital sitting
    unmanaged -- arguably more urgent than a stalled BUY, so this reuses the
    same 'Exited'/'Skipped' buttons (sell_exited/sell_skipped) as the original
    alert rather than inventing new action_ids."""
    ticker        = pos['ticker']
    account       = pos.get('account') or 'unmapped'
    _node         = db.get_watch_list_node_by_id(pos.get('wl_id'))
    ep            = pos['entry_price']
    reason        = exit_pending['reason']
    current_price = exit_pending['current_price']
    target_price  = exit_pending['target_price']
    pct           = (current_price - ep) / ep * 100
    reason_labels = {'TP': 'TAKE PROFIT', 'SL': 'STOP LOSS', 'TIME': 'TIME EXIT', 'TRAIL': 'TRAILING STOP'}

    if reason == 'SL':
        status, bsp = stop_status(pos)
        if status == 'known':
            status_line = (
                f"Protected by broker stop-loss on file @ `${bsp:.2f}` — should auto-fill there without "
                f"action from you. Confirm here once you see the fill in your account."
            )
        elif status == 'automation-pending':
            status_line = (
                f"⚠️ No broker stop on file — this may be a placement failure. Position may still be "
                f"open and unmanaged. Confirm Exited with the real fill price, or Skip if the exit "
                f"condition no longer applies."
            )
        elif status == 'dry-run':
            status_line = (
                f"Dry-run account — no real stop was ever placed. Confirm Exited with the real fill "
                f"price, or Skip if the exit condition no longer applies."
            )
        else:
            status_line = (
                f"No automated stop tracked for this position (manual). Confirm Exited with the real "
                f"fill price, or Skip if the exit condition no longer applies."
            )
    else:
        # Fresh check every time this fires, not a trust of the order_id
        # stored when the original alert was built -- that id's PRESENCE
        # doesn't prove the order is still resting (a REJECTED/CANCELED
        # order looks identical by id alone), and this is exactly the
        # repeated-reminder path where trusting a stale assumption is most
        # dangerous (Opus review, 2026-08-01: the first version of this
        # reminder dropped the "unless this persists" hedge the original
        # alert had, making the MOST reassuring message land on the MOST
        # suspicious path -- a real order that's been resting long enough to
        # trigger 2+ reminders and still hasn't filled).
        resting = _exit_order_resting(pos, reason, exit_pending.get('order_id'))
        if resting and reminder_num >= 3:
            # Escalated: routine "just waiting" stops being the likely
            # explanation once a real order has rested through several
            # reminder cycles (45+ min at the default 15-min cadence) without
            # filling -- distinguishes "nothing wrong, no action" from
            # "should have filled by now and hasn't," the original 2026-07-28
            # design intent for this case, not just wording.
            status_line = (
                f"⚠️ Automated exit order still resting after {reminder_num} reminders — should have "
                f"filled by now. Worth a look at the broker directly. Confirm Exited if you see a real "
                f"fill, or Skip if the exit condition no longer applies."
            )
        elif resting:
            status_line = (
                f"🤖 Automated exit order resting @ `${target_price:.2f}` — should fill shortly. Confirm "
                f"Exited only if you've independently verified a real fill, or Skip if the exit condition "
                f"no longer applies."
            )
        else:
            status_line = (
                f"Position may still be open and unmanaged at the broker. Confirm Exited with the real fill "
                f"price, or Skip if it turns out the exit condition no longer applies."
            )
    text = (
        f"⚠️ *{ticker}* ({account} · {mode_tag(account, _node)}) — EXIT NOT CONFIRMED (reminder #{reminder_num})\n"
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
        try:
            close_addon_leg_real_if_open(pos, fill['price'], exit_pending['reason'], datetime.now())
        except Exception as e:
            print(f"  [warn] {ticker} — unexpected error in close_addon_leg_real_if_open: {e}")


def check_sl_order_fills(open_positions):
    """Every poll cycle, rechecks each open position's own resting protective
    stop-loss order (pos['sl_order_id'], placed at entry via
    _place_stop_loss_for_position and repointed on every replace -- always the
    real order currently resting at the broker, see set_sl_order_id_by_position
    call sites) for a FILLED status, independent of whether our own bar-close
    signal check has ever computed an exit condition for this position.

    Distinct from check_own_sell_fills, which only rechecks
    trail_state.exit_pending.order_id -- an order that only exists once OUR
    bar-close signal check has already fired and called
    _attempt_automated_exit_sell. A real stop-loss order is continuously
    monitored by the broker, not just at our discrete bar-close checks, so it
    can fill on its own well before any exit_pending is ever created. Without
    this poll, that fill went undetected until the reconciliation-mismatch
    check flagged it (detection-only, never closes the position) -- and every
    subsequent bar-close SL recheck then tried to replace the now-filled/
    terminal sl_order_id via _attempt_automated_exit_sell, which 400'd and
    posted a false "UNPROTECTED -- place a stop-loss manually" alert on an
    already-safely-closed position (real incident, LABD, 2026-08-07 -- stuck
    open 8+ hours, repeated false alerts, before this fix).

    sl_order_id is repointed to the trailing-sell order id at arm time
    (_attempt_automated_sell, line ~168) -- so this same poll also covers a
    trailing-sell that fires on its own before a bar-close check ever
    computes the TRAIL condition. exit_reason is derived from ORDER IDENTITY
    (sl_order_id == trail_state.exit_order_id), not state['trailing'] --
    trailing is persisted by check_sell_condition the moment a position arms,
    independent of whether _attempt_automated_sell's own placement actually
    succeeded (see that function's docstring): on a SafetyViolation, broker
    exception, paused node, non-live node, or a ticker outside
    AUTOMATION_ENABLED_TICKERS, sl_order_id is left pointing at the ORIGINAL
    entry stop while trailing is already True. Keying off state['trailing']
    would mislabel that stop's real SL fill as 'TRAIL' in trade_log (caught
    by a paired Opus review, 2026-08-07, both independently -- one rated it
    MEDIUM via the orderType angle, the other HIGH via this exact order-id
    argument, converged in rebuttal on this fix)."""
    for pos in open_positions:
        sl_order_id = pos.get('sl_order_id')
        if sl_order_id is None:
            continue
        state = pos.get('trail_state') or {}
        exit_pending = state.get('exit_pending') or {}
        if exit_pending.get('order_id') == sl_order_id:
            # Already covered by check_own_sell_fills/check_auto_fills's poll
            # of this exact order_id -- avoid a duplicate close/alert.
            continue
        account = pos.get('account')
        if not account:
            continue
        ticker = pos['ticker']
        fill = schwab_client.get_filled_order(account, ticker, 'SELL', order_id=sl_order_id)
        if fill is None:
            continue
        if state.get('exit_forced_by_hold_time'):
            reason = 'TIME'
        elif sl_order_id == state.get('exit_order_id'):
            reason = 'TRAIL'
        else:
            reason = 'SL'
        # Real, narrow residual gap (flagged in the same review, not fixed
        # here): fill['quantity'] isn't checked against pos['shares']. A
        # top_up_position failure or an addon leg sized independently of the
        # core stop could leave sl_order_id resting for fewer shares than
        # the local position tracks -- closing the WHOLE position on a
        # partial fill would understate real open exposure. Alert instead of
        # silently closing when the two disagree, matching the existing
        # top_up_position "UNDERSTATED" alert pattern rather than inventing
        # a new auto-correction.
        if fill['quantity'] != pos['shares']:
            _post_message(
                f"⚠️ *{ticker}* ({account}) SL/TRAIL order {sl_order_id} filled for "
                f"{fill['quantity']} shares but open_positions tracks {pos['shares']} -- "
                f"not auto-closing, needs manual reconciliation"
            )
            db.log_coverage_event("automated_exit_confirmed", _coverage_mode(account), ticker=ticker,
                                  position_id=pos.get('id'), node_id=pos.get('wl_id'), result="qty_mismatch",
                                  detail=f"fill_qty={fill['quantity']} pos_shares={pos['shares']}")
            continue
        actual_pnl = (fill['price'] - pos['entry_price']) / pos['entry_price'] * 100
        # broker_stop_price (the real trigger level, set when the order was
        # placed/repointed) is a strictly better exit_signal_price than the
        # fill price itself -- preserves the signal-vs-fill slippage this
        # column exists to record, matching exit_pending's own
        # current_price/fill-price split in the sibling check_own_sell_fills.
        exit_signal_price = pos.get('broker_stop_price') or fill['price']
        # exit_bar_time passed directly, derived via compute.current_bar_time (found by
        # cold review 2026-08-14): this exit is detected by a broker-side fill poll, not
        # our own bar-close check_sell_condition call, so there's no exit_decision_bar
        # already stashed in trail_state for close_position() to fall back on -- without
        # this, the same-bar re-entry cooldown (_scan_buy_signals) silently has nothing
        # to compare against for exactly the exits it's most likely to matter for (a
        # resting SL/TRAIL order firing on its own, independent of our poll cadence).
        closed = db.close_position(pos['id'], exit_signal_price=exit_signal_price,
                                    exit_price=fill['price'], exit_time=datetime.now(),
                                    exit_reason=reason, exit_bar_time=compute.current_bar_time(ticker))
        if not closed:
            # Already closed this same cycle by another fill-detection path
            # reading the same stale open_positions snapshot.
            continue
        # Distinct result (was 'closed', identical to the other 2 call sites sharing this
        # scenario_key) -- coverage_registry.py's sl_order_fills_independent_detection row
        # exists specifically to prove THIS path (a resting SL/TRAIL order filling on its
        # own, independent of our bar-close check), and compute_status/compute_mode_statuses
        # aggregate by (mode, result) only, never by detail text -- 'via_sl_order_poll=1' in
        # detail was never actually checked anywhere, so any of the sibling paths' generic
        # 'closed' events were silently counting as proof of this one (found by Opus audit,
        # 2026-08-14).
        db.log_coverage_event("automated_exit_confirmed", _coverage_mode(account), ticker=ticker,
                              position_id=pos.get('id'), node_id=pos.get('wl_id'), result="closed_via_sl_order_poll",
                              detail=f"reason={reason} price={fill['price']:.4f} via_sl_order_poll=1")
        _post_message(f"🤖 {ticker} — auto-detected {reason} fill at ${fill['price']:.4f}  (P&L: {actual_pnl:+.2f}%)")
        try:
            close_addon_leg_real_if_open(pos, fill['price'], reason, datetime.now())
        except Exception as e:
            print(f"  [warn] {ticker} — unexpected error in close_addon_leg_real_if_open: {e}")


def check_exit_reminders(open_positions):
    """Nags every EXIT_REMINDER_MINUTES until a fired SELL signal is confirmed
    Exited or Skipped ('4r' in the buy/sell lifecycle numbering) -- mirrors
    check_trailing_reminders' supersede-not-edit-in-place pattern. Without this,
    a stalled SELL confirmation is invisible until the user happens to remember.

    Routine-wait suppression (2026-08-02): a TRAIL exit_pending with a
    confirmed-still-resting automated order is expected behavior -- the
    broker will fill it on its own and there's no action to take, so the
    first 2 reminder cycles are skipped (no Slack post) rather than repeating
    "still resting, no action needed" every 15 minutes. _exit_pending_blocks'
    own reminder_num>=3 escalation (routine stops being the likely
    explanation once a real order has rested 45+ minutes without filling)
    still fires normally -- this only silences the reminders before that
    threshold, it doesn't change the threshold itself. Only TRAIL is
    suppressed; TP/SL/TIME reminders (which genuinely may need a manual tap)
    are unaffected. Also gated by _trail_alert_should_post_now (this function
    only runs 9:00-16:00 to begin with, but too close to that 16:00 cutoff
    for reminder_num to reach 3 before reminders go quiet for the night still
    fails toward posting -- found by review, 2026-08-02)."""
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
        # dry_run gets zero Slack noise (2026-08-08 user call) -- mirrors
        # check_trailing_reminders' existing is_dry_run_sim skip above, which
        # this function never had (a real gap: dry_run exit reminders nagged
        # every EXIT_REMINDER_MINUTES with nothing to actually confirm).
        if pos.get('is_dry_run_sim'):
            continue
        # Sub-capital-at-stake nodes get zero real-time Slack, including this
        # reminder loop -- found missing by paired Opus review, 2026-08-08.
        # _node is None fails toward ALERTING, not muting (same rationale as
        # the other three sites this session).
        _node = db.get_watch_list_node_by_id(pos.get('wl_id'))
        if _node is not None and not has_capital_at_stake(_node):
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
        reminder_num = exit_pending.get('reminder_count', 0) + 1
        reason = exit_pending['reason']
        if reason == 'TRAIL' and reminder_num < 3 and not _trail_alert_should_post_now(now):
            # Fresh check every cycle, not a trust of a stale flag -- same
            # rationale as _exit_pending_blocks' own resting recheck.
            resting = _exit_order_resting(pos, reason, exit_pending.get('order_id'))
            if resting:
                new_state = dict(state)
                new_exit_pending = dict(exit_pending)
                new_exit_pending['reminder_count']   = reminder_num
                new_exit_pending['last_reminder_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
                new_state['exit_pending'] = new_exit_pending
                db.update_position_trail_state(pos['id'], new_state)
                continue
        _supersede_message(exit_pending.get('reminder_channel'), exit_pending.get('reminder_ts'), pos['ticker'])
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
        header = f"⚠️ *{ticker}* ({account} · {mode_tag(account, node)}) — FILL NOT CONFIRMED (reminder #{reminder_num})"
        trigger_str = f"  |  bounce trigger `${trigger:.2f}`" if trigger is not None else ""
        text = (
            f"{header}\n"
            f"Trailing buy order placed at the broker but not yet confirmed filled{trigger_str}.\n"
            f"Confirm Filled with the real fill price, Missed It if the bounce already passed before the "
            f"order was live, or Cancelled if the order didn't go through."
        )
    else:
        header = f"⚠️ *{ticker}* ({account} · {mode_tag(account, node)}) — ORDER NOT CONFIRMED PLACED (reminder #{reminder_num})"
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
        if _effectively_dry_run(account, pending['node']):
            continue
        last_at = datetime.strptime(pending['last_reminder_at'], '%Y-%m-%d %H:%M:%S')
        if (now - last_at).total_seconds() < BUY_REMINDER_MINUTES * 60:
            continue
        # Real-fill re-verify (2026-08-15, corrected Stage A): before assuming "still
        # pending" and nagging with stale/wrong state, actually check the broker.
        # Found live, SOXS 2026-08-14: a real fill sat unrecorded for hours while 15
        # reminders in a row all wrongly said "still pending" -- ticker/node wasn't
        # opted into auto_fill_detection, so NOTHING else was checking. Runs
        # regardless of has_capital_at_stake -- a confirmed-but-unreconciled real fill
        # is an infrastructure-precondition failure, not routine per-node noise (same
        # exemption already established for check_addon_buying_power_drift's
        # docstring: "the whole point of the capital threshold... doesn't apply to 'a
        # safety check's own precondition just broke'"). Only checks the broker once
        # per BUY_REMINDER_MINUTES per pending row (the cadence gate above already
        # throttles this), not every poll cycle.
        #
        # Deliberately requires a real order_id. The MANUAL placement flow
        # (signals_handlers.handle_trail_buy_order_placed ->
        # mark_pending_buy_placed_by_wl_id) sets order_placed=1 but never
        # captures a broker order id, so those rows are NOT re-verified here --
        # a known, accepted residual, confirmed by both reviewers 2026-08-15.
        # Relaxing the gate is the wrong fix: get_filled_order's order_id=None
        # mode is documented as a real hazard (it matched a days-old unrelated
        # fill and corrupted a real GDXU reconciliation, 2026-07-27), and
        # feeding that into a 🚨 CONFIRMED FILLED alert would be worse than the
        # stale reminder it replaces. Coverage for the manual case comes from
        # Stage D's intraday broker sweep (check_orphaned_broker_positions),
        # which is ground-truth broker-side and needs no order_id -- so the
        # residual window is ~30 min, not unbounded. A real fix would be
        # upstream: have the "Trailing Buy Order Placed" handler resolve and
        # store the order id at press time.
        if pending['order_placed'] and pending.get('order_id'):
            try:
                fill = schwab_client.get_filled_order(account, pending['ticker'], 'BUY',
                                                       order_id=pending['order_id'])
            except Exception as e:
                # This re-verification is an ENHANCEMENT on top of the reminder,
                # never a new precondition for it -- a transient broker/API
                # failure must not take the whole reminder loop down for every
                # other pending row (the _guarded() wrapper at this function's
                # call site catches at whole-function granularity, so an
                # unhandled raise here would silently skip every later pending
                # row this cycle). Falls through to the ordinary reminder below.
                print(f"  [buy_reminders] {pending['ticker']}: fill re-verification failed, "
                      f"falling back to the ordinary reminder: {e}")
                fill = None
            if fill is not None:
                db.log_coverage_event("confirmed_fill_dropped_at_gate", _coverage_mode(account),
                                       ticker=pending['ticker'], node_id=pending['node']['id'],
                                       result="alerted",
                                       detail=f"price={fill['price']:.4f} qty={fill['quantity']:g} order_id={pending['order_id']}")
                # Supersede the prior reminder first, same as the routine path
                # below -- without this the old "still pending" message keeps
                # live Filled/Missed It/Cancelled buttons sitting next to the
                # new CONFIRMED FILLED alert, and can never be superseded again
                # once update_pending_buy_reminder overwrites the tracked
                # channel/ts. Found by both reviewers, 2026-08-15.
                _supersede_message(pending['reminder_channel'], pending['reminder_ts'], pending['ticker'])
                reminder_num = pending['reminder_count'] + 1
                blocks = _pending_buy_blocks(pending, reminder_num)
                channel, ts = _post_message(
                    f"🚨 {pending['ticker']} ({account} · {mode_tag(account, pending['node'])}) — CONFIRMED FILLED "
                    f"at ${fill['price']:.4f} ({fill['quantity']:g} shares, order {pending['order_id']}) but NOT "
                    f"reconciled — tap Filled below now",
                    blocks=blocks)
                db.update_pending_buy_reminder(pending['id'], channel, ts, reminder_num)
                continue
        # Sub-capital-at-stake nodes get zero real-time Slack, including the
        # ROUTINE reminder loop below -- found missing by paired Opus review,
        # 2026-08-08: notify_buy_signal mutes the INITIAL alert but still creates
        # the pending_buys row (tracking is deliberately unconditional), so
        # without this the reminder loop nagged every BUY_REMINDER_MINUTES for a
        # signal the user was never shown in the first place. Deliberately does
        # NOT gate the real-fill check above -- see that block's own comment.
        if not has_capital_at_stake(pending['node']):
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


INTRADAY_RISK_REVIEW_WINDOW = ((9, 15), (16, 0))

# Conservative substring match against coverage_events.result -- deliberately
# NOT matching "blocked_*" (found by paired Opus review, 2026-08-08: many
# blocked_* results are a guard working correctly, e.g. blocked_same_ticker/
# blocked_insufficient, not a failure -- misclassifying those as "concerning"
# would recreate the exact noise problem this whole session was about
# fixing). Only unambiguous failure/anomaly language.
_CONCERNING_RESULT_SUBSTRINGS = (
    "fail", "reject", "mismatch", "tripped", "orphaned", "overspent", "unrecognized_account",
)


def _load_intraday_risk_review_state():
    try:
        state = json.loads(cfg.INTRADAY_RISK_REVIEW_STATE_PATH.read_text())
        return state if isinstance(state, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_intraday_risk_review_state(state):
    cfg.INTRADAY_RISK_REVIEW_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.INTRADAY_RISK_REVIEW_STATE_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(state))
    tmp.replace(cfg.INTRADAY_RISK_REVIEW_STATE_PATH)


def check_intraday_risk_review(now=None):
    """Reviews trading_incidents AND coverage_events for anything new since
    the last check and Slack-alerts only if something's there -- built
    2026-08-08 as the direct consequence of muting routine/anomaly Slack
    alerts for sub-capital-at-stake nodes (soxl_ira's small live positions,
    dry_run, canary) tonight: those events are still fully logged, just not
    paged in real time, so this is what actually reviews them.

    Originally scoped to trading_incidents only -- found by paired Opus
    review, 2026-08-08, to have no real trigger: no daemon code path calls
    log_incident, only manual scripts do, so the check ran every cycle and
    could never find anything real. Extended to also scan coverage_events,
    filtered to _CONCERNING_RESULT_SUBSTRINGS (deliberately narrow -- see
    that constant's own comment) so it can't recreate the noise problem this
    whole session was about fixing. Most real anomalies already have their
    own unconditional, size-independent Slack alert (the UNPROTECTED-style
    🚨 messages elsewhere in this file, never touched by tonight's muting) --
    this is a second-layer catch for anything logged to coverage_events
    WITHOUT a dedicated alert of its own.

    A judgment-based review layer (an LLM call, or a dedicated session,
    reading raw events rather than this fixed rule) was discussed and
    deliberately deferred -- this is the cheap, deterministic first version.
    No interval throttle deliberately -- this is a cheap DB query, not an
    LLM call, so it just runs every poll cycle (POLL_SECS, ~5min default)
    inside INTRADAY_RISK_REVIEW_WINDOW on a real trading day; caller (the
    main poll loop) calls this unconditionally every cycle, the
    window/trading-day gating happens in here.

    Both watermarks: on a missing/corrupt state file, seed to the CURRENT
    max id (not 0) so a fresh start doesn't dump every historical row
    (including already-resolved incidents) as "new" -- found by paired Opus
    review. Watermarks only advance on a CONFIRMED post (channel and ts both
    truthy) -- a failed/unconfirmed Slack post must not silently mark a real
    incident as reviewed, found by the same review round."""
    now = now or datetime.now()
    today = now.strftime('%Y-%m-%d')
    if not _coverage_is_trading_day(today):
        return
    (h0, m0), (h1, m1) = INTRADAY_RISK_REVIEW_WINDOW
    if not ((h0, m0) <= (now.hour, now.minute) <= (h1, m1)):
        return

    state = _load_intraday_risk_review_state()

    all_incidents = db.get_incidents(open_only=True, limit=200)
    if 'last_seen_incident_id' not in state:
        state['last_seen_incident_id'] = max((i['id'] for i in all_incidents), default=0)
    last_seen_incident_id = state['last_seen_incident_id']
    new_incidents = sorted(
        (i for i in all_incidents if i['id'] > last_seen_incident_id), key=lambda i: i['id'])

    all_events = db.get_coverage_events(limit=500)
    if 'last_seen_coverage_event_id' not in state:
        state['last_seen_coverage_event_id'] = max((e['id'] for e in all_events), default=0)
    last_seen_event_id = state['last_seen_coverage_event_id']
    new_events = sorted(
        (e for e in all_events
         if e['id'] > last_seen_event_id
         and any(s in (e.get('result') or '') for s in _CONCERNING_RESULT_SUBSTRINGS)),
        key=lambda e: e['id'])

    if new_incidents or new_events:
        lines = [f"🚨 Intraday risk review — {len(new_incidents)} new incident(s), "
                 f"{len(new_events)} concerning coverage event(s) since last check:"]
        for i in new_incidents:
            money = " [REAL MONEY]" if i.get('real_money_impact') else ""
            lines.append(f"  [incident #{i['id']}]{money} {i['ticker'] or ''} ({i.get('account') or 'n/a'}) — {i['title']}")
            lines.append(f"    {i['detail'][:300]}")
        for e in new_events:
            lines.append(f"  [coverage_event #{e['id']}] {e['scenario_key']} result={e['result']} "
                         f"{e.get('ticker') or ''} — {(e.get('detail') or '')[:200]}")
        channel, ts = _post_message("\n".join(lines))
        if channel and ts:
            if new_incidents:
                state['last_seen_incident_id'] = max(i['id'] for i in new_incidents)
            if new_events:
                state['last_seen_coverage_event_id'] = max(e['id'] for e in new_events)

    _save_intraday_risk_review_state(state)


# Tolerance for the drift check below -- not zero, since real balance values
# can carry sub-dollar float noise even when buying_power and cash are
# conceptually "the same field."
ADDON_BUYING_POWER_DRIFT_TOLERANCE = 1.0


def _load_addon_buying_power_drift_state():
    try:
        state = json.loads(cfg.ADDON_BUYING_POWER_DRIFT_STATE_PATH.read_text())
        return state if isinstance(state, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_addon_buying_power_drift_state(state):
    cfg.ADDON_BUYING_POWER_DRIFT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.ADDON_BUYING_POWER_DRIFT_STATE_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(state))
    tmp.replace(cfg.ADDON_BUYING_POWER_DRIFT_STATE_PATH)


# Stage D (2026-08-15). The broker-side orphan sweep itself is NOT new -- see
# scripts/check_untracked_positions.py, built 2026-08-07 after the GDXU
# incident, already wired into active_signals.py's 07:00 readiness block. What
# was missing is CADENCE: 07:00 is pre-market, so a fill that goes unreconciled
# at 09:30 (SOXS, 2026-08-14) isn't swept for until 07:00 the NEXT morning,
# ~22 hours later. That is exactly how long the real incident stayed invisible.
# This runs the SAME sweep intraday on a throttle -- deliberately reusing
# run_full_sweep rather than writing a parallel checker, for the same reason
# Stage B widened check_live_state_reconciliation instead of adding one: two
# answers to "what does the broker actually hold" will drift apart.
ORPHAN_SWEEP_WINDOW = ((9, 45), (16, 0))
# 30 min. Each sweep costs 2 real broker calls per account (long + short
# positions) across every linked account, so this is deliberately far coarser
# than POLL_SECS -- fast enough that a missed fill surfaces within half an hour
# instead of the next morning, cheap enough not to hammer the API all day.
# 9:45 start, not 9:30: a fill at the open needs time to reconcile normally
# before "no local row" means anything (the same reasoning as
# _RECONCILE_MISSING_SL_GRACE_SECS, at a coarser scale).
ORPHAN_SWEEP_INTERVAL_SECS = 1800


def _load_orphan_sweep_last_run():
    """Last sweep's epoch timestamp, persisted (2026-08-15 review finding).
    The first version kept this in a module-level list, which resets to 0 on
    every daemon restart -- and this project restarts the daemon deliberately
    and often (the morning restart is a documented manual step), so a restart
    inside the window would re-sweep immediately rather than honouring the
    30-minute interval. Impact is bounded (read-only sweep, ~2 broker calls per
    account) but both sibling checks this function explicitly mirrors --
    check_intraday_risk_review and check_addon_buying_power_drift -- already
    persist their own watermarks, so not doing so was an inconsistency, not a
    deliberate choice. A missing/corrupt file reads as 0.0, i.e. sweep now:
    erring toward an extra read-only sweep is the safe direction."""
    try:
        return float(_load_orphan_sweep_state().get('last_run', 0.0))
    except (ValueError, TypeError):
        return 0.0


def _load_orphan_sweep_state():
    """Whole persisted state dict for the intraday sweep: `last_run` (the
    throttle watermark) and `prior_findings` (the previous sweep's finding set,
    for the two-consecutive-sweep confirmation gate). A missing/corrupt file
    reads as {} -- sweep now, and treat the prior finding set as empty, which
    only ever DELAYS an alert by one interval rather than suppressing it."""
    try:
        state = json.loads(cfg.ORPHAN_SWEEP_STATE_PATH.read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        return {}


def _save_orphan_sweep_state(state):
    cfg.ORPHAN_SWEEP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.ORPHAN_SWEEP_STATE_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(state))
    tmp.replace(cfg.ORPHAN_SWEEP_STATE_PATH)


def _save_orphan_sweep_last_run(ts):
    state = _load_orphan_sweep_state()
    state['last_run'] = ts
    _save_orphan_sweep_state(state)


def _orphan_finding_keys(findings):
    """Flattens {account: [finding, ...]} into a stable, comparable key set for
    the two-sweep confirmation gate. The finding strings are deterministic for
    a given real state (they embed ticker + share counts), so an unchanged
    condition produces an identical key on the next sweep, while a transient
    one (a share count mid-reconciliation) produces a different key and
    correctly fails to confirm."""
    return {f"{account}||{f}" for account, fs in findings.items() for f in fs}


def _is_short_finding(key):
    """SHORT findings are exempt from the two-sweep gate -- see
    check_orphaned_broker_positions' docstring. Matches the marker
    scripts/check_untracked_positions.check_account emits for a real short."""
    return 'SHORT: broker holds a SHORT position' in key


def check_orphaned_broker_positions(now=None):
    """Intraday cadence for the ground-truth broker sweep (Stage D).

    Runs scripts/check_untracked_positions.run_full_sweep -- the existing,
    already-reviewed sweep that asks the broker "what do you actually hold"
    and flags anything with no local open_positions/addon_legs row (plus the
    mirror-image STALE/MISMATCH/SHORT/NULL-account cases). Detect-only, never
    auto-creates or corrects a row: automation_principles.md #5, and the
    explicit 2026-08-06 user scoping call recorded at the 07:00 call site --
    auto-correction would erase the exact signal the sweep exists to surface.

    Alerts unconditionally, not gated on has_capital_at_stake -- same
    exemption and same reasoning as check_addon_buying_power_drift below: a
    real broker position this system has no record of is an
    infrastructure-precondition failure, not routine per-node noise, and the
    capital threshold's job (mute routine sub-$10k chatter) simply doesn't
    apply to it. A fetch failure is reported as a finding by the sweep itself
    rather than swallowed as 'clean' -- "couldn't ask" is not "nothing wrong."

    TWO-CONSECUTIVE-SWEEP CONFIRMATION GATE (2026-08-15, from the cold
    reviewer's maintained MEDIUM in round 2). Moving this sweep from 07:00
    pre-market to an intraday cadence introduced a false-positive class the
    original run structurally could not have: two of check_account's finding
    kinds are TRANSIENTLY TRUE during normal market-hours operation --
      * STALE   (local row, broker holds 0) -- true between a real exit fill
                and close_position catching up
      * MISMATCH (share drift) -- true between a fill and its _reconcile_fill
                top-up, or an add-on leg fill mid-flight
    and this function alerts unconditionally (no has_capital_at_stake gate, by
    design), so first-sighting alerting would page the operator on ordinary
    reconciliation lag every 30 minutes. That is precisely the alert-fatigue
    failure the sweep's own hand-held-ticker filter was added to avoid.

    So: a finding must appear in TWO CONSECUTIVE sweeps before it pages
    anyone. The finding set is persisted in the same state file as the
    throttle, and only the intersection of the current and prior sets is
    alerted on. result='found' is still logged on FIRST sighting so the record
    is complete even when the alert is withheld.

    SHORT findings are EXEMPT from the gate and alert immediately on first
    sighting -- check_account already treats any real short as unconditionally
    finding-worthy regardless of the known-tickers filter (it's the naked/
    accidental-short case live_sanity_check.py exists to guard against), and a
    false positive there is worth tolerating.

    Cost of the gate: worst-case detection for a SOXS-shaped incident becomes
    two sweep intervals (~60 min) instead of one (~30 min) -- still down from
    the ~22 hours the 07:00-only cadence gave it, which was the entire point.

    now= is injectable (mirroring check_intraday_risk_review) so the window
    gate is testable without clock manipulation."""
    now = now or datetime.now()
    today = now.strftime('%Y-%m-%d')
    if not _coverage_is_trading_day(today):
        return
    (h0, m0), (h1, m1) = ORPHAN_SWEEP_WINDOW
    if not ((h0, m0) <= (now.hour, now.minute) <= (h1, m1)):
        return
    if time.time() - _load_orphan_sweep_last_run() < ORPHAN_SWEEP_INTERVAL_SECS:
        return
    # Stamped BEFORE the sweep runs, deliberately: an exception mid-sweep must
    # not leave the throttle unset and hammer the broker every poll cycle.
    _save_orphan_sweep_last_run(time.time())

    from scripts.check_untracked_positions import run_full_sweep
    try:
        findings = run_full_sweep()
    except Exception as e:
        # The sweep already converts a per-account fetch failure into a
        # finding; this only catches something structurally broken (an import
        # or DB error). Logged, not raised -- the poll loop's _guarded would
        # catch it anyway, but logging it as a coverage_event means the Grid
        # can tell "swept, clean" apart from "never actually swept."
        db.log_coverage_event("orphaned_broker_position", "live",
                               result="sweep_failed", detail=str(e))
        return

    # Two-consecutive-sweep confirmation gate (2026-08-15, maintained MEDIUM
    # from the cold reviewer's round-2 rebuttal). The finding set is recorded
    # every sweep regardless; only findings seen in BOTH this sweep and the
    # previous one actually page anyone.
    current_keys = _orphan_finding_keys(findings)
    _state = _load_orphan_sweep_state()
    prior_keys = set(_state.get('prior_findings') or [])
    _state['prior_findings'] = sorted(current_keys)
    _save_orphan_sweep_state(_state)

    if not findings:
        db.log_coverage_event("orphaned_broker_position", "live", result="clean",
                               detail="intraday sweep, no untracked/mismatched real positions")
        return

    total = sum(len(f) for f in findings.values())
    lines = []
    for acct, acct_findings in findings.items():
        lines.append(f"*{acct}*")
        lines.extend(acct_findings)
    detail = "\n".join(lines)
    # Logged on FIRST sighting, deliberately -- the record must show what the
    # broker actually looked like at this moment even when the alert is held
    # back for confirmation, or the gate would erase the evidence a later
    # investigation needs.
    db.log_coverage_event("orphaned_broker_position", "live", result="found",
                           detail=f"{total} finding(s): {detail}"[:2000])

    confirmed = (current_keys & prior_keys) | {k for k in current_keys if _is_short_finding(k)}
    if not confirmed:
        print(f"  [orphan_sweep] {total} finding(s) seen for the first time — holding the alert "
              f"until the next sweep confirms them (transient-state gate)")
        return

    confirmed_lines = []
    for account, acct_findings in findings.items():
        _kept = [f for f in acct_findings if f"{account}||{f}" in confirmed]
        if _kept:
            confirmed_lines.append(f"*{account}*")
            confirmed_lines.extend(_kept)
    confirmed_detail = "\n".join(confirmed_lines)
    _held = total - len(confirmed)
    _post_message(
        f"🚨 Untracked/mismatched real broker position(s) — {len(confirmed)} confirmed finding(s) "
        f"(intraday sweep {now.strftime('%H:%M')}, present in 2 consecutive sweeps)"
        f"{f'; {_held} more seen once, awaiting confirmation' if _held else ''}\n{confirmed_detail}"
    )


def check_addon_buying_power_drift(now=None):
    """Daily daemon check for follow-up #1 of the 2026-08-10 add-on
    buying-power reservation fix (docs/deep_backlog.md's 2026-08-09/10
    entry): schwab_safety.check_order's is_addon_leg buying-power block
    reserves OTHER tickers' resting-order notional at 1x, while the add-on's
    OWN notional gets ADDON_BUYING_POWER_HEADROOM_MULT (2x) -- an asymmetry
    that's currently harmless ONLY because buying_power == cash balance
    exactly on every real account with an addon_enabled live node today
    (verified directly via schwab_client at the time of that fix). The
    reservation math under-reserves by the leverage factor the moment that
    stops being true (a genuine Reg-T margin account, or if soxl_ira's
    IRA-limited-margin type ever grants real leverage) -- this check exists
    to catch THAT moment, not to fix the asymmetry itself (deliberately left
    as a real, undecided design question -- see that backlog entry).

    Tracked PER ACCOUNT, not one global watermark (2026-08-10, fixed by
    paired review -- all 3 reviewers independently caught the original
    single-watermark version stamping the whole day done even when every
    account's fetch failed, which could silently disable this check for the
    rest of the day with no retry and no alert -- the opposite of what a
    monitor for "the safety net's own assumption broke" should do). Each
    account's own successful check (diverged OR no_drift) is what marks it
    done for today; a fetch failure leaves it unmarked, so the next poll
    cycle (still same day, ~5min later) retries it -- no separate retry loop
    needed, the daemon's own cadence provides that. A diverged account is
    ALSO left unmarked if the alert doesn't confirm-post, mirroring
    check_intraday_risk_review's confirmed-post-before-advancing-watermark
    pattern a few dozen lines above -- a lost divergence alert must not be
    silently treated as "handled." Only checks accounts that currently host
    at least one state='live' addon_enabled node -- an account with no live
    add-on exposure has nothing this check protects.

    Alerts unconditionally (not gated on has_capital_at_stake) -- this is an
    infrastructure-assumption failure that silently under-reserves buying
    power for every add-on-eligible node on the account, not a per-node
    routine/anomaly event; the whole point of should_alert_live's capital
    threshold (mute routine noise below $10k) doesn't apply to "a safety
    check's own precondition just broke."
    """
    now = now or datetime.now()
    today = now.strftime('%Y-%m-%d')
    if not _coverage_is_trading_day(today):
        return

    accounts = sorted({
        node['account'] for node in db.get_watchlist()
        if node.get('state') == 'live' and node.get('addon_enabled') and node.get('account')
    })
    if not accounts:
        return

    state = _load_addon_buying_power_drift_state()
    checked = state.get('checked') or {}
    pending = [a for a in accounts if checked.get(a) != today]
    if not pending:
        return

    diverged = []
    for account in pending:
        # coverage_events has no `account` column -- ticker is the closest
        # dimension the table offers, and there's no real ticker for an
        # account-level check, so the account name goes there instead.
        mode = _coverage_mode(account)
        try:
            cash = schwab_client.get_account_balance(account)
            buying_power = schwab_client.get_account_buying_power(account)
        except Exception as e:
            db.log_coverage_event(
                "addon_buying_power_drift_check", mode, ticker=account,
                result="fetch_failed", detail=str(e))
            continue
        diff = buying_power - cash
        if abs(diff) > ADDON_BUYING_POWER_DRIFT_TOLERANCE:
            diverged.append((account, cash, buying_power, diff))
            db.log_coverage_event(
                "addon_buying_power_drift_check", mode, ticker=account,
                result="diverged", detail=f"account={account} cash=${cash:,.2f} "
                                           f"buying_power=${buying_power:,.2f} diff=${diff:,.2f}")
        else:
            db.log_coverage_event(
                "addon_buying_power_drift_check", mode, ticker=account,
                result="no_drift", detail=f"account={account} cash=${cash:,.2f} "
                                           f"buying_power=${buying_power:,.2f}")
            checked[account] = today

    if diverged:
        lines = ["\U0001F4B0 Add-on buying-power assumption broke — reservation math needs revisiting "
                 "(not blocking any order on its own):"]
        for account, cash, buying_power, diff in diverged:
            lines.append(f"  {account}: cash=${cash:,.2f} buying_power=${buying_power:,.2f} "
                         f"(diff ${diff:,.2f}) — addon_buying_power_check's other-ticker reservation "
                         f"is 1x, not scaled for this gap")
        channel, ts = _post_message("\n".join(lines))
        if channel and ts:
            for account, *_ in diverged:
                checked[account] = today
        # else: leave those accounts unmarked so the next poll retries the alert.

    state['checked'] = checked
    _save_addon_buying_power_drift_state(state)


def _reconcile_fill(node, fill_price, filled_shares, is_gap_correction=False, target_notional=None,
                     position_source='core'):
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
    so the top-up buy isn't wrongly blocked by the signal-window time gate.
    target_notional: explicit override -- a real drought-overlay fill's
    caller (_reconcile_buy_fill) passes node['starting_notional'] here
    instead of letting this default to _last_sale_recovery(node), since D4
    (docs/plans/real_order_execution_drought_addon.md) established drought
    sizing is flat starting_notional, not core's compounding recovery basis
    -- _last_sale_recovery(node, position_source='core') would otherwise
    target an unrelated (and possibly very different) core-compounded
    notional for a drought top-up.
    position_source: which of this node's legs the fill belongs to, passed
    straight through to db.top_up_position so the top-up names its target leg
    explicitly instead of relying on there only ever being one open row per
    wl_id. Corrected after review, 2026-08-15: that one-row invariant does
    currently hold (open_position dedups on wl_id alone, regardless of
    position_source), so this is defence in depth rather than a fix for
    observed corruption -- see db.top_up_position's own docstring."""
    ticker = node['ticker']
    account = node.get('account')
    if target_notional is None:
        target_notional = _last_sale_recovery(node)
    delta = target_notional - (fill_price * filled_shares)
    if delta > fill_price:
        top_up_shares = int(delta // fill_price)
        if top_up_shares > 0:
            try:
                schwab_client.place_equity_buy(account, ticker, top_up_shares, fill_price,
                                                is_gap_correction=is_gap_correction, is_protective=True,
                                                node_dry_run=(node.get('state') != 'live'), node_id=node.get('id'))
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
            if db.top_up_position(node['id'], top_up_shares, fill_price,
                                   position_source=position_source):
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


def _reconcile_buy_fill(ticker, fill_price, filled_shares, is_gap_correction=False, wl_id=None, account=None):
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
        # Two very different situations both land here and must not be
        # conflated: (1) this exact fill was already reconciled by an earlier
        # call (clear_pending_buy_by_wl_id already ran, an open_positions row
        # already exists) -- benign, a caller re-detecting the same fill,
        # silently return as before; (2) a real, confirmed-FILLED broker fill
        # with NO open position and NO pending_buys row -- genuinely never
        # tracked at all. Found live 2026-08-06: a bypass-staged real order
        # (stage_live_test_order.py) whose own node-lookup failed skipped
        # creating a pending_buys row, and this silent return meant the
        # resulting real fill sat completely unreconciled/unprotected for a
        # week with no alert anywhere -- nothing downstream
        # (open_position_from_pending, _reconcile_fill/post_fill_topup, the
        # real stop-loss placement below) ever ran, and nothing said so.
        # get_real_open_position (not bare get_open_position) -- ticker-only/
        # all-accounts/dry-run-sim-inclusive was itself a bug found by paired
        # Opus review: a same-ticker is_dry_run_sim=1 canary position in a
        # DIFFERENT account would be mistaken for "already reconciled" and
        # silently suppress the alert for a genuinely untracked real fill.
        if db.get_real_open_position(ticker, account=account) is not None:
            return
        _post_message(f"⚠️ {ticker} ({account or 'unmapped'} · {mode_tag(account)}) — real BUY fill "
                      f"detected (price=${fill_price:.4f} shares={filled_shares:g}) but NO pending_buys "
                      f"row and NO real open position exist for this ticker/account at all — not "
                      f"reconciled, no stop-loss placed. Verify and record manually.")
        # Distinct scenario_key from the success path (was "buy_fill_reconciled"
        # -- a genuine failure event under the SAME key as the success path
        # would render as verified-live proof of fill-reconciliation working,
        # in scripts/coverage_registry.py's Accountability Grid/EOD readiness
        # headline, since that row declares no bad_results. Found by paired
        # Opus review.
        db.log_coverage_event("orphaned_fill_detected", _coverage_mode(account), ticker=ticker,
                               result="no_pending_buys_row",
                               detail=f"price={fill_price:.4f} shares={filled_shares:g} wl_id_hint={wl_id}")
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
    opened = db.open_position_from_pending(pending, signal_price, fill_time, fill_price, fill_time,
                                            shares=filled_shares)
    if not opened:
        return
    drift_pct = (fill_price - signal_price) / signal_price * 100
    db.log_coverage_event("buy_fill_reconciled", _coverage_mode(node.get('account')), ticker=ticker,
                           node_id=node['id'], result="opened",
                           detail=f"shares={filled_shares:g} price={fill_price:.4f} drift={drift_pct:+.2f}%")
    _post_message(f"🤖 {ticker} — auto-detected fill at ${fill_price:.4f}  "
                  f"(drift: {drift_pct:+.2f}%)  {filled_shares:g} shares")
    _position_source = pending.get('position_source') or 'core'
    _drought_target_notional = node.get('starting_notional') if _position_source == 'drought_overlay' else None
    _reconcile_fill(node, fill_price, filled_shares, is_gap_correction=is_gap_correction,
                     target_notional=_drought_target_notional, position_source=_position_source)
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
                    account, ticker, order_id, "BUY", shares, current_price, is_gap_correction=True,
                    node_dry_run=(node.get('state') != 'live'), node_id=node.get('id'))
            else:
                _, new_order_id = schwab_client.place_equity_buy(
                    account, ticker, shares, current_price, is_gap_correction=True,
                    node_dry_run=(node.get('state') != 'live'), node_id=node.get('id'))
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

        _reconcile_buy_fill(ticker, fill['price'], fill['quantity'], is_gap_correction=True, wl_id=node['id'], account=account)


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
        # ORPHAN-FILL ALERT (added 2026-08-07, GDXU incident): a real BUY fill
        # with NO matching pending_buys row at all is a categorically
        # different situation from "opted out of auto-fill-detection" -- the
        # opt-in gate below exists to decide whether the daemon may
        # AUTO-RECONCILE a fill it already knows how to attribute; it says
        # nothing about whether a human should be told a fill happened with
        # NOTHING local to attribute it to. This is the one path that sees a
        # broker fill event independent of any pre-existing local row (every
        # other caller of _reconcile_buy_fill iterates pending_buys/
        # open_positions and so can never even observe a fully orphaned fill)
        # -- confirmed via a paired Opus review of the original silent-return
        # fix: check_auto_fills/check_gap_resize/_sync_confirm_and_protect all
        # start from a pending row and would never notice this case, and this
        # function's own opt-in gate (node_auto_fill_detection_enabled(None))
        # was silently swallowing it before this branch existed. Deliberately
        # does NOT attempt to reconcile/open a position here (no node to
        # attribute the fill to, no target notional, no strategy config) --
        # alert only, mirroring _reconcile_buy_fill's own case-(b) branch.
        if _matching_pending is None and ticker in schwab_safety.AUTOMATION_ENABLED_TICKERS:
            _confirmed_fill = schwab_client.get_filled_order(account, ticker, 'BUY', order_id=order_id)
            if _confirmed_fill is not None:
                _post_message(
                    f"🚨 {ticker} ({account} · {mode_tag(account)}) — real BUY fill confirmed "
                    f"(price=${_confirmed_fill['price']:.4f} shares={_confirmed_fill['quantity']:g}, "
                    f"order_id={order_id}) but NO pending_buys row matches this order at all — "
                    f"not reconciled, no position opened, no stop-loss placed. Verify and record manually."
                )
                db.log_coverage_event("orphaned_fill_detected", _coverage_mode(account), ticker=ticker,
                                       result="alerted", detail=f"order_id={order_id} "
                                       f"price={_confirmed_fill['price']:.4f} shares={_confirmed_fill['quantity']:g}")
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
        _reconcile_buy_fill(ticker, fill['price'], fill['quantity'], account=account)


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
        _reconcile_buy_fill(ticker, fill['price'], fill['quantity'], wl_id=node['id'], account=account)

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
        try:
            close_addon_leg_real_if_open(pos, fill['price'], exit_pending['reason'], datetime.now())
        except Exception as e:
            print(f"  [warn] {ticker} — unexpected error in close_addon_leg_real_if_open: {e}")


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
        # Bug #54 (found live: AGQ). This branch tagged is_dry_run_sim but had
        # no way at all to tell a PAPER position from a real one -- build_
        # reference_table merges paper_positions and open_positions into one
        # wl_id-keyed dict, which destroys the only signal of which table the
        # row came from, so a simulated position rendered byte-identically to
        # a real held one: same entry price, same share count, same actionable
        # framing, no marker anywhere. Now reads the origin column stamped on
        # the row itself (2026-08-15), the single source of truth both this and
        # build_reference_table share, rather than each re-deriving it.
        paper_tag = ' 📄PAPER' if pos and pos.get('origin') == 'paper' else ''
        text = (
            f"{phase_str}*{ticker}* `{version}`{sim_tag}{paper_tag} — {row['Hold']}{account_str}{entry_str}\n"
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
        # Substring, not exact-equality (2026-08-09 paired review, same gap
        # found in the new BUY/SELL alert tags) -- misses canary-family
        # variants like 'v5-canary-drought-addon' otherwise.
        mode_tag = ' 🧪CANARY' if 'canary' in ((row.get('_node') or {}).get('version') or '') \
            else (' (research)' if row.get('State') == 'paper' else '')
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
            # origin check added 2026-08-15 alongside bug #54's display tag,
            # and it is the more dangerous half of that bug. A paper row used
            # to render a real "Manually Close" button carrying
            # position_id=paper_positions.id, while the manual_close handler
            # resolves against open_positions -- two INDEPENDENT id sequences,
            # so the id would either miss entirely or, worse, match a
            # completely unrelated REAL position and close it. Suppressed for
            # the same reason is_dry_run_sim is: no real broker fill exists
            # behind this row, so there is nothing legitimate to close.
            if pos and not pos.get('is_dry_run_sim') and pos.get('origin') != 'paper':
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
                'TrailBuy%': node.get('trail_buy_pct'), 'Arm%': db._tp_or_arm_pct(node),
                'TrailSell%': node.get('trail_sell_pct'), 'Account': account, 'Last Sale $': last_sale,
                'Strategy': node['strategy'], 'Held': False, 'Phase': phase, 'State': node.get('state'),
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
                'TrailBuy%': trail_buy_pct, 'Arm%': db._tp_or_arm_pct(node),
                'TrailSell%': node.get('trail_sell_pct'), 'Account': account, 'Last Sale $': last_sale,
                'Strategy': node['strategy'], 'Held': False, 'Phase': phase, 'State': node.get('state'),
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
            # Column-overload: only TrailingBothZScoreBreakout stores its arm
            # threshold in arm_sell_pct -- every other strategy (incl.
            # TrailingExitZScoreBreakout) stores it in take_profit. Reading the
            # raw column rendered `arm ?` for a real UDOW row. db._tp_or_arm_pct
            # is the same resolver the 'Arm $' / arm-trigger math below uses.
            arm_pct = db._tp_or_arm_pct(pos)
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
                'State': node.get('state'),
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

    # Composition (and only composition) lives in scripts/coverage_report_summary.py
    # as of 2026-08-14 -- what counts as a deviation is still entirely
    # coverage_check.run_check's call, unchanged. The per-scenario block this
    # replaced rendered ~47 lines of canary/reconciliation_mismatch status every
    # night; it's one rollup line per group now, with the unexplained-deviation
    # alert kept intact (ticket_eligible defaults True -- an 'informational'
    # miss records no coverage_deviations row, so it must never render as
    # UNEXPLAINED, see coverage_check.run_check 2026-07-30) and the full
    # breakdown still one `scripts/coverage_check.py` run away.
    from scripts.coverage_report_summary import compose
    return _post_message("\n".join(compose(check_date, results, reasons)))


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
    elif db._tp_or_arm_pct(pos):
        # Same column overload as build_reference_table's 'Arm%' -- reading the
        # raw arm_sell_pct column dropped this whole line from the nightly plan
        # for every TrailingExitZScoreBreakout position (arm lives in take_profit).
        arm_sell_pct = db._tp_or_arm_pct(pos)
        arm_price = entry * (1 + arm_sell_pct / 100)
        parts.append(f"arms @ ${arm_price:.2f} ({arm_sell_pct}%) then trails {pos.get('trail_sell_pct') or '?'}%")
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
        # Grouped by scenario_key, not one line per raw result (2026-08-01,
        # after reconciliation_mismatch went from 1 global row to 20 per-node
        # rows -- see docs/backlog_cache.md's 2026-07-30 per-node breakout
        # entry): a single-row control scenario still reads identically to
        # before (one line), but a per-node one no longer floods this report
        # with 20 lines every EOD run for what's routine informational
        # status. Individual per-ticker lines only appear for a genuine
        # ticket-eligible deviation (a real problem) -- matches the canary
        # section's own met/deviation/informational/snoozed summary pattern
        # immediately above, instead of introducing a second, inconsistent
        # reporting shape for control scenarios specifically.
        other_by_key = {}
        for r in other_results:
            other_by_key.setdefault(r['scenario_key'], []).append(r)
        for key, group in sorted(other_by_key.items()):
            met_n = sum(1 for r in group if r['status'] == 'met')
            skipped_n = sum(1 for r in group if r['status'] == 'skipped')
            real_deviations = [r for r in group if r['status'] == 'deviated' and r.get('ticket_eligible', True)]
            info_misses_n = sum(1 for r in group
                                 if r['status'] == 'deviated' and not r.get('ticket_eligible', True))
            if len(group) == 1:
                r = group[0]
                status_glyph = "✓" if r['status'] == 'met' else ("~" if r['status'] == 'skipped' else "✗")
                lines.append(f"  {status_glyph} [control] {key}: {r.get('summary', r['status'])}")
            else:
                lines.append(f"  [control] {key}: {met_n} met / {len(real_deviations)} deviation(s) / "
                              f"{info_misses_n} informational / {skipped_n} snoozed of {len(group)}")
            for r in real_deviations:
                lines.append(f"    ✗ {key} ({r['ticker'] or 'n/a'}): {r['summary']}")

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
    grouped: held positions first, then buy candidates sorted by proximity.

    One uniform filter (2026-08-08 user call, final form): both Open
    Positions and Buy Candidates are filtered to has_capital_at_stake nodes
    only ($10k+ starting_notional, live). An earlier draft showed any real
    open position regardless of size -- explicitly rejected by the user
    ("i just said i don't want to see it") in favor of one consistent rule
    everywhere, no size carve-out. Sub-threshold real positions (e.g.
    soxl_ira's $500 nodes) are still fully tracked/protected by automation
    and logged to coverage_events -- just not shown in this report.
    When there's nothing in either tier, still posts the header/kill-switch-
    status/engine-buttons block (this is the ONLY place those buttons ever
    render -- an earlier version of this function short-circuited before
    building them at all, silently removing the emergency Stop Engine
    control from Slack every single day once zero nodes crossed the
    threshold, found and fixed by paired Opus review, 2026-08-08) and adds
    a short status line instead of the full held/candidate detail."""
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    watchlist = [n for n in watchlist if has_capital_at_stake(n)]
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
        category = 'RESEARCH' if node.get('state') == 'paper' else mode_tag(account, node)
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
        # Not "No open positions" -- that's a claim about ALL real positions,
        # which this report no longer shows (a sub-threshold live position,
        # e.g. soxl_ira's $500 nodes, may genuinely be open right now, just
        # not rendered here). Fixed by paired Opus review, 2026-08-08.
        units.append([{"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"No open positions above ${cfg.CAPITAL_AT_STAKE_THRESHOLD:,.0f} initial notional."}]}])

    units.append([{"type": "divider"}])
    units.append([{"type": "header", "text": {"type": "plain_text", "text": "Buy Candidates"}}])
    if not flat_rows:
        units.append([{"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"No nodes above ${cfg.CAPITAL_AT_STAKE_THRESHOLD:,.0f} initial notional to watch."}]}])
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
        extra = [x for x in (r.get('State'), r.get('Account')) if x]
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
