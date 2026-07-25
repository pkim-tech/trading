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
from signals_blocks import _post_message, _build_buy_blocks, _build_sell_blocks
from signals_helpers import (
    _proximity_emoji, _existing_position_note, _last_sale_recovery, _phase_emoji,
    buy_order_sizing,
)
# scripts/ has no __init__.py but is still importable as a Python 3 implicit
# namespace package as long as repo root is on sys.path (true whenever this
# module is reached via active_signals.py, run from repo root) -- same
# import tests/test_coverage_check.py already uses.
from scripts.coverage_check import run_check as _coverage_run_check


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
    'Order Placed' button. Returns False on any block/failure (falls back to
    the manual flow). If a STOP order is already resting from entry
    (pos['sl_order_id'], Part 4 Section 6), cancels it first -- otherwise both
    orders would be live simultaneously for the same shares (oversell attempt
    or rejected order).

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
        return False
    node = db.get_watch_list_node_by_id(pos.get('wl_id'))
    if node is None or node.get('mode') != 'live':
        return False
    if not schwab_safety.node_automation_enabled(pos.get('wl_id')):
        return False
    account = pos.get('account')
    shares = pos.get('shares')
    trail_sell_pct = pos.get('trail_sell_pct')
    if not shares or not trail_sell_pct:
        return False
    sl_order_id = pos.get('sl_order_id')
    if sl_order_id:
        try:
            _, cancel_status = schwab_client.cancel_order(account, ticker, sl_order_id)
        except Exception as e:
            _post_message(f"⚠️ {ticker} failed to cancel resting stop-loss {sl_order_id} before "
                          f"arming trailing-sell: {e} — falling back to manual")
            return False
        if cancel_status != "CANCELED":
            # Don't place a new trailing-sell without confirmed proof the old SL
            # is gone -- if it actually FILLED (not CANCELED), the shares are
            # already sold and a new sell here would be a real oversell attempt;
            # if unconfirmed, we can't tell either way. Fail safe: fall back to
            # manual instead of guessing (found via Opus review, 2026-07-24).
            _post_message(
                f"⚠️ {ticker} stop-loss {sl_order_id} cancel not confirmed CANCELED "
                f"(real status: {cancel_status!r}) — refusing to place a new trailing-sell "
                f"without proof the old order is gone; falling back to manual"
            )
            return False
    try:
        schwab_client.place_trailing_sell(account, ticker, shares, current_price, trail_sell_pct)
    except Exception as e:
        # If a resting SL was already cancelled above, the position is now
        # genuinely unprotected -- there's no safe automatic recovery here
        # (re-placing the same SL could itself hit the same block/failure),
        # so surface the SL price the user would need to manually re-enter at
        # the broker rather than leaving them to recompute it (found via
        # Opus review, 2026-07-22).
        if sl_order_id:
            sl_pct = pos.get('fixed_sl') if strategies.uses_fixed_sl(pos['strategy']) else pos.get('stop_loss')
            sl_price = pos['signal_price'] * (1 - sl_pct / 100) if sl_pct else None
            price_note = f"place stop-loss SELL {shares} @ ~${sl_price:.2f}" if sl_price else "place a stop-loss SELL manually"
            _post_message(
                f"🚨 *{ticker}* ({account}) UNPROTECTED — {price_note}\n"
                f"(auto trailing-sell failed after cancelling stop-loss {sl_order_id}: {e})"
            )
        elif not isinstance(e, schwab_safety.SafetyViolation):
            _post_message(f"⚠️ {ticker} automated trailing-sell placement failed unexpectedly: {e} — falling back to manual")
        return False
    return True


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
    per-section cooldown)."""
    db.log_coverage_event(
        "reconciliation_mismatch", _coverage_mode(pos.get('account')),
        ticker=pos.get('ticker'), position_id=pos.get('id'),
        node_id=pos.get('wl_id'), result=kind
    )
    key = f"{pos['id']}:{kind}"
    last = _RECONCILE_ALERTED.get(key, 0)
    if time.time() - last < _RECONCILE_COOLDOWN_SECS:
        return
    _RECONCILE_ALERTED[key] = time.time()
    _post_message(text)


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
        f"⚠️ *{ticker}* ({account}) exit check skipped this poll — no fresh price available\n"
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
        ticker = pos['ticker']
        if ticker not in schwab_safety.AUTOMATION_ENABLED_TICKERS:
            continue
        account = pos.get('account')
        if not account:
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

        expected_shares = pos.get('shares')
        if expected_shares is not None and real_shares != expected_shares:
            _alert_reconcile_mismatch(
                pos, "shares",
                f"⚠️ *{ticker}* live-state mismatch: `open_positions` tracks {expected_shares:g} "
                f"shares, broker shows {real_shares:g} — broker is ground truth; suggested fix: "
                f"verify no unexpected fill/manual trade explains the gap, then correct "
                f"`open_positions.shares` to {real_shares:g}"
            )

        state = pos.get('trail_state') or {}
        has_sell_order = any(
            leg.get('instruction') == 'SELL' and leg.get('instrument', {}).get('symbol') == ticker
            for o in orders for leg in o.get('orderLegCollection', [])
        )
        if expected_shares is None:
            continue
        if state.get('trailing') and state.get('order_placed') and not has_sell_order:
            _alert_reconcile_mismatch(
                pos, "missing_trailing_sell",
                f"⚠️ *{ticker}* live-state mismatch: trailing-sell marked placed but no resting "
                f"SELL order found at the broker — position may be unprotected; suggested fix: "
                f"place a trailing-sell order for {expected_shares:g} shares now"
            )
        elif not state.get('trailing') and pos.get('sl_order_id') and not has_sell_order:
            _alert_reconcile_mismatch(
                pos, "missing_sl",
                f"⚠️ *{ticker}* live-state mismatch: SL order id {pos['sl_order_id']} is recorded "
                f"but no resting SELL order found at the broker — position may be unprotected; "
                f"suggested fix: place a stop-loss order for {expected_shares:g} shares now"
            )


def _attempt_automated_market_buy(node, sizing):
    """Market-buy mirror of _attempt_automated_buy (Part 4, Section 4) --
    places a real (or dry_run) plain market order via schwab_client.place_equity_buy
    for a pilot-scope, non-trailing-buy node (e.g. TrailingExitZScoreBreakout)
    instead of waiting on the manual price-entry flow. Returns (False, None) if
    the ticker isn't in automation scope or schwab_safety blocks the order."""
    ticker = node['ticker']
    if ticker not in schwab_safety.AUTOMATION_ENABLED_TICKERS:
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


