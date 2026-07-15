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
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime

from data_manager import fetch_live_data_smart
import strategies

import signals_config as cfg
import signals_db as db
import signals_compute as compute

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
    _add_trading_hours, _proximity_emoji, _last_sale_recovery, _phase_emoji,
)
from signals_notify import (
    notify_buy_signal, notify_limit_fill, notify_sell_signal,
    TRAIL_REMINDER_MINUTES, _trailing_order_blocks, _supersede_message,
    notify_trailing_activated, check_trailing_reminders,
    EXIT_REMINDER_MINUTES, _exit_pending_blocks, check_exit_reminders,
    BUY_REMINDER_MINUTES, _trailing_buy_status, _pending_buy_blocks, check_buy_reminders,
    _ticker_block, _send_window_alert,
    _REF_TABLE_COLS, build_reference_table, format_reference_table, _STRATEGY_LABELS,
    send_reference_report,
)
import signals_handlers  # noqa: F401 -- import registers Bolt handlers as a side effect


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

# Signal windows in ET: 10:25-10:40 (9:30 bar close) and 15:25-15:40 (14:30 bar close)
_SIGNAL_WINDOWS = [(10, 25, 10, 40), (15, 25, 15, 40)]

# Reference report fires once at each of these times daily -- early (7am) so
# there's a report before the day even starts, before the open, and before the
# afternoon signal window, so a fresh full-watchlist view lands ahead of the
# moments an action is most likely to be required. Also fires unconditionally
# on daemon startup/restart, independent of this schedule.
_REFERENCE_TIMES = [(7, 0), (9, 20), (15, 20)]


def _reminders_active(now):
    """Reminders only nag during market hours (9:00-16:00) -- outside that
    window they'd just pile up overnight/pre-market with nothing anyone can
    act on, so this pauses them and they pick back up fresh at 9:00."""
    return (9, 0) <= (now.hour, now.minute) <= (16, 0)


def _in_buy_window(now):
    t = (now.hour, now.minute)
    for h0, m0, h1, m1 in _SIGNAL_WINDOWS:
        if (h0, m0) <= t <= (h1, m1):
            return True
    return False


