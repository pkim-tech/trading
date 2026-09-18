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
import os
# Must run before signals_config (or anything importing it) is imported --
# SIM_MODE/INTERACTIVE/the Bolt app singleton are computed at module-import
# time there, and signals_config now defaults SIM_MODE to "1" (fail-safe,
# 2026-08-01, after a real incident: an ad hoc test invocation that forgot to
# export SIM_MODE posted a real, unprefixed message to the live channel).
# Gated on __name__ == '__main__' (Python sets this before any of the
# module's top-level code runs, imports included, so it's safe to check this
# early) -- NOT unconditional at module scope. This file's own docstring
# above documents it as also being imported as a library by 11+ scripts
# (`from active_signals import X`), and a bare module-scope setdefault would
# have silently disabled the fail-safe default for every one of them too
# (Opus review, 2026-08-01, verified empirically: `import active_signals`
# alone flips SIM_MODE to False) -- reproducing the exact incident this
# change exists to prevent, just through the library-import path instead of
# a bare script. Only `python active_signals.py [run|list|add|remove|
# positions]` (a real direct invocation) should opt back into real posting;
# os.environ.setdefault leaves an already-exported SIM_MODE=1 (deliberate
# sim run of this same file, if that's ever wanted) untouched either way.
if __name__ == '__main__':
    os.environ.setdefault('SIM_MODE', '0')

import sys
import time
import subprocess
import threading
import contextlib
import fcntl
import functools
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, wait as _futures_wait
from datetime import datetime, timedelta
from pathlib import Path

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
    _tp_or_arm_pct, _is_trailing_buy, add_node, remove_node, set_node_state, label_node,
    get_open_positions, get_held_tickers, add_pending_buy, get_pending_buys,
    clear_pending_buy, mark_pending_buy_placed, update_pending_buy_reminder,
    update_position_trail_state, closed_today, open_position, close_position,
    log_trade_entry, log_trade_exit, _conn,
)
from signals_compute import (
    _load_cache, _current_price, _hurst_adf, compute_buy_signal, _bars_held,
    check_sell_condition, _indicator_cache, _live_tick_price,
)
from signals_charts import _upload_chart, _chart_buy, _chart_sell
from signals_blocks import (
    _post_message, _fields_block, _price_input_block, _shares_input_block,
    _build_buy_blocks, _build_sell_blocks,
)
from signals_helpers import (
    _add_trading_hours, _proximity_emoji, _last_sale_recovery, _phase_emoji, log_poll, _pos_key,
    resolve_at_bar_close, resolve_live_exit_price, mode_tag as _account_mode_tag,
)
from signals_notify import (
    notify_buy_signal, notify_limit_fill, notify_sell_signal,
    TRAIL_REMINDER_MINUTES, _trailing_order_blocks, _supersede_message,
    notify_trailing_activated, check_trailing_reminders,
    EXIT_REMINDER_MINUTES, _exit_pending_blocks, check_exit_reminders, check_own_sell_fills,
    check_sl_order_fills,
    BUY_REMINDER_MINUTES, _trailing_buy_status, _pending_buy_blocks, check_buy_reminders,
    check_auto_fills, check_gap_resize, drain_fill_queue,
    check_live_state_reconciliation, alert_stale_price_exit_suppressed,
    _ticker_block, _send_window_alert, _coverage_mode, _effectively_dry_run,
    _REF_TABLE_COLS, build_reference_table, format_reference_table, _STRATEGY_LABELS,
    send_reference_report, send_coverage_report, build_phased_monitors_report,
    update_dry_run_buys, update_real_pending_buys_running_low, check_dry_run_sim_sells,
    check_entry_abandon, check_market_buy_rejected, build_eod_scenario_review,
    check_drought_entry, check_drought_handoff, check_addon_leg_reconciliation,
    check_intraday_risk_review, check_addon_buying_power_drift,
    check_orphaned_broker_positions,
)
from signals_trade_control import sync_trade_control_channel
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
# The one moment where the backtest's literal bar Open is what's being matched
# (vs. 10:30/14:30/15:30, which approximate the just-closed bar's Close).
# 14:30 was removed 2026-09-17: schwab_client.get_session_open_price returns
# quote["openPrice"] -- the fixed 9:30 session-open print -- which is only a
# genuine "current price" proxy AT 9:30. Calling it at 14:30 returned a stale
# value up to 5 hours old (confirmed live: identical logged prices at 9:30 and
# 14:30 for the same ticker/day in open_price_quality_log, and a mean 1.811%
# drift vs. the real recorded bar over the prior week). 14:30 now takes the
# same get_current_price() live-quote path as 10:30/15:30.
_PINNED_OPEN_TIMES = {(9, 30)}

# Reference report fires once at each of these times daily -- early (7am) so
# there's a report before the day even starts, before the open, and before the
# afternoon signal window, so a fresh full-watchlist view lands ahead of the
# moments an action is most likely to be required. Also fires unconditionally
# on daemon startup/restart, independent of this schedule.
_REFERENCE_TIMES = [(7, 0), (9, 20), (15, 20)]

# End-of-day phased-monitors report (2026-07-30) -- 5 min after the 16:00
# market close, checking TODAY (unlike the 7am coverage report, which checks
# the previous trading day) since pre_action_state_verification/the node
# circuit breaker only accumulate real data during today's own trading.
# Log-only by explicit user call: printed to stdout (captured in
# logs/active_signals.log the same way every other daemon print already is),
# not posted to Slack -- an after-the-fact review artifact, not a daily
# notification.
_EOD_REPORT_TIME = (16, 5)

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


def _position_keys_by_book(real_positions, paper_positions):
    """Duplicate-suppression keys split by BOOK: {'live': {...}, 'paper': {...}}.

    These used to be unioned into one set, which let a simulated position
    silently block a REAL entry. _pos_key returns pos['wl_id'], so a paper row
    and a real row on the same node are indistinguishable once merged -- and a
    node that flips paper->live while its paper position is still open then
    reads as already_held forever after. That happened for real: SOXL/ira
    wl_id=92 ($10k, state='live' since 2026-08-10) carried an open paper
    position until 2026-08-13, so every real BUY on it was dropped silently for
    ~3 trading days. No signal happened to fire, which is luck, not design.

    Each book keeps its own (ticker, window) fallback for NULL-wl_id rows, so
    the 2026-07-26 duplicate-suppression fix is preserved intact WITHIN each
    book -- that fix protects real order placement against a real legacy row,
    and a paper row was never a legitimate reason to suppress a real BUY
    (paper_trading.py never touches open_positions, schwab_client or
    schwab_safety, and does its own dedup via get_open_position_by_wl_id(...,
    paper=True))."""
    return {
        'live': {_pos_key(p) for p in real_positions},
        'paper': {_pos_key(p) for p in paper_positions},
    }


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


def _ambient_buy_scan_nodes(watchlist, now):
    """Watchlist nodes eligible for the main ambient (_SIGNAL_WINDOWS,
    POLL_SECS-cadence) BUY scan -- excludes open_check + automation-enabled
    nodes ONLY before the window's pinned moment (10:30/15:30,
    _scan_pinned_entry), which fetches a precise Schwab session price instead
    of this scan's degraded ambient yfinance fallback. _SIGNAL_WINDOWS starts
    at :25, five minutes before the :30 pinned moment -- an ambient poll
    landing in that :25-:29 gap used to be able to fire first (setting the
    shared buy_alerted dedup key) on the worse price, permanently pre-empting
    the more accurate pinned check once it ran a few minutes later (found in
    the 2026-07-31 audit's still-open list).

    Deliberately NOT excluded for the rest of the window (:30-:40) -- an
    earlier version excluded these nodes for the whole window, which removed
    the ambient scan's role as a fallback when the pinned check didn't
    actually run for this bar (either its own price fetch failed all 3
    retries, or -- more commonly -- `pinned_bar_alerted` was pre-seeded at
    startup for any bar-time already past `now` when the daemon started,
    per automation_principles.md #15, so a restart landing at e.g. 10:33
    skips the 10:30 pinned check entirely and previously left the node with
    literally zero BUY coverage until the next window). By :30 the pinned
    check has already had its one chance to run first and win the dedup key
    for this bar -- ambient scanning after that point can only ever add
    fallback coverage, never pre-empt anything, so there's no reason to keep
    excluding these nodes past that moment (found via review before landing).

    The pre-pinned-check cutoff is derived from _PINNED_ENTRY_TIMES (the real
    pinned moments), not a hardcoded +5 minute offset -- a second review
    round flagged the hardcoded version as correct today but silently wrong
    (either re-opening the pre-emption hole, or over-excluding) if
    _SIGNAL_WINDOWS or _PINNED_BAR_TIMES/_PINNED_ENTRY_TIMES is ever retimed
    independently, given how much of this file's own comment weight is
    about exactly this class of drift."""
    before_pinned_check = False
    for h0, m0, h1, m1 in _SIGNAL_WINDOWS:
        if not ((h0, m0) <= (now.hour, now.minute) <= (h1, m1)):
            continue
        pinned_in_window = [t for t in _PINNED_ENTRY_TIMES if (h0, m0) <= t <= (h1, m1)]
        before_pinned_check = bool(pinned_in_window) and (now.hour, now.minute) < min(pinned_in_window)
        break
    if not before_pinned_check:
        return watchlist
    return [
        n for n in watchlist
        if not (n.get('entry_timing') == 'open_check'
                and n['ticker'] in schwab_safety.AUTOMATION_ENABLED_TICKERS)
    ]


_HOUSEKEEPING_LEAD_SECS = 15  # how far ahead of a pinned target the housekeeping tail pre-fires