def _place_stop_loss_for_position(node, ticker, signal_price):
    """Places the real resting STOP order for a freshly-opened automated
    position -- market-buy (Part 4, Section 6) or trailing-buy (extended
    2026-07-24, called from both _reconcile_buy_fill's auto-fill path and
    signals_handlers.handle_trail_buy_fill_price's manual 'Filled' path).
    Reads the final share count back off open_positions (post any top-up
    _reconcile_fill already applied for market-buy fills) so the stop covers
    the whole position, not just a provisional quantity.
    Anchored to signal_price (the trigger price), not the real fill price --
    the backtest kernel computes stop_price = entry_price * (1 - sl%) where
    entry_price IS the trigger (op/cp), with zero fill slippage modeled.
    Anchoring to the real fill price instead would let market-order slippage
    silently loosen or tighten the stop relative to what the backtest assumed
    for that trade -- worst case, a real fill better than the trigger produces
    a looser live stop than the backtest's, so a gap that would have exited
    the backtest position could leave the live position open through a larger
    drawdown than modeled. Looks the position up by node['id'] (wl_id), not
    ticker -- ticker-only would size/anchor off a sibling node's position and
    stamp its broker order id onto the wrong row if 2+ nodes share this ticker
    (see docs/backlog_cache.md's wl_id refactor entry)."""
    pos = db.get_open_position_by_wl_id(node['id'])
    if not pos or not pos.get('shares'):
        return
    account = node.get('account')
    sl_pct = pos.get('fixed_sl') if strategies.uses_fixed_sl(pos['strategy']) else pos['stop_loss']
    if not sl_pct:
        return
    stop_price = signal_price * (1 - sl_pct / 100)
    try:
        _, sl_order_id = schwab_client.place_stop_loss(account, ticker, int(pos['shares']), stop_price)
    except schwab_safety.SafetyViolation as e:
        db.log_coverage_event("sl_placement", _coverage_mode(account), ticker=ticker, position_id=pos.get('id'),
                               node_id=node.get('id'), result="blocked", detail=str(e))
        _post_message(
            f"🚨 *{ticker}* ({account}) UNPROTECTED — place stop-loss SELL {int(pos['shares'])} @ ~${stop_price:.2f}\n"
            f"(stop-loss placement blocked: {e})"
        )
        return
    except Exception as e:
        db.log_coverage_event("sl_placement", _coverage_mode(account), ticker=ticker, position_id=pos.get('id'),
                               node_id=node.get('id'), result="failed_unexpectedly", detail=str(e))
        _post_message(
            f"🚨 *{ticker}* ({account}) UNPROTECTED — place stop-loss SELL {int(pos['shares'])} @ ~${stop_price:.2f}\n"
            f"(stop-loss placement failed unexpectedly: {e})"
        )
        return
    db.log_coverage_event("sl_placement", _coverage_mode(account), ticker=ticker, position_id=pos.get('id'),
                           node_id=node.get('id'), result="placed", detail=f"stop_price={stop_price:.4f}")
    if sl_order_id is not None:
        db.set_sl_order_id_by_position(pos['id'], sl_order_id)