def run_loop(tickers: set = None):
    ensure_tables()

    human_fh = open(HUMAN_LOG_PATH, "a")
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

    startup_wl = get_watchlist()
    if tickers:
        startup_wl = [n for n in startup_wl if n['ticker'] in tickers]
    send_reference_report(startup_wl)

    buy_alerted:        set[tuple] = set()
    sell_alerted:       set[tuple] = set()  # (position_id, bar_ts) — dedups within a bar, not across bars
    window_alerted:     set[tuple] = set()
    limit_fill_alerted: set[tuple] = set()
    last_seen_bar:      dict       = {}   # ticker -> last hourly bar timestamp checked
    last_date = datetime.now().strftime('%Y-%m-%d')
    # Slots already past today are pre-marked "done" since the unconditional
    # send_reference_report() above just covered them -- only upcoming slots fire.
    _now0 = datetime.now()
    reference_alerted: set[tuple] = {
        (last_date, f"{rh:02d}:{rm:02d}") for rh, rm in _REFERENCE_TIMES
        if (_now0.hour, _now0.minute) >= (rh, rm)
    }

    while True:
        now   = datetime.now()
        today = now.strftime('%Y-%m-%d')
        HEARTBEAT_PATH.write_text(now.strftime('%Y-%m-%d %H:%M:%S'))

        if today != last_date:
            buy_alerted.clear()
            window_alerted.clear()
            limit_fill_alerted.clear()
            reference_alerted.clear()
            last_date = today

        for rh, rm in _REFERENCE_TIMES:
            rlabel = f"{rh:02d}:{rm:02d}"
            rkey = (today, rlabel)
            if (now.hour, now.minute) >= (rh, rm) and rkey not in reference_alerted:
                reference_alerted.add(rkey)
                wl = get_watchlist()
                if tickers:
                    wl = [n for n in wl if n['ticker'] in tickers]
                send_reference_report(wl)

        watchlist = get_watchlist()
        if tickers:
            watchlist = [n for n in watchlist if n['ticker'] in tickers]
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
                _send_window_alert(label, watchlist)

        # Exit checks run every poll cycle (not gated to the entry signal windows) —
        # the backtest evaluates TP/SL/TIME on every hourly bar once in a trade, so
        # live monitoring needs to check at least that often, not just twice a day.
        # SL/trailing checks are continuous (every poll); TP/TIME only fire when a
        # genuinely new hourly bar has closed since the last check, using that bar's
        # real Close/Low/High — not a live mid-bar tick — to match the backtest kernels.
        open_positions = get_open_positions()
        open_position_keys = {(p['ticker'], p['window']) for p in open_positions}
        for pos in open_positions:
            if tickers and pos['ticker'] not in tickers:
                continue
            df_hourly, _ = _load_cache(pos['ticker'])
            if df_hourly is None or df_hourly.empty:
                continue
            last_bar_ts = df_hourly.index[-1]
            if (pos['id'], last_bar_ts) in sell_alerted:
                continue
            at_bar_close = last_seen_bar.get(pos['ticker']) != last_bar_ts
            if at_bar_close:
                last_seen_bar[pos['ticker']] = last_bar_ts
                bar = df_hourly.iloc[-1]
                cp, low, high = float(bar['Close']), float(bar['Low']), float(bar['High'])
            else:
                cp, _ = _current_price(pos['ticker'])
                if cp is None:
                    continue
                low = high = cp
            reason, target, just_activated_trailing = check_sell_condition(
                pos, cp, now, at_bar_close=at_bar_close, low=low, high=high, df_hourly=df_hourly)
            if just_activated_trailing:
                notify_trailing_activated(pos, cp)
            if reason:
                notify_sell_signal(pos, reason, cp, target)
                sell_alerted.add((pos['id'], last_bar_ts))

        if _reminders_active(now):
            check_trailing_reminders(open_positions)
            check_exit_reminders(open_positions)
            check_buy_reminders()

        if not watchlist:
            print(f"[{now.strftime('%H:%M:%S')}] Watch list empty — add nodes with: python active_signals.py add")
            time.sleep(POLL_SECS)
            continue

        # Intrabar fill detection for limit-entry nodes (all day, not just signal window)
        for node in watchlist:
            if node.get('mode') != 'live':
                continue
            if node.get('strategy') != 'LimitOrderZScoreBreakout':
                continue
            fill_key = (node['ticker'], node['window'], today)
            if fill_key in limit_fill_alerted:
                continue
            cp, _ = _current_price(node['ticker'])
            if cp is None:
                continue
            sig = compute_buy_signal(node)
            if sig is None:
                continue
            if cp <= sig['lower_band']:
                limit_fill_alerted.add(fill_key)
                notify_limit_fill(node, cp, sig['lower_band'])

        in_window = _in_buy_window(now)
        summaries = []
        if in_window:
            for node in watchlist:
                sig = compute_buy_signal(node)
                if sig is None:
                    summaries.append(f"{node['ticker']} w={node['window']} NO_DATA")
                    continue

                alert_key = (sig['ticker'], node['strategy'], sig['window'])

                if sig['signal'] == 'BUY' and alert_key not in buy_alerted:
                    buy_alerted.add(alert_key)
                    if (sig['ticker'], sig['window']) in open_position_keys:
                        print(f"  [skip] BUY {sig['ticker']} z={sig['z_score']:+.2f} — position already open, no alert")
                    elif node.get('mode', 'live') == 'live':
                        notify_buy_signal(node, sig)
                    else:
                        print(f"  [research] BUY: {node['ticker']} z={sig['z_score']:+.2f} (no alert)")
                else:
                    mode_tag = ' [R]' if node.get('mode') == 'research' else ''
                    summaries.append(
                        f"{sig['ticker']}{mode_tag} z={sig['z_score']:+.2f} {sig['signal']}"
                    )
        else:
            summaries.append(f"outside signal window — next: 10:25 or 14:55 ET")

        if summaries:
            print(f"[{now.strftime('%H:%M:%S')}] {' | '.join(summaries)}")

        time.sleep(POLL_SECS)


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
    hdr = f"{'ID':<4} {'Ticker':<7} {'Entry Price':<13} {'Entry Time':<22} {'Bars Held':<9} {'TP%':<5} {'SL%':<5} {'Hold'}"
    print(hdr)
    print('-' * len(hdr))
    for p in positions:
        signal_time = datetime.strptime(p['signal_time'], '%Y-%m-%d %H:%M:%S')
        df_hourly_p, _ = _load_cache(p['ticker'])
        hours = _bars_held(df_hourly_p, signal_time)
        print(
            f"{p['id']:<4} {p['ticker']:<7} ${p['entry_price']:<12.4f} "
            f"{p['entry_time']:<22} {hours:<9} {_tp_or_arm_pct(p)!s:<5} "
            f"{p['stop_loss']:<5} {p['max_hold_hours']}"
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
