#!/usr/bin/env python3
"""
Manual-step live-trading simulator.

Drives the *real* active_signals.py functions (compute_buy_signal,
check_sell_condition, notify_buy_signal, notify_sell_signal, open_position,
close_position, ...) against an isolated sim DB (cache/trading_sim.db by
default), so the full BUY -> trailing-buy -> arm -> trailing-sell Slack
message sequence can be exercised bar-by-bar without touching the live
daemon or trading_live.db.

Real Slack messages fire to the same channel, prefixed "SIM" (SIM_MODE
forces the typed-input fallback instead of interactive buttons -- see
active_signals.py's INTERACTIVE flag docstring for why: the sim never
starts its own Socket Mode connection, so a real button click would be
delivered to the live daemon's connection instead of this process).

Usage:
    python scripts/live_sim.py [--sim-db PATH] [--source-watchlist-id N]

REPL commands:
    load TICKER [N]        (re)load TICKER's real cached CSV as the working
                            bar series, optionally truncated to the first N bars
    bar TICKER CLOSE [LOW HIGH] [TIME]
                            append one synthetic bar past the working series
    tail TICKER [N]        show the last N working bars (default 5)
    buy TICKER [PRICE]     run the real buy-signal check; PRICE overrides the
                            last working bar's close. Fires notify_buy_signal
                            for real if a BUY fires -- follow the "Did you
                            execute?" prompt to open a sim position.
    sell TICKER [PRICE] [--poll]
                            run the real sell-condition check for TICKER's
                            open sim position. Default is a bar-close check
                            (uses the working bar's Low/High); --poll does a
                            mid-bar check (SL/trailing only, current price only).
    winalert LABEL         fire the real signal-window ping (e.g. "10:25")
    state                  show sim watch_list + open positions + trail_state
    reset                  wipe the sim DB and reseed from the source watchlist
    quit / exit
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime

parser = argparse.ArgumentParser()
parser.add_argument("--sim-db", default="./cache/trading_sim.db")
parser.add_argument("--source-watchlist-id", type=int, default=9)
args, _ = parser.parse_known_args()

os.environ["TRADING_DB_PATH"] = args.sim_db
os.environ["SIM_MODE"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cmd  # noqa: E402
import pandas as pd  # noqa: E402

import active_signals as A  # noqa: E402  (must import after env vars are set)

LIVE_DB_PATH = "./cache/trading_live.db"

NODE_COLS = [
    'ticker', 'strategy', 'version', 'window', 'take_profit', 'stop_loss',
    'max_hold_hours', 'z_score_threshold', 'mode', 'trail_sell_pct', 'fixed_sl',
    'trail_buy_pct', 'arm_sell_pct', 'account', 'alpha', 'label',
]


def seed_from_live(watchlist_id):
    with sqlite3.connect(LIVE_DB_PATH) as lc:
        lc.row_factory = sqlite3.Row
        rows = [dict(r) for r in lc.execute(
            "SELECT * FROM watch_list WHERE watchlist_id = ?", (watchlist_id,)
        ).fetchall()]
    sim_wl_id = A.get_active_watchlist_id()
    with A._conn() as c:
        c.execute("DELETE FROM watch_list WHERE watchlist_id = ?", (sim_wl_id,))
        for r in rows:
            cols = [col for col in NODE_COLS if col in r]
            placeholders = ",".join("?" for _ in cols)
            c.execute(
                f"INSERT INTO watch_list (watchlist_id, {','.join(cols)}) VALUES (?, {placeholders})",
                (sim_wl_id, *[r[col] for col in cols]),
            )
        c.commit()
    return len(rows)


def _daily(df):
    return df.resample('D').last().dropna()


def _next_bar_time(ts):
    ts = pd.Timestamp(ts)
    nxt = ts + pd.Timedelta(hours=1)
    if nxt.hour >= 16:
        d = ts.normalize() + pd.Timedelta(days=1)
        while d.weekday() >= 5:
            d += pd.Timedelta(days=1)
        nxt = d + pd.Timedelta(hours=9, minutes=30)
    return nxt


class SimShell(cmd.Cmd):
    intro = "Live-sim REPL. 'help' for commands, 'quit' to exit.\n"
    prompt = "sim> "

    def __init__(self):
        super().__init__()
        self.dfs = {}  # ticker -> working df_hourly (real cache + appended bars)

    # -- bar-series management -------------------------------------------------

    def _working_df(self, ticker):
        if ticker not in self.dfs:
            self.do_load(ticker)
        return self.dfs.get(ticker)

    def do_load(self, arg):
        "load TICKER [N] -- (re)load TICKER's real cached CSV as the working series"
        parts = arg.split()
        if not parts:
            print("usage: load TICKER [N]")
            return
        ticker = parts[0].upper()
        df, _ = A._load_cache(ticker)
        if df is None:
            print(f"  no cached data for {ticker}")
            return
        if len(parts) > 1:
            df = df.iloc[:int(parts[1])]
        self.dfs[ticker] = df.copy()
        print(f"  {ticker}: loaded {len(df)} bars, last = {df.index[-1]}  close=${df['Close'].iloc[-1]:.4f}")

    def do_bar(self, arg):
        "bar TICKER CLOSE [LOW HIGH] [TIME] -- append a synthetic bar"
        parts = arg.split()
        if len(parts) < 2:
            print("usage: bar TICKER CLOSE [LOW HIGH] [TIME]")
            return
        ticker = parts[0].upper()
        df = self._working_df(ticker)
        if df is None:
            return
        close = float(parts[1])
        low = float(parts[2]) if len(parts) > 2 else close
        high = float(parts[3]) if len(parts) > 3 else close
        if len(parts) > 5:
            ts = pd.Timestamp(f"{parts[4]} {parts[5]}")
        elif len(parts) > 4:
            ts = pd.Timestamp(parts[4])
        else:
            ts = _next_bar_time(df.index[-1])
        row = pd.DataFrame(
            [{'Open': close, 'High': high, 'Low': low, 'Close': close, 'Volume': 0}],
            index=[ts],
        )
        self.dfs[ticker] = pd.concat([df, row]).sort_index()
        print(f"  {ticker}: appended bar {ts}  O/H/L/C = {close}/{high}/{low}/{close}")

    def do_tail(self, arg):
        "tail TICKER [N] -- show the last N working bars (default 5)"
        parts = arg.split()
        if not parts:
            print("usage: tail TICKER [N]")
            return
        ticker = parts[0].upper()
        df = self._working_df(ticker)
        if df is None:
            return
        n = int(parts[1]) if len(parts) > 1 else 5
        print(df.tail(n)[['Open', 'High', 'Low', 'Close']])

    # -- signal checks -----------------------------------------------------

    def _node_for(self, ticker):
        nodes = [n for n in A.get_watchlist() if n['ticker'] == ticker.upper()]
        if not nodes:
            print(f"  no watch_list node for {ticker} -- is it seeded? try 'reset'")
            return None
        if len(nodes) > 1:
            print(f"  {len(nodes)} nodes for {ticker}, using the first (window={nodes[0]['window']})")
        return nodes[0]

    def do_buy(self, arg):
        "buy TICKER [PRICE] -- run the real buy-signal check, fires notify_buy_signal on BUY"
        parts = arg.split()
        if not parts:
            print("usage: buy TICKER [PRICE]")
            return
        ticker = parts[0].upper()
        node = self._node_for(ticker)
        if node is None:
            return
        df = self._working_df(ticker)
        if df is None:
            return
        price = float(parts[1]) if len(parts) > 1 else float(df['Close'].iloc[-1])
        sig = A.compute_buy_signal(
            node, as_of=df.index[-1], price_override=price,
            df_hourly_override=df, df_daily_override=_daily(df),
        )
        if sig is None:
            print(f"  {ticker}: not enough data for a signal")
            return
        print(f"  {ticker}  price=${sig['current_price']:.4f}  lower_band=${sig['lower_band']:.4f}"
              f"  z={sig['z_score']:+.2f}  signal={sig['signal']}")
        if sig['signal'] != 'BUY':
            return
        if ticker in A.get_held_tickers():
            print(f"  [skip] {ticker} already held -- no alert (matches run_loop's skip logic)")
            return
        if node.get('mode', 'live') != 'live':
            print(f"  [research] {ticker} would BUY but mode={node.get('mode')} -- no alert")
            return
        A.notify_buy_signal(node, sig)

    def do_sell(self, arg):
        "sell TICKER [PRICE] [--poll] -- run the real sell-condition check for TICKER's open position"
        parts = arg.split()
        if not parts:
            print("usage: sell TICKER [PRICE] [--poll]")
            return
        ticker = parts[0].upper()
        poll = '--poll' in parts
        parts = [p for p in parts if p != '--poll']
        pos = next((p for p in A.get_open_positions() if p['ticker'] == ticker), None)
        if pos is None:
            print(f"  no open sim position for {ticker}")
            return
        df = self._working_df(ticker)
        if df is None:
            return
        at_bar_close = not poll
        if at_bar_close:
            bar = df.iloc[-1]
            cp, low, high = float(bar['Close']), float(bar['Low']), float(bar['High'])
            if len(parts) > 1:
                cp = float(parts[1])
        else:
            cp = float(parts[1]) if len(parts) > 1 else float(df['Close'].iloc[-1])
            low = high = cp
        reason, target, just_activated_trailing = A.check_sell_condition(
            pos, cp, datetime.now(), at_bar_close=at_bar_close, low=low, high=high, df_hourly=df,
        )
        print(f"  {ticker}  price=${cp:.4f}  at_bar_close={at_bar_close}  reason={reason}  target={target}")
        if just_activated_trailing:
            A.notify_trailing_activated(pos, cp)
        if reason:
            A.notify_sell_signal(pos, reason, cp, target)

    def do_winalert(self, arg):
        "winalert LABEL -- fire the real signal-window ping, e.g. 'winalert 10:25'"
        label = arg.strip()
        if not label:
            print("usage: winalert LABEL   (e.g. 10:25)")
            return
        A._send_window_alert(label, A.get_watchlist())

    # -- state ---------------------------------------------------------------

    def do_state(self, arg):
        "state -- show sim watch_list + open positions + trail_state"
        print("watch_list:")
        for n in A.get_watchlist():
            print(f"  {n['ticker']:6s} {n['strategy']:26s} {n['version']:6s} mode={n.get('mode')}")
        print("open_positions:")
        for p in A.get_open_positions():
            print(f"  {p['ticker']:6s} entry=${p['entry_price']:.4f} @ {p['entry_time']}"
                  f"  trail_state={p['trail_state']}")

    def do_reset(self, arg):
        "reset -- wipe the sim DB and reseed from the source watchlist"
        try:
            os_remove = __import__("os").remove
            os_remove(args.sim_db)
        except FileNotFoundError:
            pass
        A.ensure_tables()
        n = seed_from_live(args.source_watchlist_id)
        self.dfs = {}
        print(f"  sim DB reset, seeded {n} nodes from live watchlist {args.source_watchlist_id}")

    def do_quit(self, arg):
        "quit -- exit"
        return True

    do_exit = do_quit
    do_EOF = do_quit


def main():
    A.ensure_tables()
    if not A.get_watchlist():
        n = seed_from_live(args.source_watchlist_id)
        print(f"Seeded {n} nodes from live watchlist {args.source_watchlist_id} into {args.sim_db}")
    else:
        print(f"Using existing sim DB {args.sim_db} ({len(A.get_watchlist())} nodes) -- 'reset' to reseed")
    SimShell().cmdloop()


if __name__ == "__main__":
    main()
