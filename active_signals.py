#!/usr/bin/env python3
"""
Active signal monitor. Polls cached price data and fires BUY/SELL notifications.

Usage:
    python active_signals.py          # run signal loop
    python active_signals.py list     # show watch list
    python active_signals.py add      # add a node interactively
    python active_signals.py remove   # remove a node interactively
    python active_signals.py positions  # show open positions

Environment (Socket Mode — interactive buttons):
    SLACK_BOT_TOKEN     — bot OAuth token (xoxb-...)
    SLACK_APP_TOKEN     — app-level token (xapp-...) for Socket Mode
    SLACK_CHANNEL       — channel to post to (e.g. #trading)

Environment (Webhook fallback — fire-and-forget, no buttons):
    SLACK_WEBHOOK_URL   — incoming webhook URL

    SIGNAL_POLL_SECS    — poll interval in seconds (default 300)

Module layout: DB layer is signals_db.py, signal computation (SMA/Std
indicator cache, buy/sell evaluation) is signals_compute.py, chart PNG
generation is signals_charts.py, Slack message posting/block builders is
signals_blocks.py, small shared helpers (used by both blocks and notify) is
signals_helpers.py, Bolt interactive button/modal handlers is
signals_handlers.py, notify_*/reminder loops/reference-table/report is
signals_notify.py, and shared config/tokens/the Bolt app singleton is
signals_config.py. This file re-exports their public names for backward
compatibility with existing `from active_signals import X` / `import
active_signals as a; a.X` callers (scripts/, pages/, tests/) and keeps only
the daemon main loop and CLI dispatch.
"""

import sys
import time
import threading
import contextlib
import functools
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta

import pandas_market_calendars as mcal

from data_manager import fetch_live_data_smart
import strategies

import signals_config as cfg
import signals_db as db
import signals_compute as compute
import schwab_safety
import schwab_client
import schwab_stream
import paper_trading
import signals_invariants

# --- Backward-compatible re-exports -----------------------------------------

from signals_config import (
    DB_PATH, RESEARCH_DB_PATH, CACHE_DIR, CONFIG_PATH, POLL_SECS, SLACK_HOOK,
    LOG_DIR, HUMAN_LOG_PATH, VERBOSE_LOG_PATH, HEARTBEAT_PATH,
    SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_CHANNEL, SOCKET_MODE,
    SIM_MODE, SIM_SCENARIO, INTERACTIVE, bolt_app,
    _Tee, _resolve_channel_id,
)
from signals_db import (
    ensure_tables, get_watchlists, get_active_watchlist_id, create_watchlist,
    delete_watchlist, set_active_watchlist, get_watchlist, _config_fixed_stop_loss,
    _tp_or_arm_pct, _is_trailing_buy, add_node, remove_node, set_node_mode, label_node,
    get_open_positions, get_held_tickers, add_pending_buy, get_pending_buys,
    clear_pending_buy, mark_pending_buy_placed, update_pending_buy_reminder,
    update_position_trail_state, closed_today, open_position, close_position,
    log_trade_entry, log_trade_exit, _conn,
)
from signals_compute import (
    _load_cache, _current_price, _hurst_adf, compute_buy_signal, _bars_held,
    check_sell_condition, _indicator_cache,
)
from signals_charts import _upload_chart, _chart_buy, _chart_sell
from signals_blocks import (
    _post_message, _fields_block, _price_input_block, _shares_input_block,
    _build_buy_blocks, _build_sell_blocks,
)
from signals_helpers import (
    _add_trading_hours, _proximity_emoji, _last_sale_recovery, _phase_emoji, log_poll, _pos_key,
    mode_tag as _account_mode_tag,
)
from signals_notify import (
    notify_buy_signal, notify_limit_fill, notify_sell_signal,
    TRAIL_REMINDER_MINUTES, _trailing_order_blocks, _supersede_message,
    notify_trailing_activated, check_trailing_reminders,
    EXIT_REMINDER_MINUTES, _exit_pending_blocks, check_exit_reminders,
    BUY_REMINDER_MINUTES, _trailing_buy_status, _pending_buy_blocks, check_buy_reminders,
    check_auto_fills, check_gap_resize, drain_fill_queue,
    check_live_state_reconciliation, alert_stale_price_exit_suppressed,
    _ticker_block, _send_window_alert, _coverage_mode,
    _REF_TABLE_COLS, build_reference_table, format_reference_table, _STRATEGY_LABELS,
    send_reference_report, send_coverage_report,
    update_dry_run_buys, check_dry_run_sim_sells,
)
import signals_handlers  # noqa: F401 -- import registers Bolt handlers as a side effect


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

# Signal windows in ET: 10:25-10:40 (9:30 bar close) and 15:25-15:40 (14:30 bar close)
_SIGNAL_WINDOWS = [(10, 25, 10, 40), (15, 25, 15, 40)]

# entry_timing='open_check' nodes also get an earlier poll right after each relevant
# bar opens (9:30/14:30), mirroring the backtest kernel's Open-then-fall-through-to-Close
# order: sma/std come from df_daily_prior (strictly prior days), so they're already valid
# this early. Nodes not marked open_check are skipped here and only checked at the normal
# close-window time. If an open-check poll fires a BUY, the shared buy_alerted dedup (keyed
# without a window/time component) naturally suppresses the same node re-firing ~55 minutes
# later at the regular close-window check.
_OPEN_CHECK_WINDOWS = [(9, 31, 9, 40), (14, 31, 14, 40)]

# Pre-open overnight-gap check (Part 3, branch B) -- must run before Session.NORMAL
# orders start executing at 9:30, and after most real pre-market price discovery
# has happened. Fires once daily, same pattern as _REFERENCE_TIMES/reference_alerted.
_GAP_CHECK_WINDOW = (9, 15, 9, 29)

