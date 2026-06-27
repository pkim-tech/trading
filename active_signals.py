#!/usr/bin/env python3
"""
Active signal monitor. Polls cached price data and fires BUY/SELL notifications.

Usage:
    python active_signals.py          # run signal loop
    python active_signals.py list     # show watch list
    python active_signals.py add      # add a node interactively
    python active_signals.py remove   # remove a node interactively
    python active_signals.py positions  # show open positions

Environment:
    SLACK_WEBHOOK_URL   — incoming webhook URL for notifications
    SIGNAL_POLL_SECS    — poll interval in seconds (default 300)
"""

import os
import sys
import time
import sqlite3
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv
import strategies

load_dotenv()

DB_PATH    = Path("./cache/trading_universe.db")
CACHE_DIR  = Path("./cache")
POLL_SECS  = int(os.environ.get("SIGNAL_POLL_SECS", 300))
SLACK_HOOK = os.environ.get("SLACK_WEBHOOK_URL", "")


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def ensure_tables():
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS watch_list (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker         TEXT NOT NULL,
                strategy       TEXT NOT NULL,
                version        TEXT NOT NULL,
                window         INTEGER NOT NULL,
                take_profit    INTEGER NOT NULL,
                stop_loss      INTEGER NOT NULL,
                max_hold_hours INTEGER NOT NULL,
                label          TEXT DEFAULT '',
                added_at       TEXT DEFAULT (datetime('now')),
                UNIQUE(ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours)
            );

            CREATE TABLE IF NOT EXISTS open_positions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker         TEXT NOT NULL,
                strategy       TEXT NOT NULL,
                version        TEXT NOT NULL,
                window         INTEGER NOT NULL,
                take_profit    INTEGER NOT NULL,
                stop_loss      INTEGER NOT NULL,
                max_hold_hours INTEGER NOT NULL,
                signal_price   REAL NOT NULL,
                signal_time    TEXT NOT NULL,
                entry_price    REAL NOT NULL,
                entry_time     TEXT NOT NULL
            );
        """)
        c.commit()


# ---------------------------------------------------------------------------
# Watch list CRUD (called by Streamlit picker and CLI)
# ---------------------------------------------------------------------------

def get_watchlist():
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM watch_list ORDER BY ticker, id"
        ).fetchall()]


def add_node(ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours, label=''):
    with _conn() as c:
        c.execute("""
            INSERT OR IGNORE INTO watch_list
                (ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours, label)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (ticker, strategy, version, int(window), int(take_profit),
              int(stop_loss), int(max_hold_hours), label))
        c.commit()


def remove_node(watch_id):
    with _conn() as c:
        c.execute("DELETE FROM watch_list WHERE id = ?", (watch_id,))
        c.commit()


def label_node(watch_id, label):
    with _conn() as c:
        c.execute("UPDATE watch_list SET label = ? WHERE id = ?", (label, watch_id))
        c.commit()


# ---------------------------------------------------------------------------
# Open positions CRUD
# ---------------------------------------------------------------------------

def get_open_positions():
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM open_positions ORDER BY entry_time"
        ).fetchall()]