def _seconds_until_next_pinned_target(now):
    """Seconds until the next wake target -- either a _PINNED_BAR_TIMES moment
    itself, or _HOUSEKEEPING_LEAD_SECS before one (today's, or if all of
    today's are past, tomorrow's first) -- lets the main loop wake early
    instead of free-running past a target on POLL_SECS cadence. The
    lead-secs-early wake is what lets run_loop's pre-window housekeeping
    trigger (housekeeping_pre_window_alerted) actually fire before the pinned
    target it's meant to precede, not just get noticed late alongside it."""
    candidates = []
    for h, m, s in _PINNED_BAR_TIMES:
        base = now.replace(hour=h, minute=m, second=s, microsecond=0)
        candidates.append(base)
        candidates.append(base - timedelta(seconds=_HOUSEKEEPING_LEAD_SECS))
    # Tomorrow's first target (+ its own pre-window variant) too -- not just a
    # bare fallback -- so the day-boundary rollover gets the same early-wake
    # treatment as every intraday transition, instead of only firing exactly
    # AT that first target with no housekeeping lead time ahead of it.
    h0, m0, s0 = _PINNED_BAR_TIMES[0]
    tomorrow_base = (now + timedelta(days=1)).replace(hour=h0, minute=m0, second=s0, microsecond=0)
    candidates.append(tomorrow_base)
    candidates.append(tomorrow_base - timedelta(seconds=_HOUSEKEEPING_LEAD_SECS))
    target = min(t for t in candidates if t > now)
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
    if limits is None or _effectively_dry_run(account, node):
        return False

    def _check():
        orders = schwab_safety._open_orders(account)
        if schwab_safety._has_open_order(orders, ticker):
            return True
        return schwab_client.get_real_position(account, ticker) > 0

    try:
        # Bounded well under the client's own 10s socket timeout -- this is an
        # optional dedupe check on the timing-critical pinned-entry path (Opus
        # review, 2026-07-27), not a required one; a slow/hung broker call here
        # should fall through to the existing DB-only fallback, not stall the scan.
        with ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_check).result(timeout=5.0)
    except Exception as e:
        print(f"  [warn] {ticker} broker dedupe check failed: {e} — falling back to DB-only check")
        return False


def _fmt_price(sig):
    """Safe '%.4f'-style formatting for sig['current_price'] in a log_poll
    line -- several tests (e.g. test_same_bar_reentry_cooldown.py) monkeypatch
    compute_buy_signal with a minimal test-double dict that deliberately omits
    current_price (it's irrelevant to what they're testing), and a bare
    sig['current_price'] bracket-access in a NEW trace line would KeyError on
    those (found by the full test suite, 2026-09-18, after log_poll tracing
    was added to this function)."""
    cp = sig.get('current_price')
    return f"{cp:.4f}" if cp is not None else "n/a"