# Pinned single-shot checks (Part 4) -- one per hourly bar boundary during market
# hours (+2s buffer for the print to have landed), instead of relying on ambient
# POLL_SECS-cadence polling to notice a bar closed/opened. Two purposes, one
# scheduling mechanism: entry-signal detection at the 4 real signal-reaction
# moments (_PINNED_ENTRY_TIMES, Section 1a) and exit-arm-latency reduction at all
# 7 (Section 1b, open positions only).
_PINNED_BAR_TIMES = [(9, 30, 2), (10, 30, 2), (11, 30, 2), (12, 30, 2), (13, 30, 2), (14, 30, 2), (15, 30, 2)]
_PINNED_ENTRY_TIMES = {(9, 30), (10, 30), (14, 30), (15, 30)}
# The two moments where the backtest's literal bar Open is what's being matched
# (vs. 10:30/15:30, which approximate the just-closed bar's Close).
_PINNED_OPEN_TIMES = {(9, 30), (14, 30)}

# Reference report fires once at each of these times daily -- early (7am) so
# there's a report before the day even starts, before the open, and before the
# afternoon signal window, so a fresh full-watchlist view lands ahead of the
# moments an action is most likely to be required. Also fires unconditionally
# on daemon startup/restart, independent of this schedule.
_REFERENCE_TIMES = [(7, 0), (9, 20), (15, 20)]

# Coverage report only fires at the 7am slot -- it's the "ready to run" gate
# checking the previous trading day's results before today's signal windows
# open, not a same-day check (which would be checking a day that hasn't
# traded yet). _previous_trading_day walks back over a weekend (no holiday
# calendar, same simplicity level as scripts/live_sim.py's identical pattern)
# so a Monday morning run checks Friday, not a trivially-weekend-skipped
# Sunday.
def _previous_trading_day(today):
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime('%Y-%m-%d')


def _reminders_active(now):
    """Reminders only nag during market hours (9:00-16:00) -- outside that
    window they'd just pile up overnight/pre-market with nothing anyone can
    act on, so this pauses them and they pick back up fresh at 9:00."""
    return (9, 0) <= (now.hour, now.minute) <= (16, 0)


_NYSE_CAL = mcal.get_calendar('NYSE')


@functools.lru_cache(maxsize=8)
def _is_trading_day(date_str):
    """NYSE trading-day gate (weekends + market holidays). Root-caused the
    2026-07-26 ERY phantom-fill incident: _in_window previously checked only
    (hour, minute), so a signal window fired normally on a Sunday against
    stale cached data and placed a real order."""
    return not _NYSE_CAL.schedule(start_date=date_str, end_date=date_str).empty


def _in_window(now, windows):
    if not _is_trading_day(now.strftime('%Y-%m-%d')):
        return False
    t = (now.hour, now.minute)
    for h0, m0, h1, m1 in windows:
        if (h0, m0) <= t <= (h1, m1):
            return True
    return False


def _in_buy_window(now):
    return _in_window(now, _SIGNAL_WINDOWS)


def _seconds_until_next_pinned_target(now):
    """Seconds until the next _PINNED_BAR_TIMES moment (today or, if today's are
    all past, tomorrow's first) -- lets the main loop wake early right before a
    pinned target instead of free-running past it on POLL_SECS cadence."""
    todays = [now.replace(hour=h, minute=m, second=s, microsecond=0) for h, m, s in _PINNED_BAR_TIMES]
    upcoming = [t for t in todays if t > now]
    if upcoming:
        target = min(upcoming)
    else:
        h, m, s = _PINNED_BAR_TIMES[0]
        target = (now + timedelta(days=1)).replace(hour=h, minute=m, second=s, microsecond=0)
    return (target - now).total_seconds()


def _sleep_until_next_cycle(now):
    """Wakes early right before the next pinned bar-time target instead of
    free-running the full POLL_SECS past it, while otherwise behaving exactly
    like the old flat time.sleep(POLL_SECS)."""
    time.sleep(min(POLL_SECS, max(1, _seconds_until_next_pinned_target(now))))


def _real_order_or_position_exists(node, ticker):
    """True if a real broker order or held position already exists for this
    ticker/account -- checked right before firing a fresh BUY signal alert so
    a node with an already-unresolved pending buy the DB lost track of (or an
    already-held position the DB doesn't know about) doesn't get a duplicate
    alert. Broker-truth, not local state -- same rationale as schwab_safety.
    _all_orders ("local tracking could drift or miss an order placed outside
    our own code"). Only meaningful for a genuinely real (dry_run=False)
    account -- a dry_run/paper/canary node never places a real order or holds
    a real position, so there's nothing to check at the broker; those rely on
    the pending_wl_ids DB check alone (see _scan_buy_signals)."""
    account = node.get('account')
    if not account or ticker not in schwab_safety.AUTOMATION_ENABLED_TICKERS:
        return False
    limits = schwab_safety.ACCOUNTS.get(account)
    if limits is None or limits.dry_run:
        return False
    try:
        orders = schwab_safety._open_orders(account)
        if schwab_safety._has_open_order(orders, ticker):
            return True
        return schwab_client.get_real_position(account, ticker) > 0
    except Exception as e:
        print(f"  [warn] {ticker} broker dedupe check failed: {e} — falling back to DB-only check")
        return False