def _sync_confirm_and_protect(ticker, node):
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
    but a human should know about the gap in the meantime."""
    account = node.get('account')
    ticker_label = ticker
    for _ in range(_SL_FAST_CONFIRM_ATTEMPTS):
        fill = schwab_client.get_filled_order(account, ticker, 'BUY')
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
                _sync_confirm_and_protect(ticker, node)

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

    reason_labels = {'TP': 'TAKE PROFIT', 'SL': 'STOP LOSS', 'TIME': 'TIME EXIT', 'TRAIL': 'TRAILING STOP'}

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
    # nag about than the buy side.
    state = dict(pos.get('trail_state') or {})
    state['exit_pending'] = {
        'reason': reason, 'current_price': current_price, 'target_price': target_price,
        'reminder_channel': channel, 'reminder_ts': ts, 'reminder_count': 0,
        'last_reminder_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
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
        state = dict(pos.get('trail_state') or {})
        state.pop('exit_pending', None)
        db.update_position_trail_state(pos['id'], state)
        print("  Skipped — position kept open.")


TRAIL_REMINDER_MINUTES = 15


def _trailing_order_blocks(pos, current_price, reminder_num=0):
    ticker    = pos['ticker']
    ep        = pos['entry_price']
    pct       = (current_price - ep) / ep * 100
    header    = f"⚠️ *{ticker}* — STILL PENDING (reminder #{reminder_num})" if reminder_num else f"🎯 *{ticker}* — TRAILING ACTIVATED — action needed"
    if reminder_num:
        text = (
            f"{header}\n"
            f"entry `${ep:.2f}`  |  current `${current_price:.2f}`  |  P&L `{pct:+.1f}%`\n"
            f"Trailing stop order not yet confirmed placed at the broker."
        )
    else:
        text = (
            f"{header}\n"
            f"entry `${ep:.2f}`  |  current `${current_price:.2f}`  |  P&L `{pct:+.1f}%`\n"
            f"Place the trailing stop order at the broker now."
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
    auto_placed = _attempt_automated_sell(pos, current_price)
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
    db.update_position_trail_state(pos['id'], state)


def check_trailing_reminders(open_positions):
    """Nags every TRAIL_REMINDER_MINUTES until the trailing-stop order is confirmed
    placed -- a single one-time alert is too easy to miss, and an unplaced trailing
    stop between polls is a real risk if price moves fast."""
    now = datetime.now()
    for pos in open_positions:
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
        f"⚠️ *{ticker}* — EXIT NOT CONFIRMED (reminder #{reminder_num})\n"
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


def check_exit_reminders(open_positions):
    """Nags every EXIT_REMINDER_MINUTES until a fired SELL signal is confirmed
    Exited or Skipped ('4r' in the buy/sell lifecycle numbering) -- mirrors
    check_trailing_reminders' supersede-not-edit-in-place pattern. Without this,
    a stalled SELL confirmation is invisible until the user happens to remember."""
    now = datetime.now()
    for pos in open_positions:
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
    placed = pending['order_placed']
    met, trigger = _trailing_buy_status(pending)

    if placed:
        header = f"⚠️ *{ticker}* — FILL NOT CONFIRMED (reminder #{reminder_num})"
        trigger_str = f"  |  bounce trigger `${trigger:.2f}`" if trigger is not None else ""
        text = (
            f"{header}\n"
            f"Trailing buy order placed at the broker but not yet confirmed filled{trigger_str}.\n"
            f"Confirm Filled with the real fill price, Missed It if the bounce already passed before the "
            f"order was live, or Cancelled if the order didn't go through."
        )
    else:
        header = f"⚠️ *{ticker}* — ORDER NOT CONFIRMED PLACED (reminder #{reminder_num})"
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
    signal_time = datetime.strptime(pending['signal_time'], '%Y-%m-%d %H:%M:%S')
    db.clear_pending_buy_by_wl_id(node['id'])
    opened = db.open_position(node, signal_price, signal_time, fill_price, datetime.now(),
                               shares=filled_shares)
    if not opened:
        return
    drift_pct = (fill_price - signal_price) / signal_price * 100
    _post_message(f"🤖 {ticker} — auto-detected fill at ${fill_price:.4f}  "
                  f"(drift: {drift_pct:+.2f}%)  {filled_shares:g} shares")
    _reconcile_fill(node, fill_price, filled_shares, is_gap_correction=is_gap_correction)
    if ticker in schwab_safety.AUTOMATION_ENABLED_TICKERS:
        _place_stop_loss_for_position(node, ticker, signal_price)


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
    cycle. Call once per day (active_signals._GAP_CHECK_WINDOW's once-daily-fire
    pattern) -- not idempotent against being called mid-flight twice for the same
    order (it would attempt to cancel an already-cancelled order)."""
    for pending in db.get_pending_buys():
        if not pending['order_placed']:
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
        running_low = pending['signal_price']
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

        order_id = pending.get('order_id')
        if order_id:
            try:
                _, cancel_status = schwab_client.cancel_order(account, ticker, order_id)
            except Exception as e:
                db.log_coverage_event("gap_resize", _coverage_mode(account), ticker=ticker,
                                       node_id=node.get('id'), result="cancel_failed", detail=str(e))
                _post_message(f"⚠️ {ticker} gap-correction cancel failed: {e} — leaving resting order in place")
                continue
            if cancel_status != "CANCELED":
                # Don't place a replacement MARKET order without confirmed proof
                # the original trailing-buy is gone -- proceeding here risks a
                # real double-order (both the original and the replacement fill)
                # if the cancel didn't actually take effect. Leave the pending_buys
                # row in place so a stray fill of the original order is still
                # reconciled normally (found via Opus review, 2026-07-24).
                db.log_coverage_event("gap_resize", _coverage_mode(account), ticker=ticker,
                                       node_id=node.get('id'), result="cancel_unconfirmed", detail=f"status={cancel_status!r}")
                _post_message(
                    f"⚠️ {ticker} gap-correction cancel not confirmed CANCELED "
                    f"(real status: {cancel_status!r}) — refusing to place a replacement order; "
                    f"leaving resting order/pending row as-is"
                )
                continue

        target_notional = _last_sale_recovery(node)
        padded_price = current_price * (1 + _GAP_RESIZE_PAD_PCT / 100)
        shares = int(target_notional // padded_price)
        _post_message(f"🌅 {ticker} — overnight gap cleared trigger (${current_price:.4f} vs "
                      f"${buy_trigger:.4f}); replacing with a MARKET order for {shares} shares")
        try:
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
        db.log_coverage_event("gap_resize", _coverage_mode(account), ticker=ticker,
                               node_id=node.get('id'), result="replaced", detail=f"shares={shares} price={current_price:.4f}")
        db.set_pending_buy_order_id_by_wl_id(node['id'], new_order_id)

        if new_order_id is None:
            # dry_run -- no real fill will ever appear on Schwab's order book
            continue

        for _ in range(_GAP_FILL_POLL_ATTEMPTS):
            fill = schwab_client.get_filled_order(account, ticker, 'BUY')
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
            account, ticker, side, _stream_price, _stream_shares = schwab_stream.FILL_QUEUE.get_nowait()
        except Exception:
            break
        if side != 'BUY':
            continue
        _node = db.get_watch_list_node(ticker=ticker, account=account)
        _node_id = _node['id'] if _node else None
        fill = None
        for _ in range(_GAP_FILL_POLL_ATTEMPTS):
            fill = schwab_client.get_filled_order(account, ticker, 'BUY')
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
        if not schwab_safety.auto_fill_detection_enabled(ticker):
            continue
        node = pending['node']
        account = node.get('account')
        if not account:
            continue
        fill = schwab_client.get_filled_order(account, ticker, 'BUY')
        if fill is None:
            continue
        _reconcile_buy_fill(ticker, fill['price'], fill['quantity'], wl_id=node['id'])

    for pos in open_positions:
        ticker = pos['ticker']
        if ticker not in schwab_safety.AUTOMATION_ENABLED_TICKERS:
            continue
        if not schwab_safety.auto_fill_detection_enabled(ticker):
            continue
        state = pos.get('trail_state') or {}
        exit_pending = state.get('exit_pending')
        if not (state.get('order_placed') and exit_pending):
            continue
        account = pos.get('account')
        if not account:
            continue
        fill = schwab_client.get_filled_order(account, ticker, 'SELL')
        if fill is None:
            continue
        actual_pnl = (fill['price'] - pos['entry_price']) / pos['entry_price'] * 100
        db.close_position(pos['id'], exit_signal_price=exit_pending['current_price'],
                           exit_price=fill['price'], exit_time=datetime.now(),
                           exit_reason=exit_pending['reason'])
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
        text = (
            f"{phase_str}*{ticker}* `{version}` — {row['Hold']}{account_str}{entry_str}\n"
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
            if pos:
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
            fill_detection_on = schwab_safety.auto_fill_detection_enabled(ticker)
            elements.append(
                {"type": "button", "text": {"type": "plain_text", "text": f"🤖 Disable {ticker} Auto-Fill Detection"},
                 "style": "danger", "action_id": "disable_auto_fill_detection", "value": ticker}
                if fill_detection_on else
                {"type": "button", "text": {"type": "plain_text", "text": f"🤖 Enable {ticker} Auto-Fill Detection"},
                 "action_id": "enable_auto_fill_detection", "value": ticker}
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
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}, {"type": "divider"}]
    for r in hot:
        blocks += _ticker_block(r)
    _post_message(header, blocks=blocks)


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
    positions = {p['wl_id']: p for p in db.get_open_positions()}
    pending_buys = {p['node']['id']: p for p in db.get_pending_buys()}
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
    if datetime.strptime(check_date, '%Y-%m-%d').weekday() >= 5:
        return _post_message(f"Coverage Report — {check_date} is a weekend, no trading day to check.")

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
    unexplained = [r for r in results if r['status'] == 'deviated' and not reasons.get(_key(r))]
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
        elif reasons.get(_key(r)):
            status = "✗ (explained)"
        else:
            status = "✗ UNEXPLAINED"
        lines.append(f"{status}  {r['scenario_key']}  ({r['ticker'] or ''})")

    return _post_message("\n".join(lines))


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

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Morning Report — {now_str}"}},
    ]
    stopped = schwab_safety.kill_switch_engaged()
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"{'🛑 Automated engine STOPPED' if stopped else '▶️ Automated engine running'}"}]})
    if cfg.INTERACTIVE:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "▶️ Start Engine"}, "style": "primary",
             "action_id": "start_engine"} if stopped else
            {"type": "button", "text": {"type": "plain_text", "text": "🛑 Stop Engine"}, "style": "danger",
             "action_id": "stop_engine"},
        ]})
    if cfg.INTERACTIVE:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "🔄 Resend Report"}, "action_id": "resend_ref_table"},
            {"type": "button", "text": {"type": "plain_text", "text": "🧭 Coverage Report"}, "action_id": "send_coverage_report"},
        ]})

    if held_rows:
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": "Open Positions"}})
        for r in held_rows:
            blocks += _ticker_block(r)
    else:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "No open positions."}]})

    blocks.append({"type": "divider"})
    blocks.append({"type": "header", "text": {"type": "plain_text", "text": "Buy Candidates"}})
    for r in flat_rows:
        blocks += _ticker_block(r)
        proximity = r.get('Proximity')
        if isinstance(proximity, (int, float)) and proximity < 5:
            chart = _chart_buy(r['_node'], r['_sig'])
            if chart:
                _upload_chart(chart, f"{r['Ticker']}_morning.png", f"{r['Ticker']} `{r['Version']}`  z={r['Z']:+.2f}")

    # Console output
    print(f"Morning Report — {now_str}")
    if held_rows:
        print("  Open positions:")
        for r in held_rows:
            print(f"    {r['Ticker']:<6} {r['Version']}  hold={r['Hold']}  now=${r['Now']:.2f}  {r['Next Action']}")
    for r in flat_rows:
        if r['Next Action'] == 'NO_DATA':
            print(f"  {r['Ticker']:<6} {r['Version']}  NO_DATA  [{r['Strategy']}]")
        else:
            emoji = _proximity_emoji(r['Proximity'])
            print(f"  {emoji} {r['Ticker']:<6} {r['Version']}  now=${r['Now']:>7.2f}  trigger=${r['Next Trigger $']:>7.2f}  ({r['Proximity']:+.1f}%)  z={r['Z']:>+5.2f}  [{r['Strategy']}]")

    return _post_message(f"Morning Report — {now_str}", blocks=blocks)