def _scan_buy_signals(nodes, buy_alerted, open_position_keys, price_overrides=None):
    """Runs compute_buy_signal over `nodes` and fires notify_buy_signal on new BUYs.
    Shared by the open-check/close-window ambient polls and the pinned single-shot
    checks (_scan_pinned_entry) so a node checked from any of them gets identical
    handling -- price_overrides (ticker -> price) lets the pinned path substitute a
    precise fetched price for the default ambient yfinance lookup inside
    compute_buy_signal."""
    price_overrides = dict(price_overrides or {})
    _caller_override_tickers = set(price_overrides)  # for price_source logging below
    _prefetch_ts = {}  # ticker -> time.time() this batch prefetch completed, for the
                        # staleness re-check right before order placement further down
    # Parallel price pre-fetch (2026-09-17, docs/plans/tick_to_trade_latency_
    # design.md section E) -- ambient/open_check callers pass no
    # price_overrides at all, so compute_buy_signal would otherwise fetch
    # each node's live tick (_live_tick_price, a real yfinance call) one at a
    # time inside the sequential loop below. Pre-fetching concurrently for
    # every ticker not already covered gives the ambient path the same class
    # of speedup the pinned path already gets from _scan_pinned_entry's own
    # parallel fetch, WITHOUT touching the sequential decision/dedup/order-
    # placement logic below -- each node still gets its own compute_buy_signal
    # call, in the same order, just fed a pre-fetched price instead of
    # fetching it inline. daily_sync nodes are excluded from the fetch
    # entirely (not just from using the result) -- they ignore any
    # price_override unconditionally (see compute_buy_signal's daily_sync
    # branch), so fetching for them would be a real network call for nothing.
    # A fetch failure for a ticker simply leaves it OUT of price_overrides --
    # that node's own compute_buy_signal call falls through to ITS normal
    # _live_tick_price call (with the real cached-Close fallback), rather than
    # this prefetch silently injecting a bad price.
    #
    # Gated on `not cfg.SIM_MODE` (independent-cold review, 2026-09-17): several
    # offline tests patch compute_buy_signal directly but never patched
    # _live_tick_price, so this prefetch -- which calls it BEFORE the (mocked)
    # compute_buy_signal ever runs -- was making real yfinance calls (real 404s)
    # during the test suite. SIM_MODE defaults on for any non-daemon invocation
    # (signals_config.py), so this matches the project's existing "never hit a
    # real external API from a test/sim context" convention rather than
    # patching every affected test individually.
    _tickers_to_fetch = sorted({
        n['ticker'] for n in nodes
        if n['ticker'] not in price_overrides and n.get('paper_role') != 'daily_sync'
    }) if not cfg.SIM_MODE else []
    if _tickers_to_fetch:
        _t0 = time.time()
        with ThreadPoolExecutor(max_workers=min(16, len(_tickers_to_fetch))) as _pool:
            _fetched = dict(zip(_tickers_to_fetch,
                                 _pool.map(lambda t: _live_tick_price(t, None), _tickers_to_fetch)))
        _fetch_done_at = time.time()
        for _t, _p in _fetched.items():
            if _p is not None:
                price_overrides[_t] = _p
                _prefetch_ts[_t] = _fetch_done_at
        _ok = sum(1 for v in _fetched.values() if v is not None)
        # Explicit warning line (not just a stat), separate from the routine
        # summary below -- a materially-below-100% fetch rate means multiple
        # nodes are about to silently fall back to a cached hourly Close for
        # their entry decision, the exact failure family behind tonight's
        # 14:30 incident (independent-cold review finding C, 2026-09-17).
        if _ok < len(_tickers_to_fetch):
            log_poll(f"⚠️ ambient_prefetch DEGRADED: only {_ok}/{len(_tickers_to_fetch)} "
                     f"tickers got a live price -- the rest fall back to compute_buy_signal's "
                     f"own cached-Close path this poll")
        log_poll(f"ambient_prefetch tickers={len(_tickers_to_fetch)} ok={_ok} "
                 f"elapsed={_fetch_done_at - _t0:.2f}s prices={ {k: round(v, 4) for k, v in _fetched.items() if v is not None} }")

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
        # Real price source, not just "was a dict key present" (independent-
        # cold review, 2026-09-17: the old version logged BOTH a pinned Schwab
        # price and this function's own ambient yfinance prefetch as identical
        # "override", and logged a daily_sync node as "override" even though
        # compute_buy_signal's daily_sync branch ignores price_override
        # entirely and always uses the cached hourly bar Close). This ordering
        # matches compute_buy_signal's own if/elif precedence exactly.
        if node.get('paper_role') == 'daily_sync':
            _price_source = 'daily_sync_bar'
        elif node['ticker'] in _caller_override_tickers:
            _price_source = 'pinned_schwab'
        elif node['ticker'] in price_overrides:
            _price_source = 'ambient_prefetch'
        else:
            _price_source = 'inline_live_tick'
        # Replay context (independent-cold + contextual review, 2026-09-17):
        # ticker/z-score/price alone aren't enough for a GT-sim replay to
        # recompute what THIS node should have done -- it also needs which
        # strategy/window/z-threshold produced the signal and which
        # account/state this node actually is, without a separate DB join.
        _replay_ctx = (f"strategy={node.get('strategy')} window={node.get('window')} "
                       f"z_thresh={node.get('z_score_threshold', 2.0)} "
                       f"account={node.get('account')} state={node.get('state')} "
                       f"entry_timing={node.get('entry_timing')} price_source={_price_source}")
        if sig is None:
            summaries.append(f"{node['ticker']} w={node['window']} NO_DATA")
            log_poll(f"{node['ticker']} node={node['id']} entry_decision=NO_DATA {_replay_ctx} "
                     f"price_override={price_overrides.get(node['ticker'])}")
            continue

        # Verbose per-decision-point trace (2026-09-17, at the user's explicit
        # request after the 14:30 stale-price incident) -- every node's
        # signal computation logs price/bands/z-score/bar here, BEFORE any
        # dedup/gating logic runs, so a GT-sim replay of this exact moment can
        # be checked against what the daemon actually saw and decided, not
        # just against aggregate stats (the same class of gap that let the
        # 14:30 bug hide undetected for a month -- see docs/plans/
        # tick_to_trade_latency_design.md's root-cause section). This is
        # deliberately unconditional (every node, every poll), not just on a
        # BUY -- the interesting failure mode is often "why did this NOT
        # fire," which needs the non-BUY case logged too.
        log_poll(f"{node['ticker']} node={node['id']} entry_decision "
                 f"price={sig.get('current_price')} lower_band={sig.get('lower_band')} "
                 f"z={sig.get('z_score')} signal={sig['signal']} bar={sig.get('last_bar')} {_replay_ctx}")

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
        # Select the book matching this node's own state (2026-08-15). A
        # paper node is deduped against paper positions, a live/dry_run node
        # against real ones -- never across. dry_run correctly takes the
        # 'live' book: its positions are synthesized into open_positions with
        # is_dry_run_sim=1, not into paper_positions. node['state'] is already
        # load-bearing elsewhere in this same function, so this adds no new
        # dependency.
        _keys = open_position_keys['paper' if node.get('state') == 'paper' else 'live']
        already_held = node['id'] in _keys or (sig['ticker'], node['window']) in _keys

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
        is_paper_node = node.get('state') == 'paper'
        if (alert_key in buy_alerted and not already_held
                and node['id'] not in pending_wl_ids and closed_today(sig['ticker'], paper=is_paper_node, node=node)):
            buy_alerted.discard(alert_key)

        if sig['signal'] == 'BUY' and alert_key not in buy_alerted:
            buy_alerted.add(alert_key)
            if already_held:
                log_poll(f"{sig['ticker']} node={node['id']} entry_decision=SKIP_ALREADY_HELD "
                         f"price={_fmt_price(sig)} z={sig['z_score']:+.2f} bar={sig['last_bar']}")
                print(f"  [skip] BUY {sig['ticker']} z={sig['z_score']:+.2f} — position already open, no alert")
                # Real drought HANDOFF ordering fix (docs/plans/
                # real_order_execution_drought_addon.md 0.6/5.4): in real mode,
                # HANDOFF *initiates* the exit before core's scan runs -- core's
                # entry only lands on a LATER poll once the exit fill confirms.
                # Without this, the blocker here is a drought position (or a
                # still-resting drought entry order), and the once-per-day
                # buy_alerted lockout above never releases (the already_held
                # branch never discards it, unlike the already_pending branch
                # below) -- core's real signal would burn its one alert slot and
                # never re-fire for the rest of the day, even after the HANDOFF
                # exit confirms a poll or two later. Paper never hits this: its
                # HANDOFF is a synchronous DB write, so already_held is already
                # False again by the time this same poll's core scan runs.
                _blocking = db.get_open_position_by_wl_id(node['id'])
                _handoff_in_flight = bool(_blocking) and _blocking.get('position_source') == 'drought_overlay' and (
                    (_blocking.get('trail_state') or {}).get('exit_pending', {}).get('reason') == 'HANDOFF')
                if _handoff_in_flight or db.get_drought_pending_buy(node['id']):
                    buy_alerted.discard(alert_key)
                    # Two distinct result values, not one -- a still-resting
                    # drought entry order (pre-handoff) also releases the slot
                    # here, and compute_status buckets purely on (mode, result)
                    # with bad_results=[] for this row, so a single shared
                    # result string would let that sub-case alone flip this
                    # row to verified-live without the HANDOFF race itself
                    # (the scenario this row exists to document) ever firing
                    # (found by paired Opus review, 2026-08-10).
                    db.log_coverage_event("drought_handoff_alert_slot_preserved", _coverage_mode(node.get('account'), node),
                                           ticker=sig['ticker'], node_id=node['id'],
                                           result="slot_released_handoff" if _handoff_in_flight
                                                  else "slot_released_pending_entry")
            elif node.get('state') != 'paper':
                # Same-bar re-entry cooldown (added 2026-08-14, real incident: RETL/
                # soxl_ira exited via TIME at 09:30:54 on 2026-08-13, a fresh trailing-buy
                # order was placed at 09:31:54 the same bar). The backtest kernel's
                # per-bar loop (_simulate_trail_both, backtester.py) structurally cannot
                # produce this -- an exit processed on bar i only reaches the entry-check
                # branch on bar i+1, never the same iteration -- so live must enforce the
                # same minimum 1-bar gap or it can trade sequences the kernel never
                # validated. sig['last_bar'] is compute_buy_signal's own bar-close
                # reference (last closed hourly bar in cache); last_exit_bar is this
                # node's most recent real trade_log exit_bar_time (populated by
                # _stash_exit_decision_bar at the moment the SELL was decided, not when
                # the fill later confirms). None means either no prior exit or a
                # historical exit predating this fix -- fails open (no cooldown), not
                # closed, since refusing every entry for a node whose history predates
                # this change would be a worse regression than the narrow gap it closes.
                last_exit_bar = db.get_last_exit_bar_time(node['id'])
                same_bar_cooldown = False
                if last_exit_bar is not None and sig.get('last_bar') is not None:
                    # Compare as real datetimes, not strings (found by cold review
                    # 2026-08-14): sig['last_bar'] is a pandas Timestamp -- if that
                    # index were ever tz-aware, str() would append a '-04:00'-style
                    # suffix, making the LONGER string compare lexically greater
                    # regardless of the actual moment in time, silently defeating
                    # the cooldown. Every hourly bar timestamp elsewhere in this
                    # project is naive US/Eastern (see CLAUDE.md); stripped
                    # defensively here rather than assumed, since a bug in that
                    # invariant elsewhere must not also break this comparison.
                    sig_bar = sig['last_bar']
                    if getattr(sig_bar, 'tzinfo', None) is not None:
                        sig_bar = sig_bar.replace(tzinfo=None)
                    try:
                        last_exit_dt = datetime.strptime(last_exit_bar, '%Y-%m-%d %H:%M:%S')
                        same_bar_cooldown = sig_bar <= last_exit_dt
                    except ValueError:
                        # Fail open, never raise into _scan_buy_signals (found by cold
                        # review 2026-08-14): an unparseable exit_bar_time -- a format
                        # this fix's own write side didn't anticipate, or a stray value
                        # from before exit_bar_time was wired up -- must not abort the
                        # entry scan for every other node this poll cycle. Missing this
                        # one node's cooldown is a narrow, acceptable gap; a daemon-wide
                        # crash from a bad string in one row is not.
                        db.log_coverage_event("same_bar_reentry_cooldown", _coverage_mode(node.get('account')),
                                               ticker=sig['ticker'], node_id=node['id'], result="unparseable_exit_bar",
                                               detail=f"last_exit_bar={last_exit_bar!r}")
                # pending_wl_ids alone would have prevented the 2026-07-26 DIA/IWM/
                # QQQ/LABU duplicate-pending-buy incident (a restarted daemon forgot
                # buy_alerted and re-fired a fresh alert/pending_buys row on top of an
                # already-unresolved one) -- it was computed above but never actually
                # checked here. _real_order_or_position_exists adds a broker-truth
                # check on top for real accounts, catching drift pending_wl_ids can't
                # (e.g. a resting order or held position the DB lost track of).
                already_pending = node['id'] in pending_wl_ids or _real_order_or_position_exists(node, sig['ticker'])
                if same_bar_cooldown:
                    # Same handling shape as already_pending below: don't burn today's
                    # alert slot -- a genuinely new bar arriving later today must still
                    # be able to alert, not wait for tomorrow's buy_alerted reset.
                    buy_alerted.discard(alert_key)
                    db.log_coverage_event("same_bar_reentry_cooldown", _coverage_mode(node.get('account')),
                                           ticker=sig['ticker'], node_id=node['id'], result="suppressed",
                                           detail=f"signal_bar={sig['last_bar']} last_exit_bar={last_exit_bar}")
                    print(f"  [cooldown] BUY {sig['ticker']} suppressed — signal bar {sig['last_bar']} "
                          f"not newer than last exit bar {last_exit_bar}")
                elif already_pending:
                    # Don't burn today's alert slot on a suppressed signal (Opus review,
                    # 2026-07-26) -- if the resting order/position clears later today,
                    # the node must still be able to alert same-day, not wait for
                    # tomorrow's buy_alerted reset.
                    buy_alerted.discard(alert_key)
                    # Throttle the Slack message (not the block itself) to once/day --
                    # an unrelated manual order/position that never clears would
                    # otherwise re-alert every poll indefinitely (Opus review, 2026-07-27).
                    already_alerted_today = db.dup_alert_suppressed_today(node['id'])
                    db.log_coverage_event("dup_buy_alert_suppressed", _coverage_mode(node.get('account')),
                                           ticker=sig['ticker'], node_id=node['id'], result="suppressed")
                    if not already_alerted_today:
                        _post_message(f"🔇 {sig['ticker']} ({node.get('account')} · {_account_mode_tag(node.get('account'), node)}) "
                                      f"BUY signal suppressed — already pending/resting at broker or in pending_buys",
                                      node_id=node['id'])
                else:
                    # price_age surfaces a real, NOT-yet-fixed limitation
                    # (contextual review, 2026-09-17): a batch-prefetched
                    # ambient price is captured once, up front, for every node
                    # in this poll -- a node late in this loop can now fire on
                    # a price that's seconds older than it would have been
                    # under the old per-node-fetch-at-its-own-turn behavior,
                    # if an earlier-firing node's own notify_buy_signal call
                    # blocked (real order placement + fill-confirm polling can
                    # take up to ~10s). Logged here so this is visible/
                    # auditable rather than silent; not auto-mitigated tonight
                    # -- a real fix would re-fetch and re-validate the signal
                    # immediately before firing when this value is large,
                    # which is its own change deserving its own review.
                    _price_age = time.time() - _prefetch_ts[sig['ticker']] if sig['ticker'] in _prefetch_ts else 0.0
                    log_poll(f"{sig['ticker']} node={node['id']} entry_decision=FIRE_REAL "
                             f"price={_fmt_price(sig)} z={sig['z_score']:+.2f} "
                             f"bar={sig['last_bar']} account={node.get('account')} "
                             f"price_source={_price_source} price_age={_price_age:.2f}s")
                    notify_buy_signal(node, sig)
            elif sig['ticker'] in schwab_safety.AUTOMATION_ENABLED_TICKERS:
                if paper_trading.start_paper_buy(node, sig):
                    # Cooldown-suppressed (see start_paper_buy's docstring) -- release
                    # the slot so a genuinely new bar later today can still alert,
                    # mirroring the real branch's same_bar_cooldown handling above.
                    buy_alerted.discard(alert_key)
                    print(f"  [cooldown] PAPER BUY {sig['ticker']} suppressed — signal bar "
                          f"not newer than last exit decision bar")
                else:
                    log_poll(f"{sig['ticker']} node={node['id']} entry_decision=FIRE_PAPER "
                             f"price={_fmt_price(sig)} z={sig['z_score']:+.2f} bar={sig['last_bar']}")
                    print(f"  [paper] BUY: {node['ticker']} z={sig['z_score']:+.2f} (paper-trading)")
            else:
                log_poll(f"{sig['ticker']} node={node['id']} entry_decision=RESEARCH_NO_ALERT "
                         f"price={_fmt_price(sig)} z={sig['z_score']:+.2f} bar={sig['last_bar']}")
                print(f"  [research] BUY: {node['ticker']} z={sig['z_score']:+.2f} (no alert)")
        else:
            mode_tag = ' [R]' if node.get('state') == 'paper' else ''
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

    # Parallel fetch (2026-09-17, docs/plans/tick_to_trade_latency_design.md
    # section E) -- real measured 7.5x speedup for 17 tickers (4.34s sequential
    # -> 0.58s threaded), pure reads with no state mutation, so nothing here
    # needs new locking. Each unique ticker fetched exactly once (mirrors the
    # old loop's `if ticker in price_overrides: continue` dedup, just done
    # up front instead of inline) regardless of how many nodes share it.
    _tickers = sorted({n['ticker'] for n in nodes})

    def _fetch_one(ticker):
        try:
            if is_open_check:
                price, is_true_open = schwab_client.get_session_open_price(ticker)
            else:
                price, is_true_open = schwab_client.get_current_price(ticker), False
            return ticker, price, is_true_open, None
        except Exception as e:
            return ticker, None, None, e

    price_overrides = {}
    failed_tickers = set()
    _t0 = time.time()
    with ThreadPoolExecutor(max_workers=min(16, len(_tickers))) as _pool:
        _results = list(_pool.map(_fetch_one, _tickers))
    _elapsed = time.time() - _t0
    for ticker, price, is_true_open, err in _results:
        if err is not None:
            print(f"  [pinned] {ticker} price fetch failed at {target_h:02d}:{target_m:02d}: {err}")
            log_poll(f"{ticker} pinned_entry target={target_h:02d}:{target_m:02d} FETCH FAILED: {err}")
            failed_tickers.add(ticker)
            continue
        price_overrides[ticker] = price
        log_poll(f"{ticker} pinned_entry target={target_h:02d}:{target_m:02d} price={price:.4f} is_true_open={is_true_open}")
        db.log_open_price_quality(ticker, target_h, target_m, price, is_true_open)
    log_poll(f"pinned_entry_fetch target={target_h:02d}:{target_m:02d} tickers={len(_tickers)} "
             f"ok={len(price_overrides)} failed={len(failed_tickers)} elapsed={_elapsed:.2f}s")
    ready_nodes = [n for n in nodes if n['ticker'] not in failed_tickers]
    summaries = _scan_buy_signals(ready_nodes, buy_alerted, open_position_keys, price_overrides=price_overrides)
    return summaries, failed_tickers