def _scan_buy_signals(nodes, buy_alerted, open_position_keys, price_overrides=None):
    """Runs compute_buy_signal over `nodes` and fires notify_buy_signal on new BUYs.
    Shared by the open-check/close-window ambient polls and the pinned single-shot
    checks (_scan_pinned_entry) so a node checked from any of them gets identical
    handling -- price_overrides (ticker -> price) lets the pinned path substitute a
    precise fetched price for the default ambient yfinance lookup inside
    compute_buy_signal."""
    price_overrides = price_overrides or {}
    # Pending (order placed but not yet confirmed filled) tickers, real and
    # paper -- the same-day unlock below must not re-fire while one of these
    # is still resting, or it would re-notify (and, if wired to automated
    # placement, re-place a real order) on every single poll until the human
    # clicks Filled / the paper bounce-fill lands. Found by Opus review
    # 2026-07-24: closed_today + no open position is also true for the whole
    # window between "order placed" and "position opens on fill confirmation,"
    # not just after a genuine close. Paper trailing-buy nodes have the exact
    # same pending-window shape (paper_trading.start_paper_buy/
    # paper_pending_buys), so both tables are checked.
    pending_wl_ids = {p['node']['id'] for p in get_pending_buys()} | {p['node']['id'] for p in db.get_paper_pending_buys()}
    summaries = []
    for node in nodes:
        sig = compute_buy_signal(node, price_override=price_overrides.get(node['ticker']))
        if sig is None:
            summaries.append(f"{node['ticker']} w={node['window']} NO_DATA")
            continue

        # Keyed on the watch_list row's own PK (wl_id), not (ticker, strategy, window) --
        # two concurrent nodes differing only in account/take_profit/label could otherwise
        # share one alert_key and dedupe against each other (see docs/backlog_cache.md's
        # wl_id refactor entry).
        alert_key = node['id']
        # open_position_keys entries are wl_id (int) when available, else a
        # (ticker, window) fallback for legacy/unbackfillable positions -- a
        # live node's own id never collides with that fallback shape, but the
        # fallback must still be checked or a NULL-wl_id legacy position on
        # this exact ticker+window silently stops suppressing a duplicate BUY
        # (found by a second Opus review round, 2026-07-26).
        already_held = node['id'] in open_position_keys or (sig['ticker'], node['window']) in open_position_keys

        # buy_alerted's once-per-day lockout structurally blocked a real,
        # quantified slice (~8% for SOXL) of a winning node's backtested
        # trades: buy -> sell -> buy all in one day, where the ticker already
        # got its one alert before the position even closed. Once the
        # position has genuinely closed (not just still open/pending) and a
        # real exit was recorded today, clear the lock so a later same-day
        # signal can alert again -- same-day re-buy risk itself is still
        # covered separately by check_order's same_day_block guard.
        # closed_today's paper flag matters here (Opus review 2026-07-24): a
        # research-mode/paper node's real exit only ever lands in
        # paper_trade_log, never trade_log -- without it this unlock could
        # never fire for a paper node at all.
        is_paper_node = node.get('mode', 'live') != 'live'
        if (alert_key in buy_alerted and not already_held
                and node['id'] not in pending_wl_ids and closed_today(sig['ticker'], paper=is_paper_node, node=node)):
            buy_alerted.discard(alert_key)

        if sig['signal'] == 'BUY' and alert_key not in buy_alerted:
            buy_alerted.add(alert_key)
            if already_held:
                print(f"  [skip] BUY {sig['ticker']} z={sig['z_score']:+.2f} — position already open, no alert")
            elif node.get('mode', 'live') == 'live':
                # pending_wl_ids alone would have prevented the 2026-07-26 DIA/IWM/
                # QQQ/LABU duplicate-pending-buy incident (a restarted daemon forgot
                # buy_alerted and re-fired a fresh alert/pending_buys row on top of an
                # already-unresolved one) -- it was computed above but never actually
                # checked here. _real_order_or_position_exists adds a broker-truth
                # check on top for real accounts, catching drift pending_wl_ids can't
                # (e.g. a resting order or held position the DB lost track of).
                already_pending = node['id'] in pending_wl_ids or _real_order_or_position_exists(node, sig['ticker'])
                if already_pending:
                    # Don't burn today's alert slot on a suppressed signal (Opus review,
                    # 2026-07-26) -- if the resting order/position clears later today,
                    # the node must still be able to alert same-day, not wait for
                    # tomorrow's buy_alerted reset.
                    buy_alerted.discard(alert_key)
                    db.log_coverage_event("dup_buy_alert_suppressed", _coverage_mode(node.get('account')),
                                           ticker=sig['ticker'], node_id=node['id'], result="suppressed")
                    _post_message(f"🔇 {sig['ticker']} ({node.get('account')} · {_account_mode_tag(node.get('account'))}) "
                                  f"BUY signal suppressed — already pending/resting at broker or in pending_buys")
                else:
                    notify_buy_signal(node, sig)
            elif sig['ticker'] in schwab_safety.AUTOMATION_ENABLED_TICKERS:
                paper_trading.start_paper_buy(node, sig)
                print(f"  [paper] BUY: {node['ticker']} z={sig['z_score']:+.2f} (paper-trading)")
            else:
                print(f"  [research] BUY: {node['ticker']} z={sig['z_score']:+.2f} (no alert)")
        else:
            mode_tag = ' [R]' if node.get('mode') == 'research' else ''
            summaries.append(
                f"{sig['ticker']}{mode_tag} z={sig['z_score']:+.2f} {sig['signal']}"
            )
    return summaries


def _scan_pinned_entry(target_h, target_m, watchlist, buy_alerted, open_position_keys):
    """Pinned single-shot entry check (Part 4, Section 1a) -- fetches a precise
    price (Schwab's true session Open at 9:30/14:30, matching the backtest
    kernel's literal bar Open exactly; a live quote at 10:30/15:30) instead of
    relying on ambient POLL_SECS-cadence polling, for automation-enabled
    open_check nodes only. Delegates to _scan_buy_signals via price_overrides so
    there's one alert code path for both ambient and pinned checks.

    Returns (summaries, failed_tickers) -- a ticker whose price fetch raised is
    excluded from this call's _scan_buy_signals entirely (not passed through to
    fall back on compute_buy_signal's ambient yfinance price), and reported back
    in failed_tickers so the caller can retry just that ticker at the real
    Schwab price instead of silently committing at a degraded one (Opus review,
    2026-07-26 -- the retry loop below only helps if a fetch failure doesn't
    fire an order on attempt 1 in the first place)."""
    nodes = [n for n in watchlist
             if n.get('entry_timing') == 'open_check' and n['ticker'] in schwab_safety.AUTOMATION_ENABLED_TICKERS]
    if not nodes:
        return [], set()
    is_open_check = (target_h, target_m) in _PINNED_OPEN_TIMES
    price_overrides = {}
    failed_tickers = set()
    for node in nodes:
        ticker = node['ticker']
        if ticker in price_overrides or ticker in failed_tickers:
            continue
        try:
            if is_open_check:
                price, is_true_open = schwab_client.get_session_open_price(ticker)
            else:
                price, is_true_open = schwab_client.get_current_price(ticker), False
        except Exception as e:
            print(f"  [pinned] {ticker} price fetch failed at {target_h:02d}:{target_m:02d}: {e}")
            log_poll(f"{ticker} pinned_entry target={target_h:02d}:{target_m:02d} FETCH FAILED: {e}")
            failed_tickers.add(ticker)
            continue
        price_overrides[ticker] = price
        log_poll(f"{ticker} pinned_entry target={target_h:02d}:{target_m:02d} price={price:.4f} is_true_open={is_true_open}")
        db.log_open_price_quality(ticker, target_h, target_m, price, is_true_open)
    ready_nodes = [n for n in nodes if n['ticker'] not in failed_tickers]
    summaries = _scan_buy_signals(ready_nodes, buy_alerted, open_position_keys, price_overrides=price_overrides)
    return summaries, failed_tickers