def open_position(node, signal_price, signal_time, entry_price, entry_time):
    with _conn() as c:
        existing = c.execute(
            "SELECT id FROM open_positions WHERE ticker=? AND window=?",
            (node['ticker'], int(node['window']))
        ).fetchone()
        if existing:
            print(f"  [warn] position already open for {node['ticker']} w={node['window']} — skipping duplicate")
            return
        c.execute("""
            INSERT INTO open_positions
                (ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
                 signal_price, signal_time, entry_price, entry_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            node['ticker'], node['strategy'], node['version'],
            int(node['window']), int(node['take_profit']), int(node['stop_loss']),
            int(node['max_hold_hours']),
            float(signal_price), signal_time.strftime('%Y-%m-%d %H:%M:%S'),
            float(entry_price), entry_time.strftime('%Y-%m-%d %H:%M:%S'),
        ))
        c.commit()


def close_position(position_id):
    with _conn() as c:
        c.execute("DELETE FROM open_positions WHERE id = ?", (position_id,))
        c.commit()


# ---------------------------------------------------------------------------
# Price data
# ---------------------------------------------------------------------------

def _load_cache(ticker):
    path = CACHE_DIR / f"{ticker}_1h.csv"
    if not path.exists():
        return None, None
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    df_daily = df.resample('D').last().dropna()
    return df, df_daily


def _current_price(ticker):
    df, _ = _load_cache(ticker)
    if df is None:
        return None, None
    prices = df['Close'].dropna()
    return float(prices.iloc[-1]), df.index[-1]


# ---------------------------------------------------------------------------
# Signal computation
# ---------------------------------------------------------------------------

def compute_buy_signal(node):
    ticker = node['ticker']
    window = int(node['window'])

    strategy_cls = getattr(strategies, node['strategy'], None)
    if strategy_cls is None:
        return None

    df_hourly, df_daily = _load_cache(ticker)
    if df_hourly is None or len(df_daily) < window:
        return None

    strat      = strategy_cls(window=window)
    indicators = strat.generate_daily_indicators(df_daily)
    if indicators.empty:
        return None

    last_row      = indicators.iloc[-1]
    close_series  = df_hourly['Close'].dropna()
    current_price = close_series.iloc[-1]
    last_bar      = close_series.index[-1]
    sma           = last_row['SMA']
    std           = last_row['Std']

    return {
        'ticker':        ticker,
        'window':        window,
        'current_price': current_price,
        'sma':           sma,
        'std':           std,
        'lower_band':    sma - 2.0 * std,
        'z_score':       (current_price - sma) / std,
        'signal':        strat.check_signal(current_price, last_row),
        'last_bar':      last_bar,
    }


def check_sell_condition(pos, current_price, now):
    strategy_cls = getattr(strategies, pos['strategy'], None)
    if strategy_cls is None:
        return None, None
    entry_time = datetime.strptime(pos['entry_time'], '%Y-%m-%d %H:%M:%S')
    hours_held = (now - entry_time).total_seconds() / 3600
    strat      = strategy_cls(window=pos['window'])
    return strat.check_exit(
        current_price, pos['entry_price'],
        pos['take_profit'], pos['stop_loss'],
        hours_held, pos['max_hold_hours']
    )


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def _slack(text, blocks=None):
    if not SLACK_HOOK:
        return
    payload = {'text': text}
    if blocks:
        payload['blocks'] = blocks
    try:
        r = requests.post(SLACK_HOOK, json=payload, timeout=5)
        if not r.ok:
            print(f"  [slack error] HTTP {r.status_code}")
    except Exception as e:
        print(f"  [slack error] {e}")


def _slack_blocks(header_text, fields, context=None):
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*{k}:*\n{v}"} for k, v in fields.items()
        ]},
    ]
    if context:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": context}]})
    return blocks


def notify_buy_signal(node, sig):
    ticker   = sig['ticker']
    price    = sig['current_price']
    z        = sig['z_score']
    bar_time = sig['last_bar']
    bar_str  = bar_time.strftime('%Y-%m-%d %H:%M')
    tp       = node['take_profit']
    sl       = node['stop_loss']
    hold     = node['max_hold_hours']

    sep = '=' * 62
    print(f"\n{sep}")
    print(f"  BUY SIGNAL  {ticker}  {bar_str}")
    print(f"  Price:  ${price:.4f}   Lower band: ${sig['lower_band']:.4f}   z = {z:.2f}")
    print(f"  Node:   window={node['window']}  TP={tp}%  SL={sl}%  hold={hold}h")
    print(f"  SMA: ${sig['sma']:.4f}   Std: ${sig['std']:.4f}")
    print(sep)

    _slack(
        f"BUY SIGNAL — {ticker}  ${price:.4f}  z={z:.2f}  ({bar_str})",
        _slack_blocks(
            f"\U0001f514 BUY SIGNAL — {ticker}",
            {"Price": f"${price:.4f}", "Lower Band": f"${sig['lower_band']:.4f}",
             "Z-Score": f"{z:.2f}", "Bar": bar_str,
             "Window": str(node['window']), "TP / SL": f"{tp}% / {sl}%",
             "Max Hold": f"{hold}h", "SMA": f"${sig['sma']:.4f}"},
            context="Reply with execution price when filled.",
        )
    )

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
            open_position(node, price, bar_time, exec_price, now)
            note = f"Entered at ${exec_price:.4f}  (drift: {drift_pct:+.2f}%)"
            print(f"  Position opened. {note}")
            _slack(f"{ticker} position opened: {note}")
        except ValueError:
            print("  Invalid price — position not opened.")
    else:
        print("  Skipped.")


def notify_sell_signal(pos, reason, current_price, target_price):
    ticker     = pos['ticker']
    ep         = pos['entry_price']
    entry_time = pos['entry_time']
    pct        = (current_price - ep) / ep * 100
    tp         = pos['take_profit']
    sl         = pos['stop_loss']
    hold       = pos['max_hold_hours']

    reason_labels = {'TP': 'TAKE PROFIT', 'SL': 'STOP LOSS', 'TIME': 'TIME EXIT'}

    sep = '=' * 62
    print(f"\n{sep}")
    print(f"  SELL SIGNAL  {ticker}  — {reason_labels[reason]}")
    print(f"  Entry: ${ep:.4f}  →  Current: ${current_price:.4f}  ({pct:+.2f}%)")
    print(f"  Target: ${target_price:.4f}   Node: TP={tp}%  SL={sl}%  hold={hold}h")
    print(f"  Entered: {entry_time}")
    print(sep)

    _slack(
        f"SELL SIGNAL — {ticker}  {reason_labels[reason]}  ${current_price:.4f}  ({pct:+.2f}%)",
        _slack_blocks(
            f"\U0001f6a8 SELL SIGNAL — {ticker}  ({reason_labels[reason]})",
            {"Entry Price": f"${ep:.4f}", "Current Price": f"${current_price:.4f}",
             "P&L": f"{pct:+.2f}%", "Target": f"${target_price:.4f}",
             "TP / SL": f"{tp}% / {sl}%", "Max Hold": f"{hold}h",
             "Entered": entry_time},
            context="Reply with exit price when filled.",
        )
    )

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
            close_position(pos['id'])
            print(f"  Position closed. {note}")
            _slack(f"{ticker} position closed: {note}")
        except ValueError:
            print("  Invalid price — position kept open.")
    else:
        print("  Skipped — position kept open.")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_loop():
    ensure_tables()
    print(f"Signal monitor started  |  poll={POLL_SECS}s  |  Ctrl+C to stop")
    if not SLACK_HOOK:
        print("  [info] SLACK_WEBHOOK_URL not set — console only")

    # One BUY alert per (ticker, window) per calendar day
    buy_alerted: set[tuple] = set()
    # One SELL alert per open position ID per run (until position closed or restarted)
    sell_alerted: set[int] = set()
    last_date = datetime.now().strftime('%Y-%m-%d')

    while True:
        now      = datetime.now()
        today    = now.strftime('%Y-%m-%d')

        if today != last_date:
            buy_alerted.clear()
            last_date = today

        # --- Check open positions for SELL conditions first ---
        for pos in get_open_positions():
            if pos['id'] in sell_alerted:
                continue
            cp, _ = _current_price(pos['ticker'])
            if cp is None:
                continue
            reason, target = check_sell_condition(pos, cp, now)
            if reason:
                notify_sell_signal(pos, reason, cp, target)
                sell_alerted.add(pos['id'])

        # --- Check watch list for BUY conditions ---
        watchlist = get_watchlist()
        if not watchlist:
            print(f"[{now.strftime('%H:%M:%S')}] Watch list empty — add nodes with: python active_signals.py add")
            time.sleep(POLL_SECS)
            continue

        for node in watchlist:
            sig = compute_buy_signal(node)
            if sig is None:
                print(f"[{now.strftime('%H:%M:%S')}] {node['ticker']} w={node['window']}: no data")
                continue

            alert_key = (sig['ticker'], node['strategy'], sig['window'])

            if sig['signal'] == 'BUY' and alert_key not in buy_alerted:
                buy_alerted.add(alert_key)
                notify_buy_signal(node, sig)
            else:
                print(
                    f"[{now.strftime('%H:%M:%S')}] {sig['ticker']:<6} w={sig['window']:<3} "
                    f"{sig['signal']:<4}  ${sig['current_price']:.4f}  "
                    f"z={sig['z_score']:+.2f}  bar={sig['last_bar'].strftime('%m-%d %H:%M')}"
                )

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
            f"{n['id']:<4} {n['ticker']:<7} {n['window']:<4} {n['take_profit']:<4} "
            f"{n['stop_loss']:<4} {n['max_hold_hours']:<6} {(n.get('label') or ''):<20} {n['added_at']}"
        )


def cmd_positions():
    ensure_tables()
    positions = get_open_positions()
    if not positions:
        print("No open positions.")
        return
    now = datetime.now()
    hdr = f"{'ID':<4} {'Ticker':<7} {'Entry Price':<13} {'Entry Time':<22} {'Hours':<7} {'TP%':<5} {'SL%':<5} {'Hold'}"
    print(hdr)
    print('-' * len(hdr))
    for p in positions:
        et    = datetime.strptime(p['entry_time'], '%Y-%m-%d %H:%M:%S')
        hours = (now - et).total_seconds() / 3600
        print(
            f"{p['id']:<4} {p['ticker']:<7} ${p['entry_price']:<12.4f} "
            f"{p['entry_time']:<22} {hours:<7.1f} {p['take_profit']:<5} "
            f"{p['stop_loss']:<5} {p['max_hold_hours']}"
        )


def cmd_add():
    ensure_tables()
    print("Add node to watch list (values from backtest_cache):")
    ticker         = input("  ticker: ").strip().upper()
    strategy       = input("  strategy [ZScore_Original]: ").strip() or "ZScore_Original"
    version        = input("  version [v1.4]: ").strip() or "v1.4"
    window         = int(input("  window: ").strip())
    take_profit    = int(input("  take_profit: ").strip())
    stop_loss      = int(input("  stop_loss: ").strip())
    max_hold_hours = int(input("  max_hold_hours: ").strip())
    label          = input("  label (optional): ").strip()
    add_node(ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours, label)
    print(f"Added {ticker} (w={window} TP={take_profit} SL={stop_loss} hold={max_hold_hours}h) label='{label}'.")


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
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'run'
    _CMDS.get(cmd, run_loop)()