def _stash_exit_decision_bar(pos, last_bar_ts):
    """Persists the hourly bar a real SELL was DECIDED on (check_sell_condition returned a
    reason) into trail_state, so close_position() -- which may run much later, once a
    resting order's fill is actually confirmed -- can auto-derive exit_bar_time from it
    without every close_position() call site needing to thread the bar through explicitly.

    Added 2026-08-14 as part of the same-bar re-entry cooldown fix (RETL, 2026-08-13):
    trade_log.exit_bar_time existed in the schema but no real (non-paper) exit path ever
    populated it, so _scan_buy_signals' new cooldown check (below) had nothing to compare
    a fresh entry signal's bar against. Re-fetches trail_state fresh rather than trusting
    the caller's possibly-stale in-memory `pos` -- check_sell_condition may have just
    written its own trail_state update (e.g. exit_forced_by_hold_time) that a blind
    overwrite here would clobber (same hazard the callers' own fresh-refetch comments
    already document for the same reason)."""
    fresh = db.get_position_by_id(pos['id']) or pos
    state = dict(fresh.get('trail_state') or {})
    # .strftime, not str() (found by cold review 2026-08-14): str() on a tz-aware or
    # sub-second-precision pandas Timestamp produces a format _scan_buy_signals'
    # datetime.strptime(..., '%Y-%m-%d %H:%M:%S') read side cannot parse -- an
    # uncaught ValueError there would abort the ENTIRE entry scan (every node,
    # not just this one) inside the _guarded("scan_buy_signals", ...) wrapper.
    # Matches the exact write convention signals_db.py's log_trade_exit/
    # log_trade_entry already use for every other timestamp column.
    state['exit_decision_bar'] = (last_bar_ts.strftime('%Y-%m-%d %H:%M:%S')
                                   if hasattr(last_bar_ts, 'strftime') else str(last_bar_ts))
    db.update_position_trail_state(pos['id'], state)
    return fresh


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
        # Re-fetch fresh, skip if closed (2026-09-17) -- same reasoning as
        # _check_position_exit's own re-fetch. This loop iterates the
        # once-per-poll-cycle open_positions snapshot, which was previously
        # safe to treat as current because nothing earlier in the SAME
        # iteration could close a position before this ran. That's no longer
        # true: the new pre-window housekeeping trigger (housekeeping_pre_
        # window_alerted) can close this exact position via check_own_sell_
        # fills/check_auto_fills seconds earlier in this same iteration.
        # Without this re-fetch, a stale `pos` here could reach
        # notify_sell_signal for an already-closed position (found by
        # independent-cold review, 2026-09-17).
        pos = db.get_position_by_id(pos['id'])
        if pos is None:
            continue
        df_hourly, _ = _load_cache(pos['ticker'])
        if df_hourly is None or df_hourly.empty:
            continue
        last_bar_ts = df_hourly.index[-1]
        if (pos['id'], last_bar_ts) in sell_alerted:
            continue
        if not resolve_at_bar_close(pos, last_bar_ts, last_seen_bar):
            # Either no new bar since the last check, or this is the position's
            # first-ever check (just seeded, deferred to the next genuinely
            # new bar so it isn't graded against pre-entry bar history --
            # 2026-07-27 fix, see resolve_at_bar_close docstring).
            log_poll(f"{pos['ticker']} pinned_exit_arm bar={last_bar_ts} -- SKIPPED (no new bar)")
            continue
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
            # check_sell_condition already persisted the updated trail_state
            # (e.g. exit_forced_by_hold_time) to the DB -- pos here is still
            # the pre-call in-memory copy. Re-fetch fresh so notify_sell_signal/
            # _attempt_automated_exit_sell see it, not a stale copy that would
            # otherwise get written straight back to the DB (clobbering the
            # just-persisted update -- found live 2026-07-31, defeated the
            # 2026-07-29/30 SH hold-time-forced fixes).
            fresh_pos = _stash_exit_decision_bar(pos, last_bar_ts)
            notify_sell_signal(fresh_pos, reason, cp, target)
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


def _alert_violations(log_prefix: str, violations: list, slack_prefix: str):
    """Shared join+print+try/except-wrapped-post plumbing for a startup
    violations list -- sim_mode_check and invariants.run_all() both did the
    identical joined-message + print + try/except _post_message pattern
    inline (2026-08-15 cleanup). Returns early if violations is falsy (call
    sites also guard this themselves before calling, since building a
    caller-side log_prefix can involve len(violations) -- calling
    unconditionally would crash on a None/empty violations list before this
    function's own guard ever ran). Output text unchanged from the
    pre-extraction inline version -- only the plumbing moved, callers keep
    their own exact wording via the prefix params."""
    if not violations:
        return
    _msg = "\n".join(f"- {v}" for v in violations)
    print(f"{log_prefix}{_msg}")
    try:
        _post_message(f"{slack_prefix}{_msg}")
    except Exception:
        pass  # a Slack posting failure must not prevent daemon startup


def _guarded(section: str, fn, *args, **kwargs):
    """Runs fn(*args, **kwargs), catching and logging any exception so one
    failing run_loop section can't crash the whole daemon (automation_principles.md
    #3 -- per-unit failure isolation). Posts a Slack alert on failure so it
    doesn't fail silently (#4), rate-limited per section (a persistent failure
    would otherwise repost every poll cycle). Returns fn's result, or None on
    failure -- callers that expect a list (e.g. summaries += _guarded(...))
    must handle None.

    Section timing (2026-09-17): every _guarded call already funnels through
    this one function, so it's the cheapest single place to get per-section
    duration visibility into run_loop -- found live the same night that a
    76s gap between two logged steps (dry_run_update_buys -> pinned_exit_arm)
    was completely invisible, because none of the ~10 _guarded sections in
    between (check_auto_fills, check_own_sell_fills, check_orphaned_broker_
    positions, etc. -- several make real Schwab API calls) call log_poll
    themselves. Logged via log_poll (verbose log only, same as every other
    per-poll trace line) -- no threshold/alert here, this is purely
    diagnostic visibility, not a new alerting decision."""
    _t0 = time.time()
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
    finally:
        log_poll(f"section={section} elapsed={time.time() - _t0:.2f}s")


def _run_housekeeping_tail(open_positions, last_seen_bar, paper_sell_alerted, dry_run_sell_alerted, now,
                            timeout=None):
    """Runs the daemon's DISCRETIONARY per-cycle housekeeping (broker
    reconciliation, fill-detection, reminders, pending-buy bookkeeping)
    concurrently instead of as a ~31s sequential block -- see docs/plans/
    tick_to_trade_latency_design.md's cost model. None of these gate any order
    decision made elsewhere in this same poll iteration.

    timeout: max seconds this call blocks the caller waiting for jobs to
    finish (None = wait for all, the default/regular-call behavior). A job
    still running past timeout is NOT cancelled (Python threads can't be
    killed) -- it keeps running in its own already-_guarded thread and simply
    isn't waited on further by this call. Exists so the pre-window trigger
    (run_loop) can't itself overrun into the pinned target it's meant to
    finish ahead of.

    Called from two places in run_loop: (1) its original unconditional
    once-per-cycle position, unchanged, and (2) ~15s ahead of each pinned
    bar-time target (housekeeping_pre_window_alerted) so the tail is freshly
    complete by the time that window's own critical path (pinned-check ->
    ambient scan) runs, instead of competing with it for the same thread.

    Safe to run concurrently: WAL mode + busy_timeout (signals_db._conn,
    2026-09-17) let concurrent writers queue instead of raising
    "database is locked"; open_position()/close_position() already serialize
    the one real double-close race via signals_db._position_lock (built for
    the pre-existing poll-loop-vs-Bolt-handler-thread concurrency; extends
    unchanged to these jobs as a 3rd concurrent caller).

    Deliberately NOT included here: check_sl_order_fills/
    check_live_state_reconciliation (safety-critical missing-protective-order/
    auto-close detection -- stays sequential and first in run_loop, per design
    doc section C) and sync_trade_control_channel (must run after every write
    in the cycle completes so it reflects end-of-cycle state -- run_loop still
    calls it sequentially, after this function returns)."""
    jobs = [
        ("paper_check_sells", paper_trading.check_paper_sells,
         (last_seen_bar, paper_sell_alerted, _load_cache)),
        ("dry_run_sim_check_sells", check_dry_run_sim_sells,
         (last_seen_bar, dry_run_sell_alerted, _load_cache)),
        ("intraday_risk_review", check_intraday_risk_review, ()),
        ("addon_buying_power_drift", check_addon_buying_power_drift, ()),
        ("orphaned_broker_positions", check_orphaned_broker_positions, ()),
        ("auto_fills", check_auto_fills, (open_positions,)),
        ("own_sell_fills", check_own_sell_fills, (open_positions,)),
        ("addon_leg_reconciliation", check_addon_leg_reconciliation, (open_positions,)),
        ("drain_fill_queue", drain_fill_queue, ()),
        ("paper_update_buys", paper_trading.update_paper_buys, ()),
        ("dry_run_update_buys", update_dry_run_buys, ()),
        ("real_pending_buys_running_low", update_real_pending_buys_running_low, ()),
        ("check_entry_abandon", check_entry_abandon, ()),
        ("check_market_buy_rejected", check_market_buy_rejected, ()),
    ]
    if _reminders_active(now):
        jobs += [
            ("trailing_reminders", check_trailing_reminders, (open_positions,)),
            ("exit_reminders", check_exit_reminders, (open_positions,)),
            ("buy_reminders", check_buy_reminders, ()),
        ]
    # max_workers capped (not len(jobs)) -- an uncapped pool fires every
    # Schwab-touching job in this batch simultaneously instead of spreading
    # them at all, a real burst-risk flagged by independent-cold review
    # (2026-09-17): near a pinned target this tail can already run twice
    # close together (pre-window trigger + regular call, on a non-pinned-
    # adjacent cycle) or from two different call sites; capping bounds how
    # many concurrent Schwab calls any single invocation can make.
    #
    # No `with` block: `with ThreadPoolExecutor(...):` calls shutdown(wait=True)
    # on exit, which blocks until every job finishes regardless of `timeout`
    # below -- exactly the unbounded-join bug this timeout param exists to
    # avoid (found by contextual review, 2026-09-17). shutdown(wait=False)
    # returns immediately; any job still running past `timeout` keeps running
    # in its own thread (already-isolated via _guarded) until it finishes on
    # its own -- not waited on further by this call.
    pool = ThreadPoolExecutor(max_workers=min(8, len(jobs)))
    futures = [pool.submit(_guarded, name, fn, *args) for name, fn, args in jobs]
    _done, not_done = _futures_wait(futures, timeout=timeout)
    pool.shutdown(wait=False)
    if not_done:
        log_poll(f"housekeeping_tail: {len(not_done)}/{len(jobs)} job(s) still running "
                 f"past {timeout}s timeout -- not waited on further this call")