def _scan_pinned_exit_arm(open_positions, sell_alerted, last_seen_bar):
    """Pinned bar-boundary exit-arm check (Part 4, Section 1b) -- collapses the
    up-to-5-minute ambient-poll detection gap on a newly-closed bar to ~2s for
    open positions on automation-enabled tickers, since place_trailing_sell's
    real starting reference is live price at order-submission time, not
    anything computed here -- a late detection means the real trailing order
    can start from a materially drifted (lower) peak than the backtest
    assumed. Decision logic is unchanged (check_sell_condition); this only
    changes *when* it runs. Shares sell_alerted/last_seen_bar with the ambient
    exit-check loop, so whichever notices a bar-close first suppresses the
    other -- call this before that loop each iteration."""
    for pos in open_positions:
        if pos.get('is_dry_run_sim'):
            # No real order/broker linkage exists for this position -- routed
            # through check_dry_run_sim_sells instead (Opus review 2026-07-26:
            # this loop was missed by the original is_dry_run_sim skip, letting
            # it consume last_seen_bar's bar-close marker out from under
            # check_dry_run_sim_sells AND fire real notify_trailing_activated/
            # notify_sell_signal Slack flows for a synthetic position).
            continue
        if pos['ticker'] not in schwab_safety.AUTOMATION_ENABLED_TICKERS:
            continue
        df_hourly, _ = _load_cache(pos['ticker'])
        if df_hourly is None or df_hourly.empty:
            continue
        last_bar_ts = df_hourly.index[-1]
        if (pos['id'], last_bar_ts) in sell_alerted:
            continue
        if last_seen_bar.get(_pos_key(pos)) == last_bar_ts:
            log_poll(f"{pos['ticker']} pinned_exit_arm bar={last_bar_ts} matches last_seen -- SKIPPED (no new bar)")
            continue  # no new bar since the last check -- nothing to react to early
        last_seen_bar[_pos_key(pos)] = last_bar_ts
        bar = df_hourly.iloc[-1]
        cp, low, high, op = float(bar['Close']), float(bar['Low']), float(bar['High']), float(bar['Open'])
        log_poll(f"{pos['ticker']} pinned_exit_arm bar={last_bar_ts} cp={cp:.4f} low={low:.4f} high={high:.4f} op={op:.4f}")
        db.log_coverage_event("exit_arm_latency", _coverage_mode(pos.get('account')), ticker=pos['ticker'],
                               position_id=pos.get('id'), node_id=pos.get('wl_id'), result="evaluated",
                               detail=f"bar={last_bar_ts}")
        reason, target, just_activated_trailing = check_sell_condition(
            pos, cp, datetime.now(), at_bar_close=True, low=low, high=high, open_price=op, df_hourly=df_hourly)
        if just_activated_trailing:
            notify_trailing_activated(pos, cp)
        if reason:
            notify_sell_signal(pos, reason, cp, target)
            sell_alerted.add((pos['id'], last_bar_ts))


def _seed_last_seen_bar(open_positions):
    """Startup seed for last_seen_bar (see run_loop) -- wl_id -> real current
    hourly bar timestamp for every currently-open position. See run_loop's
    last_seen_bar comment for why an empty dict causes a spurious off-schedule
    bar-close evaluation on every restart."""
    seeded = {}
    for pos in open_positions:
        df_hourly, _ = _load_cache(pos['ticker'])
        if df_hourly is not None and not df_hourly.empty:
            seeded[_pos_key(pos)] = df_hourly.index[-1]
    return seeded


_LAST_SECTION_ALERT: dict[str, float] = {}
_SECTION_ALERT_COOLDOWN_SECS = 900  # 15 min -- matches the reminder-nag cadence elsewhere


def _guarded(section: str, fn, *args, **kwargs):
    """Runs fn(*args, **kwargs), catching and logging any exception so one
    failing run_loop section can't crash the whole daemon (automation_principles.md
    #3 -- per-unit failure isolation). Posts a Slack alert on failure so it
    doesn't fail silently (#4), rate-limited per section (a persistent failure
    would otherwise repost every poll cycle). Returns fn's result, or None on
    failure -- callers that expect a list (e.g. summaries += _guarded(...))
    must handle None."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"  [loop] section '{section}' failed: {e}")
        db.log_coverage_event("daemon_section_exception", _coverage_mode(None),
                               result=section, detail=str(e))
        last = _LAST_SECTION_ALERT.get(section, 0)
        if time.time() - last > _SECTION_ALERT_COOLDOWN_SECS:
            _LAST_SECTION_ALERT[section] = time.time()
            try:
                _post_message(
                    f"⚠️ daemon loop section '{section}' failed: {e} "
                    f"(will keep retrying every poll; repeat alerts suppressed for "
                    f"{_SECTION_ALERT_COOLDOWN_SECS // 60}min)"
                )
            except Exception:
                pass  # a Slack posting failure must not compound the original one
        return None


def run_loop(tickers: set = None):
    ensure_tables()
    schwab_safety.sync_automation_scope()

    # Config-invariant checks (signals_invariants.py) -- non-blocking, alerted
    # loudly rather than silently: a violation means some other code's
    # assumption is already broken, not that anything is unsafe to keep running.
    # Routed through _guarded (fault-isolated, per automation_principles.md #3)
    # since this runs before the loop's own per-section guarding starts -- a
    # pure-observability diagnostic (e.g. a transient DB lock) must never be
    # able to prevent the daemon itself from starting.
    _invariant_violations = _guarded("invariants", signals_invariants.run_all)
    if _invariant_violations:
        _msg = "\n".join(f"- {v}" for v in _invariant_violations)
        print(f"[invariants] {len(_invariant_violations)} violation(s):\n{_msg}")
        try:
            _post_message(f"⚠️ Config invariant violation(s) at startup:\n{_msg}")
        except Exception:
            pass  # a Slack posting failure must not prevent daemon startup

    # buffering=1 (line-buffered) -- without it this file object block-buffers
    # since it's not a tty, so console output (including any Slack post error)
    # can sit invisible on disk for a long time; found 2026-07-22 debugging a
    # live missing-report incident where the file's mtime was frozen for 10+
    # minutes while the daemon was demonstrably still looping (heartbeat proved
    # it), making the buffered output useless for real-time diagnosis.
    human_fh = open(HUMAN_LOG_PATH, "a", buffering=1)
    sys.stdout = _Tee(sys.__stdout__, human_fh)
    sys.stderr = _Tee(sys.__stderr__, human_fh)
    verbose_fh = open(VERBOSE_LOG_PATH, "a")

    ticker_label = ",".join(sorted(tickers)) if tickers else "all"
    print(f"Signal monitor started  |  poll={POLL_SECS}s  |  tickers={ticker_label}  |  Ctrl+C to stop")

    if SOCKET_MODE:
        from slack_bolt.adapter.socket_mode import SocketModeHandler
        handler = SocketModeHandler(bolt_app, SLACK_APP_TOKEN)
        t = threading.Thread(target=handler.start, daemon=True)
        t.start()
        _resolve_channel_id()
        print("  [slack] Socket Mode active — interactive buttons enabled")
    elif SLACK_HOOK:
        print("  [slack] Webhook mode — no interactive buttons")
    else:
        print("  [info] No Slack config — console only")

    # Account-activity websocket (Part 3, branch C fast path) -- latency
    # improvement only, check_auto_fills keeps polling unconditionally as the
    # always-on fallback if this thread degrades or never comes up.
    threading.Thread(target=schwab_stream.run_stream_forever, daemon=True).start()

    startup_wl = get_watchlist()
    if tickers:
        startup_wl = [n for n in startup_wl if n['ticker'] in tickers]
    _ref_channel, _ref_ts = send_reference_report(startup_wl)
    # 2026-07-22: run_loop previously discarded this return value entirely --
    # a real live incident (startup report sent but never received) had no way
    # to be confirmed/denied afterward since there was no ts to check against
    # Slack directly (chat.getPermalink). Print it so it lands in the
    # now-line-buffered human log immediately, not just trusted silently.
    print(f"  [slack] startup reference report: channel={_ref_channel} ts={_ref_ts}"
          f"{' (no confirmed post -- check for a prior [slack error] line)' if not _ref_channel else ''}")

    # Unconditional at startup, same reasoning as the reference report above:
    # reference_alerted is pre-seeded below with every _REFERENCE_TIMES slot
    # already past "today" marked done, which means the (7,0)-gated coverage-
    # report block further down in the loop will never fire today if the
    # daemon (re)starts any time after 7am -- the normal case, since this
    # project restarts the daemon after most source edits. Without this call,
    # that day's coverage check would be silently skipped entirely: no
    # deviation rows, no Slack post, nothing recording that the check didn't
    # run -- indistinguishable from a clean day in a sticky-ticket model
    # (Opus review finding, this session).
    try:
        _cov_channel, _cov_ts = send_coverage_report(_previous_trading_day(datetime.now()))
        print(f"  [slack] startup coverage report: channel={_cov_channel} ts={_cov_ts}"
              f"{' (no confirmed post -- check for a prior [slack error] line)' if not _cov_channel else ''}")
    except Exception as e:
        print(f"  [error] startup coverage report failed: {e}")

    buy_alerted:        set[tuple] = set()
    sell_alerted:       set[tuple] = set()  # (position_id, bar_ts) — dedups within a bar, not across bars
    paper_sell_alerted: set[tuple] = set()  # same shape, separate set — paper position ids aren't real ones
    dry_run_sell_alerted: set[tuple] = set()  # same shape, for is_dry_run_sim positions (real ids, own set)
    window_alerted:     set[tuple] = set()
    limit_fill_alerted: set[tuple] = set()
    # wl_id -> last hourly bar timestamp checked. Seeded from each open
    # position's real current bar below (not left empty) -- found live
    # 2026-07-24: last_seen_bar.get(ticker) returning None on a fresh restart
    # trivially != last_bar_ts, so at_bar_close evaluates True on the very
    # first poll after ANY restart regardless of real timing (confirmed:
    # restarting at 11:14 ET caused SPY's arm/TP check to fire at 11:21 ET,
    # not a real hourly bar close or pinned exit-arm time). Seeding to the
    # real current bar means a restart's first poll correctly recognizes "no
    # new bar since last real close" instead of "no record at all" == "a new
    # bar just happened."
    last_seen_bar: dict = _seed_last_seen_bar(get_open_positions())
    last_date = datetime.now().strftime('%Y-%m-%d')
    # Slots already past today are pre-marked "done" since the unconditional
    # send_reference_report() above just covered them -- only upcoming slots fire.
    _now0 = datetime.now()
    reference_alerted: set[tuple] = {
        (last_date, f"{rh:02d}:{rm:02d}") for rh, rm in _REFERENCE_TIMES
        if (_now0.hour, _now0.minute) >= (rh, rm)
    }
    _gap_h1, _gap_m1 = _GAP_CHECK_WINDOW[2], _GAP_CHECK_WINDOW[3]
    gap_check_alerted: set[str] = {last_date} if (_now0.hour, _now0.minute) >= (_gap_h1, _gap_m1) else set()
    pinned_bar_alerted: set[tuple] = {
        (last_date, h, m) for h, m, s in _PINNED_BAR_TIMES
        if (_now0.hour, _now0.minute) >= (h, m)
    }

    while True:
        now   = datetime.now()
        today = now.strftime('%Y-%m-%d')
        HEARTBEAT_PATH.write_text(now.strftime('%Y-%m-%d %H:%M:%S'))

        # Outer last-resort net (automation_principles.md #3): every section
        # below is already individually guarded via _guarded()/per-item
        # try-except, so this should rarely trigger -- it exists only to catch
        # whatever an unexpected exception in the glue code between sections
        # (or a bug in a guard itself) would otherwise let crash the daemon.
        try:
            if today != last_date:
                buy_alerted.clear()
                window_alerted.clear()
                limit_fill_alerted.clear()
                reference_alerted.clear()
                gap_check_alerted.clear()
                pinned_bar_alerted.clear()
                last_date = today

            for rh, rm in _REFERENCE_TIMES:
                rlabel = f"{rh:02d}:{rm:02d}"
                rkey = (today, rlabel)
                if (now.hour, now.minute) >= (rh, rm) and rkey not in reference_alerted:
                    reference_alerted.add(rkey)

                    def _send_reference():
                        wl = get_watchlist()
                        if tickers:
                            wl = [n for n in wl if n['ticker'] in tickers]
                        rc, rts = send_reference_report(wl)
                        print(f"  [slack] {rlabel} reference report: channel={rc} ts={rts}"
                              f"{' (no confirmed post -- check for a prior [slack error] line)' if not rc else ''}")
                    _guarded(f"reference_report[{rlabel}]", _send_reference)

                    if (rh, rm) == (7, 0):
                        def _send_coverage():
                            check_date = _previous_trading_day(now)
                            cc, cts = send_coverage_report(check_date)
                            print(f"  [slack] {rlabel} coverage report ({check_date}): channel={cc} ts={cts}"
                                  f"{' (no confirmed post -- check for a prior [slack error] line)' if not cc else ''}")
                        _guarded("coverage_report[07:00]", _send_coverage)

            gap_h0, gap_m0, gap_h1, gap_m1 = _GAP_CHECK_WINDOW
            if (gap_h0, gap_m0) <= (now.hour, now.minute) <= (gap_h1, gap_m1) and today not in gap_check_alerted:
                gap_check_alerted.add(today)
                _guarded("gap_resize", check_gap_resize)

            watchlist = get_watchlist()
            if tickers:
                watchlist = [n for n in watchlist if n['ticker'] in tickers]
            summaries = []

            def _refresh(ticker):
                verbose_fh.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} {ticker} ---\n")
                with contextlib.redirect_stdout(verbose_fh), contextlib.redirect_stderr(verbose_fh):
                    fetch_live_data_smart(ticker)
                verbose_fh.flush()

            refresh_tickers = {p['ticker'] for p in get_open_positions()} | {n['ticker'] for n in watchlist}
            with ThreadPoolExecutor(max_workers=1) as ex:
                for t in sorted(refresh_tickers):
                    try:
                        ex.submit(_refresh, t).result(timeout=15)
                    except FuturesTimeoutError:
                        print(f"  [data] {t} refresh timed out — skipping")
                    except Exception as e:
                        print(f"  [data] {t} refresh failed: {e}")

            # Fire once per window: notify that algo is alive anywhere inside the window
            # (POLL_SECS=300 means we rarely land on the exact opening minute).
            for wh, wm, wh1, wm1 in _SIGNAL_WINDOWS:
                label = f"{wh:02d}:{wm:02d}"
                wkey = (today, label)
                if (wh, wm) <= (now.hour, now.minute) <= (wh1, wm1) and wkey not in window_alerted:
                    window_alerted.add(wkey)
                    _guarded(f"window_alert[{label}]", _send_window_alert, label, watchlist)

            # Exit checks run every poll cycle (not gated to the entry signal windows) —
            # the backtest evaluates TP/SL/TIME on every hourly bar once in a trade, so
            # live monitoring needs to check at least that often, not just twice a day.
            # SL/trailing checks are continuous (every poll); TP/TIME only fire when a
            # genuinely new hourly bar has closed since the last check, using that bar's
            # real Close/Low/High — not a live mid-bar tick — to match the backtest kernels.
            open_positions = get_open_positions()
            paper_positions = get_open_positions(paper=True)
            # Keyed via _pos_key: wl_id (the watch_list row's own PK) when available,
            # else a (ticker, window) fallback for legacy positions that predate the
            # wl_id migration and couldn't be backfilled unambiguously (see
            # docs/backlog_cache.md's wl_id refactor entry). A bare wl_id=None for
            # every such row would let a real live node's duplicate-position
            # suppression silently stop working -- found by a second Opus review
            # round, 2026-07-26 -- so the fallback must still be checkable below.
            open_position_keys = ({_pos_key(p) for p in open_positions}
                                   | {_pos_key(p) for p in paper_positions})

            # Pinned single-shot checks (Part 4) -- fire once per hourly bar boundary,
            # ahead of/instead of relying purely on ambient POLL_SECS-cadence detection.
            for ph, pm, ps in _PINNED_BAR_TIMES:
                pkey = (today, ph, pm)
                if (now.hour, now.minute) >= (ph, pm) and pkey not in pinned_bar_alerted:
                    pinned_bar_alerted.add(pkey)
                    _guarded("pinned_exit_arm", _scan_pinned_exit_arm, open_positions, sell_alerted, last_seen_bar)
                    if (ph, pm) in _PINNED_ENTRY_TIMES and _is_trading_day(today):
                        # This is a second, independent BUY entry point that never
                        # routed through _in_window (which only gates the ambient
                        # POLL_SECS-cadence scan) -- it's plausibly the actual path
                        # the 2026-07-26 ERY Sunday order took, since it's restricted
                        # to AUTOMATION_ENABLED_TICKERS (real-order-eligible) and was
                        # gated only on (hour, minute) plus a dedup set that clears on
                        # every calendar date, weekends included (Opus review finding).
                        # Up to 3 attempts, 5s apart, retrying ONLY tickers whose price
                        # fetch actually failed (a transient schwab_client.get_session_
                        # open_price/get_current_price error) -- _scan_pinned_entry
                        # excludes a failed ticker from its own _scan_buy_signals call
                        # rather than falling through to an ambient/degraded price, so
                        # a successful retry is the only way that ticker ever alerts.
                        # open_position_keys is re-read fresh before every attempt
                        # (NOT the stale once-per-loop-iteration snapshot) -- an
                        # earlier version of this retry reused the stale snapshot
                        # across all 3 attempts, which could place a second real BUY
                        # order for a node whose same-day-unlock branch (line ~263)
                        # only sees a just-filled position as "already held" once
                        # open_position_keys is refreshed (Opus review, 2026-07-26).
                        retry_nodes = watchlist
                        for attempt in range(3):
                            fresh_open_position_keys = (
                                {_pos_key(p) for p in get_open_positions()}
                                | {_pos_key(p) for p in get_open_positions(paper=True)}
                            )
                            res = _guarded(
                                "pinned_entry", _scan_pinned_entry, ph, pm, retry_nodes,
                                buy_alerted, fresh_open_position_keys
                            )
                            result, failed_tickers = res if res is not None else ([], set())
                            summaries += result
                            if not failed_tickers or attempt == 2:
                                break
                            retry_nodes = [n for n in retry_nodes if n['ticker'] in failed_tickers]
                            time.sleep(5)
                        if failed_tickers:
                            # A ticker whose price fetch failed all 3 attempts is
                            # silently skipped for this bar otherwise -- no BUY
                            # alert is possible without a real price, but that
                            # miss must still surface somewhere (automation_
                            # principles.md #4), not just a log_poll line (Opus
                            # review, 2026-07-26).
                            try:
                                _post_message(
                                    f"⚠️ pinned_entry: price fetch failed 3x for "
                                    f"{', '.join(sorted(failed_tickers))} at {ph:02d}:{pm:02d} ET "
                                    f"— entry check skipped this bar"
                                )
                            except Exception:
                                pass  # a Slack posting failure must not crash the poll loop

            def _check_position_exit(pos):
                df_hourly, _ = _load_cache(pos['ticker'])
                if df_hourly is None or df_hourly.empty:
                    return
                last_bar_ts = df_hourly.index[-1]
                if (pos['id'], last_bar_ts) in sell_alerted:
                    return
                at_bar_close = last_seen_bar.get(_pos_key(pos)) != last_bar_ts
                if at_bar_close:
                    last_seen_bar[_pos_key(pos)] = last_bar_ts
                    bar = df_hourly.iloc[-1]
                    cp, low, high, op = float(bar['Close']), float(bar['Low']), float(bar['High']), float(bar['Open'])
                else:
                    cp, _ = _current_price(pos['ticker'])
                    if cp is None:
                        alert_stale_price_exit_suppressed(pos)
                        return
                    low = high = op = cp
                log_poll(f"{pos['ticker']} exit_check bar={last_bar_ts} at_bar_close={at_bar_close} "
                         f"cp={cp:.4f} low={low:.4f} high={high:.4f} op={op:.4f}")
                reason, target, just_activated_trailing = check_sell_condition(
                    pos, cp, now, at_bar_close=at_bar_close, low=low, high=high, open_price=op, df_hourly=df_hourly)
                if just_activated_trailing:
                    notify_trailing_activated(pos, cp)
                if reason:
                    notify_sell_signal(pos, reason, cp, target)
                    sell_alerted.add((pos['id'], last_bar_ts))

            for pos in open_positions:
                if tickers and pos['ticker'] not in tickers:
                    continue
                if pos.get('is_dry_run_sim'):
                    # No real order was ever placed for this position (the account
                    # is dry_run) -- routed through check_dry_run_sim_sells below
                    # instead, which closes immediately rather than waiting on a
                    # Slack button tap confirming a real fill that will never come.
                    continue
                _guarded(f"exit_check[{pos['ticker']}]", _check_position_exit, pos)

            _guarded("paper_check_sells", paper_trading.check_paper_sells, last_seen_bar, paper_sell_alerted, _load_cache)
            _guarded("dry_run_sim_check_sells", check_dry_run_sim_sells,
                     last_seen_bar, dry_run_sell_alerted, _load_cache)
            _guarded("live_state_reconciliation", check_live_state_reconciliation, open_positions)

            if _reminders_active(now):
                _guarded("trailing_reminders", check_trailing_reminders, open_positions)
                _guarded("exit_reminders", check_exit_reminders, open_positions)
                _guarded("buy_reminders", check_buy_reminders)

            # Not gated to market hours -- a GTC trailing order can fill any time it's
            # resting at the broker, and auto-fill-detection is opt-in per ticker anyway
            # (schwab_safety.auto_fill_detection_enabled, off by default).
            _guarded("auto_fills", check_auto_fills, open_positions)

            # Fast-path fill reconciliation (Part 3, branch C) -- cheap, non-blocking
            # drain of whatever schwab_stream's account-activity websocket has queued
            # since the last iteration. check_auto_fills above is the always-on
            # fallback, so this is a latency improvement, not a new dependency.
            _guarded("drain_fill_queue", drain_fill_queue)

            # Same "not gated to a window" reasoning as check_auto_fills above -- a
            # simulated trailing buy can bounce-fill any time after the signal fires.
            _guarded("paper_update_buys", paper_trading.update_paper_buys)
            _guarded("dry_run_update_buys", update_dry_run_buys)

            if not watchlist:
                print(f"[{now.strftime('%H:%M:%S')}] Watch list empty — add nodes with: python active_signals.py add")
                _sleep_until_next_cycle(now)
                continue

            def _check_limit_fill(node):
                fill_key = (node['ticker'], node['window'], today)
                if fill_key in limit_fill_alerted:
                    return
                cp, _ = _current_price(node['ticker'])
                if cp is None:
                    return
                sig = compute_buy_signal(node)
                if sig is None:
                    return
                log_poll(f"{node['ticker']} limit_fill_check cp={cp:.4f} lower_band={sig['lower_band']:.4f}")
                if cp <= sig['lower_band']:
                    limit_fill_alerted.add(fill_key)
                    notify_limit_fill(node, cp, sig['lower_band'])

            # Intrabar fill detection for limit-entry nodes (all day, not just signal window)
            for node in watchlist:
                if node.get('mode') != 'live':
                    continue
                if node.get('strategy') != 'LimitOrderZScoreBreakout':
                    continue
                _guarded(f"limit_fill[{node['ticker']}]", _check_limit_fill, node)

            in_window = _in_buy_window(now)
            in_open_check_window = _in_window(now, _OPEN_CHECK_WINDOWS)
            if in_open_check_window:
                open_check_nodes = [n for n in watchlist if n.get('entry_timing') == 'open_check']
                if open_check_nodes:
                    summaries += _guarded(
                        "scan_buy_open_check", _scan_buy_signals, open_check_nodes, buy_alerted, open_position_keys
                    ) or []
            if in_window:
                summaries += _guarded(
                    "scan_buy_signals", _scan_buy_signals, watchlist, buy_alerted, open_position_keys
                ) or []
            elif not in_open_check_window:
                summaries.append(f"outside signal window — next: 10:25 or 14:55 ET")

            if summaries:
                print(f"[{now.strftime('%H:%M:%S')}] {' | '.join(summaries)}")
        except Exception as e:
            print(f"  [loop] unhandled exception in main iteration: {e}")
            try:
                _post_message(f"🔴 daemon loop iteration crashed: {e} (recovering, continuing to next poll)")
            except Exception:
                pass

        _sleep_until_next_cycle(now)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_list():
    ensure_tables()
    wl = get_watchlist()
    if not wl:
        print("Watch list is empty.")
        return
    hdr = f"{'ID':<4} {'Ticker':<7} {'Win':<4} {'TP':<4} {'SL':<4} {'Hold':<6} {'Label':<20} Added"
    print(hdr)
    print('-' * len(hdr))
    for n in wl:
        print(
            f"{n['id']:<4} {n['ticker']:<7} {n['window']:<4} {_tp_or_arm_pct(n)!s:<4} "
            f"{n['stop_loss']:<4} {n['max_hold_hours']:<6} {(n.get('label') or ''):<20} {n['added_at']}"
        )


def cmd_positions():
    ensure_tables()
    positions = get_open_positions()
    if not positions:
        print("No open positions.")
        return
    hdr = f"{'ID':<4} {'Ticker':<7} {'Entry Price':<13} {'Entry Time':<22} {'Bars Held':<9} {'TP%':<5} {'SL%':<5} {'Hold':<6} {'Sim'}"
    print(hdr)
    print('-' * len(hdr))
    for p in positions:
        signal_time = datetime.strptime(p['signal_time'], '%Y-%m-%d %H:%M:%S')
        df_hourly_p, _ = _load_cache(p['ticker'])
        hours = _bars_held(df_hourly_p, signal_time)
        sim_tag = 'DRY-RUN-SIM' if p.get('is_dry_run_sim') else ''
        print(
            f"{p['id']:<4} {p['ticker']:<7} ${p['entry_price']:<12.4f} "
            f"{p['entry_time']:<22} {hours:<9} {_tp_or_arm_pct(p)!s:<5} "
            f"{p['stop_loss']:<5} {p['max_hold_hours']:<6} {sim_tag}"
        )


def cmd_add():
    ensure_tables()
    print("Add node to watch list (values from backtest_cache):")
    ticker         = input("  ticker: ").strip().upper()
    strategy       = input("  strategy [ZScoreBreakout]: ").strip() or "ZScoreBreakout"
    version        = input("  version [v1.4]: ").strip() or "v1.4"
    window         = int(input("  window: ").strip())
    take_profit    = int(input("  take_profit: ").strip())
    stop_loss      = int(input("  stop_loss: ").strip())
    max_hold_hours    = int(input("  max_hold_hours: ").strip())
    z_score_threshold = float(input("  z_score_threshold [2.0]: ").strip() or "2.0")
    label             = input("  label (optional): ").strip()
    add_node(ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours, label, z_score_threshold)
    print(f"Added {ticker} (w={window} TP={take_profit} SL={stop_loss} hold={max_hold_hours}h Z={z_score_threshold}) label='{label}'.")


def cmd_remove():
    ensure_tables()
    cmd_list()
    if not get_watchlist():
        return
    watch_id = int(input("ID to remove: ").strip())
    remove_node(watch_id)
    print(f"Removed ID {watch_id}.")


_CMDS = {
    'run':       run_loop,
    'list':      cmd_list,
    'add':       cmd_add,
    'remove':    cmd_remove,
    'positions': cmd_positions,
}

if __name__ == '__main__':
    args = sys.argv[1:]
    cmd  = args[0] if args else 'run'

    if cmd in ('run', ) or cmd not in _CMDS:
        tickers = None
        if '--ticker' in args:
            idx     = args.index('--ticker')
            tickers = {t.strip().upper() for t in args[idx + 1].split(',')}
        run_loop(tickers=tickers)
    else:
        _CMDS[cmd]()