_RUN_LOCK_FH = None  # module-level so the fd (and its flock) survives for run_loop's whole life


def _acquire_run_lock():
    """Refuse a second concurrent `active_signals.py run` (2026-09-15, added after a real
    incident where two independent instances of a sweep queue script ran concurrently
    against the same campaign, doubling CPU/memory load -- the daemon has no equivalent
    guard today, and two live daemons double-placing orders/Slack alerts against the same
    watch_list would be far worse). flock is non-blocking: a second invocation exits
    immediately instead of silently running alongside the first."""
    global _RUN_LOCK_FH
    lock_path = HEARTBEAT_PATH.parent / "active_signals_run.lock"
    _RUN_LOCK_FH = open(lock_path, "w")
    try:
        fcntl.flock(_RUN_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"Another active_signals.py run is already active (lock: {lock_path}). "
              f"Refusing to start a second daemon.", file=sys.stderr)
        sys.exit(1)


def run_loop(tickers: set = None):
    _acquire_run_lock()
    ensure_tables()
    schwab_safety.sync_automation_scope()

    # Moved here from just before "Signal monitor started" below (found
    # 2026-08-28: every startup diagnostic above that point -- sim_mode_check,
    # signals_invariants.run_all(), print_all_live_node_state, the startup EOD
    # closures -- printed only to sys.__stdout__, never reaching logs/
    # active_signals.log at all, since the Tee redirect used to happen after
    # all of them ran. The real invariant/sim_mode violations still reached
    # Slack (that path doesn't depend on stdout), but anyone checking the log
    # file itself for "what did startup find" saw nothing. buffering=1
    # (line-buffered) -- see the comment this block used to sit under, same
    # reasoning (console output must not block-buffer on a non-tty).
    human_fh = open(HUMAN_LOG_PATH, "a", buffering=1)
    sys.stdout = _Tee(sys.__stdout__, human_fh)
    sys.stderr = _Tee(sys.__stderr__, human_fh)

    # SIM_MODE must be off for a genuine daemon run (see os.environ.setdefault
    # at the top of this file, and signals_config's fail-safe SIM_MODE default,
    # 2026-08-01). Not part of signals_invariants.run_all() below -- that
    # function also runs standalone via the pre-commit checklist, where
    # SIM_MODE=1 is correct/expected and would false-positive there. Checked
    # directly, once, here instead -- non-blocking (alerted, not fatal) since
    # a misconfigured SIM_MODE, while a severe operational problem, isn't a
    # crash-worthy one.
    _sim_mode_violations = _guarded("sim_mode_check", signals_invariants.check_sim_mode_off_for_real_daemon)
    if _sim_mode_violations:
        _alert_violations("[sim_mode] ", _sim_mode_violations, "🚨 ")

    # Config-invariant checks (signals_invariants.py) -- non-blocking, alerted
    # loudly rather than silently: a violation means some other code's
    # assumption is already broken, not that anything is unsafe to keep running.
    # Routed through _guarded (fault-isolated, per automation_principles.md #3)
    # since this runs before the loop's own per-section guarding starts -- a
    # pure-observability diagnostic (e.g. a transient DB lock) must never be
    # able to prevent the daemon itself from starting.
    _invariant_violations = _guarded("invariants", signals_invariants.run_all)
    if _invariant_violations:
        _alert_violations(f"[invariants] {len(_invariant_violations)} violation(s):\n",
                           _invariant_violations, "⚠️ Config invariant violation(s) at startup:\n")

    # Same reasoning as run_all() above -- reference_alerted (below) pre-seeds
    # every _REFERENCE_TIMES slot already past "today" as done, so the 7am-gated
    # live_node_state block further down would silently never fire today if the
    # daemon (re)starts any time after 7am, the normal case here. Found live
    # 2026-07-30: today's actual restart (07:19) would have skipped this report
    # entirely without this unconditional call.
    _guarded("live_node_state[startup]", signals_invariants.print_all_live_node_state)

    # 2026-07-30 Opus review finding (HIGH): the EOD block's Slack-posting half
    # (outcome coverage report + the EOD invariants alert) needs the same
    # startup-catchup + pre-seed pairing reference_alerted/gap_check_alerted
    # already use below -- without it, every daemon restart after 16:05 (this
    # project restarts constantly) would re-post both to Slack, since the
    # un-pre-seeded eod_report_alerted's "no side effects" premise stopped
    # being true the moment Slack-posting calls were added to that block.
    # build_phased_monitors_report/print_all_live_node_state stay un-pre-seeded
    # below (genuinely idempotent log prints, no Slack) -- only these two.
    if (datetime.now().hour, datetime.now().minute) >= _EOD_REPORT_TIME:
        _eod_today = datetime.now().strftime('%Y-%m-%d')

        # 2026-08-15 cleanup: of the 3 startup EOD closures, coverage and
        # scenario_review share the same shape (call a report function
        # returning (channel, ts), print the identical format) -- deduped via
        # this small helper instead of a data-driven loop. A loop was the
        # original spec, but it forces the two calls to sit adjacent, which
        # silently reordered the 3 sections (coverage -> scenario_review ->
        # invariants instead of coverage -> invariants -> scenario_review) --
        # a real behavior change caught by paired review (both independent-
        # cold and contextual agents found it), since it breaks deliberate
        # parity with the live (non-startup) EOD path a few hundred lines
        # down, which runs outcome-check -> readiness-recheck -> review/plan
        # in that specific order on purpose. This helper keeps the dedup
        # without forcing adjacency -- called individually, in the original
        # order, straddling the still-separate invariants closure below.
        def _run_startup_eod_report(guard_label, log_label, fn):
            def _run(fn=fn, log_label=log_label):
                cc, cts = fn(_eod_today)
                print(f"  [slack] {log_label} ({_eod_today}): channel={cc} ts={cts}"
                      f"{' (no confirmed post -- check for a prior [slack error] line)' if not cc else ''}")
            _guarded(guard_label, _run)

        _run_startup_eod_report("coverage_report[EOD:startup]", "startup EOD coverage report",
                                 send_coverage_report)

        def _startup_eod_invariants():
            violations = signals_invariants.run_all()
            if violations:
                msg = "\n".join(f"- {v}" for v in violations)
                print(f"[invariants] {len(violations)} violation(s) at EOD (startup):\n{msg}")
                _post_message(f"⚠️ Config invariant violation(s) ({_eod_today}, EOD check):\n{msg}")
            else:
                print("[invariants] EOD (startup): all invariants hold.")
        _guarded("invariants[EOD:startup]", _startup_eod_invariants)

        _run_startup_eod_report("eod_scenario_review[startup]", "startup EOD scenario review",
                                 build_eod_scenario_review)

    # human_fh/stdout+stderr Tee setup now happens at the top of run_loop()
    # (see the comment there) so startup diagnostics land in the log file too.
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

    # 2026-07-30: the trade-outcome coverage check (send_coverage_report --
    # "did yesterday's designed trade actually close") moved off the 7am
    # start-of-day slot entirely and into the 16:05 EOD slot below, checking
    # *today's* date once the trading day is actually over -- a start-of-day
    # report answering "is this ready to go right now" has no business
    # showing yesterday's outcome, per the user's explicit correction. No
    # startup catch-up call needed here the way the old 7am version required
    # one: eod_report_alerted (below) is deliberately NOT pre-seeded, so a
    # restart any time after 16:05 still fires the EOD block once on the next
    # loop iteration, same restart-safety property the old 7am design needed
    # this call to fake.

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
    # Deliberately NOT pre-seeded like gap_check_alerted/reference_alerted above
    # -- those are pre-seeded because an unconditional startup call (the
    # reference report) or a bounded window (the gap check) already covers a
    # restart after that slot, so re-running would be redundant/wrong. Nothing
    # covers a restart after 16:05 the same way for THIS set -- fine here,
    # since eod_report_alerted only gates the read-only, idempotent log prints
    # (build_phased_monitors_report/live_node_state), so letting it fire once
    # on the very next loop iteration after a late restart is strictly better
    # than silently losing that day's report.
    eod_report_alerted: set[str] = set()
    # Separate from eod_report_alerted above -- this one gates the two
    # Slack-posting EOD pieces (outcome coverage report + invariants alert),
    # which DO need pre-seeding (an unconditional startup call above already
    # covered them if the daemon started after 16:05) -- see the Opus review
    # comment above the startup call for why (2026-07-30).
    eod_slack_alerted: set[str] = {last_date} if (_now0.hour, _now0.minute) >= _EOD_REPORT_TIME else set()
    pinned_bar_alerted: set[tuple] = {
        (last_date, h, m) for h, m, s in _PINNED_BAR_TIMES
        if (_now0.hour, _now0.minute) >= (h, m)
    }
    # Same pre-seed/clear pattern as pinned_bar_alerted above, minute-granularity
    # approximation of the real (target - _HOUSEKEEPING_LEAD_SECS) trigger --
    # close enough to avoid a spurious catch-up fire on restart, same as that set.
    housekeeping_pre_window_alerted: set[tuple] = {
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
                housekeeping_pre_window_alerted.clear()
                eod_report_alerted.clear()
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
                        # "Are we prepared for today?" -- signals_invariants.run_all()
                        # previously only ran once, at daemon startup, so a daemon that
                        # has been running for days (the normal case) never re-checked
                        # config state again. Re-running it here every real trading day
                        # at 07:00 makes it a genuine start-of-day check across the whole
                        # live watchlist (config-invariant checks +
                        # check_staged_config_matches_expected's committed-baseline diff,
                        # 2026-07-30), not just a one-time startup diagnostic.
                        def _check_invariants():
                            violations = signals_invariants.run_all()
                            if violations:
                                msg = "\n".join(f"- {v}" for v in violations)
                                print(f"[invariants] {len(violations)} violation(s) at {rlabel}:\n{msg}")
                                _post_message(f"⚠️ Config invariant violation(s) ({today}):\n{msg}")
                            else:
                                print(f"[invariants] {rlabel}: all invariants hold.")
                        _guarded("invariants[07:00]", _check_invariants)

                        # Per-node state report for the real soxl_ira live tier
                        # (SH/RETL/GDXU/SPY/DPST) and the 13 ira/canary nodes --
                        # console/log-only (same as build_phased_monitors_report),
                        # not Slack -- a config-drift ✗ here doesn't need a page,
                        # just visibility at start of day.
                        _guarded("live_node_state[07:00]", signals_invariants.print_all_live_node_state)

                        # Ground-truth broker sweep -- "does the broker hold
                        # anything real with no local record, or a local
                        # record the broker no longer backs" -- previously
                        # only ever run on-demand (scripts/check_untracked_
                        # positions.py, built after the GDXU incident sat
                        # undetected a week). Wired in here 2026-08-08 per
                        # user's explicit scoping call: auto-run, detect-only,
                        # alert on findings, NEVER auto-correct -- matches
                        # every other "ground truth vs local belief" check in
                        # this project (daily-track reconciliation, node
                        # circuit breaker) that deliberately stays
                        # observation-only, since auto-correction would erase
                        # the exact signal this sweep exists to surface.
                        def _check_untracked_positions():
                            from scripts.check_untracked_positions import run_full_sweep
                            findings = run_full_sweep()
                            if findings:
                                total = sum(len(f) for f in findings.values())
                                lines = []
                                for acct, acct_findings in findings.items():
                                    lines.append(f"*{acct}*")
                                    lines.extend(acct_findings)
                                msg = "\n".join(lines)
                                print(f"[untracked_positions] {rlabel}: {total} finding(s):\n{msg}")
                                _post_message(f"🚨 Untracked/mismatched real position(s) found ({today}):\n{msg}")
                            else:
                                print(f"[untracked_positions] {rlabel}: clean, all accounts matched.")
                        _guarded("untracked_positions[07:00]", _check_untracked_positions)

            gap_h0, gap_m0, gap_h1, gap_m1 = _GAP_CHECK_WINDOW
            if (gap_h0, gap_m0) <= (now.hour, now.minute) <= (gap_h1, gap_m1) and today not in gap_check_alerted:
                gap_check_alerted.add(today)
                _guarded("gap_resize", check_gap_resize)

            if (now.hour, now.minute) >= _EOD_REPORT_TIME and today not in eod_report_alerted:
                eod_report_alerted.add(today)

                def _print_eod_report():
                    print(build_phased_monitors_report(today))
                _guarded("eod_phased_monitors_report", _print_eod_report)
                _guarded("live_node_state[EOD]", signals_invariants.print_all_live_node_state)

                # 2026-08-05: nightly reconcile for daily-track paper nodes (paper_role=
                # 'daily_sync') -- log-only, no Slack, DB-only and idempotent (safe to
                # duplicate on a restart-after-16:05, same reasoning as the other calls in
                # this pre-seeded-but-not-Slack block). PURE OBSERVATION: classifies each
                # node's actual state against a fresh backtest replay (match / explained
                # divergence / unexplained divergence) and logs it in full
                # (db.log_daily_track_reconciliation) -- never mutates daily-track's real
                # paper state. See paper_trading.reconcile_daily_track_nodes.
                def _reconcile_daily_track():
                    n = paper_trading.reconcile_daily_track_nodes()
                    print(f"  [paper] daily-track nightly reconcile: {n} divergence(s) classified")
                _guarded("reconcile_daily_track_nodes", _reconcile_daily_track)

                # 2026-08-09: nightly reconcile for the drought/addon/skim
                # overlay mechanisms -- same log-only, idempotent, pure-
                # observation shape as reconcile_daily_track_nodes above (never
                # mutates any real/paper state). Safe to duplicate on a restart
                # after 16:05 -- overlay_reconciliation_log's own UNIQUE(wl_id,
                # mechanism, mode, check_date) constraint (log_overlay_
                # reconciliation uses INSERT OR REPLACE) makes a same-day
                # rerun refresh that night's row with the latest result
                # instead of duplicating it or letting an earlier transient
                # failure permanently mask a later clean result.
                _guarded("reconcile_overlay_nodes", paper_trading.reconcile_overlay_nodes)

                # 2026-08-20 (docs/backlog_cache.md, specced 2026-08-20): Part 3's real-vs-
                # kernel divergence check was never wired into anything automated -- confirmed
                # nobody ran it 2026-08-18 evening. Run via subprocess, not a direct import:
                # scripts/evening_status.py does `import active_signals as a` at module level,
                # so a direct import here would be circular. Log-only (evening_status.py itself
                # persists structured results to signals_db.divergence_check_log and its own
                # RUN_LOG_PATH), no Slack post -- matches this block's other reconcilers. 30d
                # trailing window means once/day is correct, no value running more often.
                #
                # Paired Opus review (2026-08-20, independent-cold + contextual, both agreed)
                # caught two real gaps in the first version: (1) a non-zero returncode was only
                # printed, never raised -- _guarded's Slack alert + coverage_event only fire on
                # an actual exception, so a persistently-failing run looked wired forever while
                # producing nothing; now raises so _guarded catches it properly. (2) the
                # subprocess ran the WHOLE of part3() (5 sub-parts), and the one sub-part that
                # actually writes divergence_check_log (sub-part 5) runs LAST, behind the
                # slowest piece (_deep_live_parity's bar-by-bar replay, sub-part 4) -- a slow
                # night could exhaust the timeout before the intended output is ever written.
                # --skip-deep-parity (an existing flag evening_status.py already supports)
                # protects the actual target of this item; a human can still run the full
                # `evening_status.py 3` manually anytime for the deep-parity check. Absolute
                # path (not a cwd-relative one) since the daemon's cwd isn't guaranteed to be
                # the repo root.
                def _run_evening_status_part3():
                    script = str(Path(__file__).resolve().parent / "scripts" / "evening_status.py")
                    r = subprocess.run([sys.executable, script, "3", "--skip-deep-parity"],
                                        capture_output=True, text=True, timeout=300)
                    if r.returncode != 0:
                        raise RuntimeError(
                            f"evening_status.py 3 exited rc={r.returncode}: {r.stderr[-500:]}")
                    print("  [evening_status] Part 3 divergence check: ran, "
                          "see logs/evening_status_runs.log and divergence_check_log")
                _guarded("evening_status_part3", _run_evening_status_part3)

                # 2026-08-14: nightly storage trim -- blocks_json (the full
                # Block Kit payload of every Slack message) is 10-50x the size
                # of `text`, and output/live_backups/ keeps 720 uncompressed
                # hourly DB snapshots, so unbounded growth here compounds into
                # ~720x the backup cost within a month. Nulls the column only
                # (never the row) past a rolling 7-day window, so the
                # text-searchable history scripts/recent_slack_messages.py
                # reads stays queryable long-term. DB-only and idempotent
                # (already-nulled rows are skipped), so a duplicate run after
                # a restart-past-16:05 is a no-op -- same shape as the two
                # reconcilers above.
                def _trim_slack_blocks():
                    n = db.trim_old_slack_blocks(days=7)
                    print(f"  [db] slack_message_log blocks_json trim: {n} row(s) nulled")
                _guarded("trim_old_slack_blocks", _trim_slack_blocks)

            # Separate gate from eod_report_alerted above (pre-seeded, unlike
            # it -- see eod_slack_alerted's definition) since these two post
            # to Slack: a duplicate log print is harmless, a duplicate Slack
            # message on every restart after 16:05 is not.
            if (now.hour, now.minute) >= _EOD_REPORT_TIME and today not in eod_slack_alerted:
                eod_slack_alerted.add(today)

                # Moved here 2026-07-30 from the 7am slot -- "did today's
                # designed canary/reconciliation scenario actually play out"
                # is an outcome question, answerable only once the trading
                # day is over, not a start-of-day readiness question. Checks
                # `today`, not the previous trading day, since by 16:05 the
                # day's trading is effectively done.
                def _send_coverage():
                    cc, cts = send_coverage_report(today)
                    print(f"  [slack] EOD coverage report ({today}): channel={cc} ts={cts}"
                          f"{' (no confirmed post -- check for a prior [slack error] line)' if not cc else ''}")
                _guarded("coverage_report[EOD]", _send_coverage)

                # 2026-07-30: the EOD slot mirrors the 7am one, not just "how
                # did today's tests do" -- user's explicit framing is the
                # night session is BOTH "how did our tests do" (outcome check
                # above) AND "are we ready to go, are things staged to test
                # for the following day" (readiness check, same as 7am).
                # Re-running the same readiness checks here catches a
                # same-day config drift (a manual DB edit, a mid-day node
                # add/change) before the *next* morning's report would.
                def _check_invariants_eod():
                    violations = signals_invariants.run_all()
                    if violations:
                        msg = "\n".join(f"- {v}" for v in violations)
                        print(f"[invariants] {len(violations)} violation(s) at EOD:\n{msg}")
                        _post_message(f"⚠️ Config invariant violation(s) ({today}, EOD check):\n{msg}")
                    else:
                        print("[invariants] EOD: all invariants hold.")
                _guarded("invariants[EOD]", _check_invariants_eod)

                # 2026-08-01: the nightly review/plan cycle -- review today's
                # activity across live/canary/paper with explanations, then
                # reset (build tomorrow's daily_plan) -- codified as real code
                # after 5 prior sessions of this not sticking as a manual habit.
                def _eod_scenario_review():
                    cc, cts = build_eod_scenario_review(today)
                    print(f"  [slack] EOD scenario review ({today}): channel={cc} ts={cts}"
                          f"{' (no confirmed post -- check for a prior [slack error] line)' if not cc else ''}")
                _guarded("eod_scenario_review", _eod_scenario_review)

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

            # Runs before the bar-close exit scan below (not just alongside
            # check_own_sell_fills/check_auto_fills later this cycle) -- a
            # real protective stop-loss order is continuously monitored by
            # the broker, not just at our discrete bar-close checks, so it
            # can fill before our own signal check ever computes an exit and
            # tries to act on the now-terminal order. Checking first means a
            # fill that's already visible this cycle closes the position
            # before _check_position_exit below ever gets a chance to 400 on
            # it and post a false "UNPROTECTED" alert. See check_sl_order_fills'
            # docstring (real incident, LABD, 2026-08-07).
            _guarded("sl_order_fills", check_sl_order_fills, open_positions)
            open_positions = get_open_positions()

            # Moved here 2026-08-10 (was after exit_check/paper/dry-run sells
            # below) -- user's call: broker-vs-local-DB state should be
            # verified before this cycle's SELL/BUY actions are considered,
            # same "broker is ground truth, check first" reasoning as
            # check_sl_order_fills immediately above, not just alongside the
            # other post-hoc checks it used to sit next to. Detection/alert-
            # only (never blocks or gates an order), so moving it earlier
            # only changes when a mismatch is *noticed*, not what happens as
            # a result.
            _guarded("live_state_reconciliation", check_live_state_reconciliation, open_positions)

            paper_positions = get_open_positions(paper=True)
            # Keyed via _pos_key: wl_id (the watch_list row's own PK) when available,
            # else a (ticker, window) fallback for legacy positions that predate the
            # wl_id migration and couldn't be backfilled unambiguously (see
            # docs/backlog_cache.md's wl_id refactor entry). A bare wl_id=None for
            # every such row would let a real live node's duplicate-position
            # suppression silently stop working -- found by a second Opus review
            # round, 2026-07-26 -- so the fallback must still be checkable below.
            # Split by BOOK, not unioned (2026-08-15). Unioning real and paper
            # keys let a PAPER position suppress a REAL node's entry: _pos_key
            # returns pos['wl_id'], so a paper row and a live row on the SAME
            # node produce an identical key. This is not hypothetical -- it was
            # armed on a real $10k live node: SOXL/ira wl_id=92 flipped
            # paper->live on 2026-08-10 while its paper position (paper_trade_log
            # id=36) stayed open until 2026-08-13, so for ~3 trading days every
            # real BUY on that node hit already_held and was silently dropped
            # (_scan_buy_signals' skip branch prints only, no Slack, no coverage
            # event). It cost nothing purely because no signal fired in the
            # window. Each book keeps its OWN (ticker, window) fallback intact,
            # so the 2026-07-26 NULL-wl_id duplicate-suppression fix is fully
            # preserved within each book -- what's removed is only paper rows'
            # ability to masquerade as a real node's key.
            open_position_keys = _position_keys_by_book(open_positions, paper_positions)

            # Pre-window housekeeping trigger (2026-09-17) -- fires once per
            # pinned bar-time target, _HOUSEKEEPING_LEAD_SECS ahead of it (the
            # wake-scheduling in _seconds_until_next_pinned_target already
            # wakes the loop this early), so the discretionary housekeeping
            # tail is freshly complete by the time the pinned-check block right
            # below it runs, instead of competing with it for this thread.
            #
            # Window is bounded on BOTH ends (target_dt <= now < pinned_dt) --
            # an earlier version fired unconditionally once now >= target_dt,
            # which meant a late-arriving iteration (previous iteration
            # overran, daemon restart mid-minute) fired the full batch AT OR
            # AFTER the real pinned target, synchronously, immediately ahead of
            # the pinned-check block below -- directly inverting the point of
            # this change (found by contextual review, 2026-09-17). Past the
            # pinned target, this trigger no-ops for that target and the
            # tail's regular once-per-cycle call further down still covers it.
            #
            # timeout=... on the call below bounds how long this specific
            # invocation can block the loop thread -- several of these jobs
            # make real Schwab calls, so an unbounded join here could itself
            # run past the pinned target it's trying to get ahead of. A timed-
            # out job keeps running in its own thread (not killed, Python
            # can't do that); it just isn't waited on further here.
            #
            # housekeeping_done_this_iter skips the tail's unconditional call
            # later in this same iteration if it already ran here seconds ago
            # -- without this, a poll landing inside the lead window fires the
            # full ~14-17-job batch TWICE in the same iteration, seconds apart
            # (not the intended ~15s-before-and-then-separately-on-schedule
            # spacing), roughly doubling real Schwab call volume at exactly the
            # 7 pinned moments/day (found by both paired reviewers, 2026-09-17).
            housekeeping_done_this_iter = False
            for ph, pm, ps in _PINNED_BAR_TIMES:
                hkey = (today, ph, pm)
                pinned_dt = now.replace(hour=ph, minute=pm, second=ps, microsecond=0)
                target_dt = pinned_dt - timedelta(seconds=_HOUSEKEEPING_LEAD_SECS)
                if target_dt <= now < pinned_dt and hkey not in housekeeping_pre_window_alerted:
                    housekeeping_pre_window_alerted.add(hkey)
                    _guarded("housekeeping_pre_window", _run_housekeeping_tail, open_positions,
                             last_seen_bar, paper_sell_alerted, dry_run_sell_alerted, now,
                             timeout=max(1, _HOUSEKEEPING_LEAD_SECS - 3))
                    housekeeping_done_this_iter = True

            # Pinned single-shot checks (Part 4) -- fire once per hourly bar boundary,
            # ahead of/instead of relying purely on ambient POLL_SECS-cadence detection.
            for ph, pm, ps in _PINNED_BAR_TIMES:
                pkey = (today, ph, pm)
                if (now.hour, now.minute) >= (ph, pm) and pkey not in pinned_bar_alerted:
                    pinned_bar_alerted.add(pkey)
                    _guarded("pinned_exit_arm", _scan_pinned_exit_arm, open_positions, sell_alerted, last_seen_bar)
                    if (ph, pm) in _PINNED_ENTRY_TIMES and _is_trading_day(today):
                        # drought-overlay HANDOFF, run here too -- a paired Opus
                        # review of the wiring diff (2026-08-09) found that
                        # _scan_pinned_entry below is the PRIMARY real core-entry
                        # path for every current drought candidate (all
                        # entry_timing='open_check' + automation-enabled), and it
                        # runs well before the in_window/in_open_check_window-gated
                        # HANDOFF loop later in this same iteration -- so a core
                        # signal firing at exactly 9:30/14:30 would see the drought
                        # position still open (already_held via open_position_keys)
                        # and skip, burning that bar's alert, with HANDOFF only
                        # clearing the drought row a poll or more later. Placed here,
                        # HANDOFF now precedes every real core-entry path, not just
                        # the two _scan_buy_signals calls.
                        for node in watchlist:
                            if not node.get('drought_overlay_enabled'):
                                continue
                            # Match _scan_pinned_entry's own real scope exactly
                            # (open_check + automation-enabled) -- an
                            # independent review of this fix found the first
                            # version had NO node filter at all, so it ran
                            # HANDOFF for a close-timing or non-automation
                            # drought node at 9:30/14:30 even though no core
                            # scan for THAT node ever runs at those moments,
                            # closing its position with nothing behind it.
                            if not (node.get('entry_timing') == 'open_check'
                                    and node['ticker'] in schwab_safety.AUTOMATION_ENABLED_TICKERS):
                                continue
                            _guarded(f"drought_handoff_pinned[{node['ticker']}]",
                                     paper_trading.check_paper_drought_handoff, node)
                            # Real sibling, exact same site, inverse mode gate --
                            # check_drought_handoff no-ops immediately for a
                            # research-mode node, so calling both unconditionally
                            # is safe and keeps the paper call's behavior
                            # completely unchanged (docs/plans/
                            # real_order_execution_drought_addon.md 5.5).
                            _guarded(f"drought_handoff_pinned_real[{node['ticker']}]",
                                     check_drought_handoff, node)
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
                            fresh_open_position_keys = _position_keys_by_book(
                                get_open_positions(), get_open_positions(paper=True))
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

            # Moved here 2026-09-17 (was much later in the loop, just before
            # drought ENTRY below) -- the AMBIENT buy scan (_ambient_buy_scan_
            # nodes/_scan_buy_signals) is the fallback path for any signal the
            # pinned entry-check block above missed or failed on (a real
            # incident, ETHU, fired through exactly this ambient path). It used
            # to sit behind ~31s of broker-housekeeping (paper/dry-run sells,
            # reminders, auto-fills, own-sell-fills, addon-leg reconciliation,
            # fill-queue drain, pending-buy updates, entry-abandon/market-buy-
            # rejected checks) further down -- see docs/plans/
            # tick_to_trade_latency_design.md section D. Moved to run
            # immediately after the pinned block instead, ahead of that
            # housekeeping. The drought-overlay HANDOFF block below (which
            # MUST run before this poll's own core buy-signal scans, per its
            # own docstring) moves with it, in the same relative order. The
            # drought-overlay ENTRY block stays in its original position
            # further down (it only needs to run AFTER the core buy-signal
            # scans, which is still true now that they run earlier) --
            # ambient_eligible_ids/open_position_keys computed here remain
            # valid there since normal Python scoping, not loop position,
            # governs when a value is visible.
            in_window = _in_buy_window(now)
            in_open_check_window = _in_window(now, _OPEN_CHECK_WINDOWS)

            # 2026-08-09: drought-overlay HANDOFF -- MUST run BEFORE this poll's
            # own core buy-signal scans below (paper_trading.
            # check_paper_drought_handoff's own docstring states this ordering
            # requirement explicitly; this is where it's actually enforced).
            # Gated to the same real signal-check windows core's own entry scan
            # uses -- compute_buy_signal itself has no window gating, so calling
            # this unconditionally every poll could close a drought position on
            # a signal that only transiently existed between real window checks
            # (found by the independent Opus review during the paper-trading
            # build, MEDIUM-9). Node-aware gate (only in_open_check_window for an
            # open_check-timing node, matching which scan actually runs for it
            # this poll) -- a paired review of the wiring itself found the
            # original blanket `in_window or in_open_check_window` gate ran
            # HANDOFF for a close-timing node during the 9:31-9:40/14:31-14:40
            # open_check windows even though no core scan runs for that node
            # then, closing a drought position with nothing behind it.
            # open_position_keys is refreshed immediately after (not left stale)
            # -- the same review found the scans below would otherwise still see
            # a just-closed drought row as "already held" via the pre-handoff
            # snapshot taken earlier in this iteration.
            # Reuses _ambient_buy_scan_nodes' OWN real decision (not a
            # re-derived approximation of it) for which nodes the ambient
            # scan actually processes this exact poll -- an independent
            # review found the first version's `in_window` branch above
            # treated every node as eligible, missing that function's own
            # :25-:29 pre-pinned exclusion for open_check+automation-enabled
            # nodes. Reusing the real function directly means this can never
            # drift from it the way a second hand-copied condition could.
            ambient_eligible_ids = {n['id'] for n in _ambient_buy_scan_nodes(watchlist, now)} if in_window else set()
            handoff_ran = False
            for node in watchlist:
                if not node.get('drought_overlay_enabled'):
                    continue
                node_in_window = ((in_window and node['id'] in ambient_eligible_ids)
                                  or (in_open_check_window and node.get('entry_timing') == 'open_check'))
                if node_in_window:
                    _guarded(f"drought_handoff[{node['ticker']}]", paper_trading.check_paper_drought_handoff, node)
                    # Real sibling, exact same site/gate, inverse mode --
                    # no-ops for a research-mode node (docs/plans/
                    # real_order_execution_drought_addon.md 5.5).
                    _guarded(f"drought_handoff_real[{node['ticker']}]", check_drought_handoff, node)
                    handoff_ran = True
            if handoff_ran:
                open_position_keys = _position_keys_by_book(
                    get_open_positions(), get_open_positions(paper=True))

            if in_open_check_window:
                open_check_nodes = [n for n in watchlist if n.get('entry_timing') == 'open_check']
                if open_check_nodes:
                    summaries += _guarded(
                        "scan_buy_open_check", _scan_buy_signals, open_check_nodes, buy_alerted, open_position_keys
                    ) or []
            if in_window:
                summaries += _guarded(
                    "scan_buy_signals", _scan_buy_signals, _ambient_buy_scan_nodes(watchlist, now),
                    buy_alerted, open_position_keys
                ) or []
            elif not in_open_check_window:
                windows = " or ".join(f"{h0:02d}:{m0:02d}" for h0, m0, _, _ in _SIGNAL_WINDOWS)
                summaries.append(f"outside signal window — next: {windows} ET")

            def _check_position_exit(pos):
                # Re-fetch fresh before check_sell_condition -- pos here is still
                # the once-per-poll-cycle open_positions snapshot (top of this
                # iteration), and check_sell_condition itself internally merges/
                # persists trail_state, so calling it against a stale snapshot
                # risks clobbering a real concurrent update (the Slack-handler-
                # thread race the SH stuck-exit bug family is built from).
                # Originally added defensively (found in the 2026-07-31 audit's
                # still-open list) when this call always ran before any writer
                # inside the same poll iteration touched this position -- that
                # stopped being true 2026-09-17, when the new pre-window
                # housekeeping trigger started running check_own_sell_fills/
                # check_auto_fills earlier in this same iteration, so this
                # re-fetch is now load-bearing, not just a hardening measure
                # (see _scan_pinned_exit_arm's matching re-fetch, added same
                # night once the earlier version of this comment was found
                # stale by review). A None return means the position was closed
                # concurrently since the snapshot was taken -- nothing left to
                # check.
                fresh_pos = db.get_position_by_id(pos['id'])
                if fresh_pos is None:
                    return
                pos = fresh_pos
                df_hourly, _ = _load_cache(pos['ticker'])
                if df_hourly is None or df_hourly.empty:
                    return
                last_bar_ts = df_hourly.index[-1]
                if (pos['id'], last_bar_ts) in sell_alerted:
                    return
                at_bar_close = resolve_at_bar_close(pos, last_bar_ts, last_seen_bar)
                if at_bar_close:
                    bar = df_hourly.iloc[-1]
                    cp, low, high, op = float(bar['Close']), float(bar['Low']), float(bar['High']), float(bar['Open'])
                else:
                    # resolve_live_exit_price (real Schwab quote), NOT
                    # _current_price (cached hourly bar Close) -- found live
                    # 2026-08-19, SOXS: an 18-minute-stale cached Close fed a
                    # false SL classification on a brand-new position that
                    # was never actually below its stop. See
                    # resolve_live_exit_price's docstring for the full
                    # incident. A None return (network/quote failure) is
                    # still handled the same fail-safe way as the old
                    # _current_price None case below.
                    cp = resolve_live_exit_price(pos['ticker'])
                    if cp is None:
                        # There's nothing actionable about a failed live quote
                        # off-hours -- only alert during real trading hours
                        # (found live: would otherwise repost every 15min, all
                        # night/weekend, per open position). NOT
                        # _reminders_active's 9:00 window -- the day's first
                        # bar isn't even fresh yet until ~9:35 (9:30 bar +
                        # poll/refresh lag), so reusing that window verbatim
                        # would still fire 2-3 pure-noise alerts every trading
                        # morning (Opus review, 2026-07-31).
                        if _is_trading_day(today) and (9, 35) <= (now.hour, now.minute) <= (16, 0):
                            alert_stale_price_exit_suppressed(pos)
                        return
                    low = high = op = cp
                log_poll(f"{pos['ticker']} exit_check bar={last_bar_ts} at_bar_close={at_bar_close} "
                         f"cp={cp:.4f} low={low:.4f} high={high:.4f} op={op:.4f}")
                reason, target, just_activated_trailing = check_sell_condition(
                    pos, cp, now, at_bar_close=at_bar_close, low=low, high=high, open_price=op, df_hourly=df_hourly)
                # Structured, queryable record of the exact inputs and outcome of
                # every real exit-check decision -- added 2026-08-19 after the
                # SOXS stale-price incident took a multi-tool-call log-archaeology
                # session to diagnose, with zero durable trace beyond a free-text
                # log_poll line in a multi-hundred-MB file. price_source records
                # WHICH of the two price paths fed this decision (the actual root
                # cause of that incident was silent otherwise) -- entry_price is
                # included so a future SL/TP misfire is auditable without a
                # separate trade_log join.
                db.log_coverage_event(
                    "exit_check_decision", _coverage_mode(pos.get('account')), ticker=pos['ticker'],
                    position_id=pos.get('id'), node_id=pos.get('wl_id'), result=reason or "HOLD",
                    detail=f"at_bar_close={at_bar_close} price_source={'bar_close' if at_bar_close else 'live_quote'} "
                           f"entry_price={pos.get('entry_price')} cp={cp:.4f} low={low:.4f} high={high:.4f} op={op:.4f}")
                if just_activated_trailing:
                    notify_trailing_activated(pos, cp)
                if reason:
                    # See the matching comment in _scan_pinned_exit_arm above --
                    # re-fetch fresh so this call sees check_sell_condition's
                    # just-persisted trail_state, not the stale pre-call pos.
                    fresh_pos = _stash_exit_decision_bar(pos, last_bar_ts)
                    notify_sell_signal(fresh_pos, reason, cp, target)
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

            # Discretionary housekeeping (broker reconciliation, fill-detection,
            # reminders, pending-buy bookkeeping) -- runs concurrently via
            # _run_housekeeping_tail (2026-09-17) instead of as a ~31s sequential
            # block; see that function's docstring and docs/plans/
            # tick_to_trade_latency_design.md. Also invoked ~15s ahead of each
            # pinned bar-time target earlier in this same iteration (see
            # housekeeping_pre_window_alerted above) -- skipped here if that
            # already ran THIS iteration (housekeeping_done_this_iter), so a
            # poll landing inside the lead window doesn't run the full batch
            # twice seconds apart. A cycle that isn't near a pinned target
            # still gets full coverage here, unchanged.
            if not housekeeping_done_this_iter:
                _run_housekeeping_tail(open_positions, last_seen_bar, paper_sell_alerted,
                                        dry_run_sell_alerted, now)

            # Dedicated trade-control channel (2026-08-17). Deliberately last
            # and deliberately unconditional: every step above may have just
            # changed the state it renders, and it must reflect the END of the
            # cycle, not a mid-cycle snapshot. Inert unless
            # SLACK_TRADE_CONTROL_CHANNEL is configured, DB-only (no broker or
            # price calls), and only talks to Slack when a card's content
            # actually changed -- see signals_trade_control's module docstring.
            # Deliberately NOT passed this loop's `watchlist`: that list is both
            # scoped to the ACTIVE watchlist (real live nodes span several) and
            # filtered by the --tickers CLI option, so a filtered debugging run
            # would see every other real-live node as "gone". The sync resolves
            # its own scope via db.get_live_nodes().
            _guarded("trade_control_sync", sync_trade_control_channel)

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
                if node.get('state') == 'paper':
                    continue
                if node.get('strategy') != 'LimitOrderZScoreBreakout':
                    continue
                _guarded(f"limit_fill[{node['ticker']}]", _check_limit_fill, node)

            # in_window/in_open_check_window/HANDOFF/the ambient+open_check buy
            # scans now run earlier in this function -- see the 2026-09-17
            # comment right after the pinned entry-check block above. They no
            # longer live here; ambient_eligible_ids and open_position_keys
            # computed there are still valid by the time drought ENTRY below
            # uses them.

            # drought-overlay ENTRY -- MUST run AFTER the core buy-signal scans
            # above (the opposite ordering from HANDOFF), so a core signal that
            # fired THIS poll already has its own pending-buy/position created
            # first; check_paper_drought_entry's own get_paper_pending_buy/
            # get_open_position_by_wl_id check then correctly sees that and
            # skips, letting core win ties rather than racing it. Same
            # node-aware window gate as HANDOFF above, for the same reason --
            # reuses the SAME ambient_eligible_ids computed before the
            # HANDOFF loop (still valid, `now` hasn't changed this iteration).
            for node in watchlist:
                if not node.get('drought_overlay_enabled'):
                    continue
                node_in_window = ((in_window and node['id'] in ambient_eligible_ids)
                                  or (in_open_check_window and node.get('entry_timing') == 'open_check'))
                if node_in_window:
                    _guarded(f"drought_entry[{node['ticker']}]", paper_trading.check_paper_drought_entry, node)
                    # Real sibling, exact same site/gate/ordering, inverse mode --
                    # no-ops for a research-mode node (docs/plans/
                    # real_order_execution_drought_addon.md 5.5).
                    _guarded(f"drought_entry_real[{node['ticker']}]", check_drought_entry, node)

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
    max_hold_hours    = int(input(
        "  max_hold_hours (trading-HOUR BARS elapsed, not wall-clock hours -- "
        "~7 bars/trading day, so this runs slower than calendar time across "
        "weekends/off-hours; see signals_compute._bars_held): ").strip())
    z_score_threshold = float(input("  z_score_threshold [2.0]: ").strip() or "2.0")
    label             = input("  label (optional): ").strip()
    fixed_sl_override = None
    if strategies.uses_fixed_sl(strategy):
        fixed_sl_override = float(input("  fixed_sl_override (real per-node SL %): ").strip())
    add_node(ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours, label, z_score_threshold,
             fixed_sl_override=fixed_sl_override)
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
