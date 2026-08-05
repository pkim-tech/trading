"""
Research script: does "chop" cluster? Raised 2026-08-04 evening as a follow-on to the (negative,
first-pass) z-entry-velocity audit -- instead of asking whether one trade's breach shape predicts
its own outcome, this asks whether a ticker's choppy state *persists*: after two quick round-trip
entries/exits close together in time, is a third one more likely (and does it tend to win), or is
each trade independent?

Signal frequency problem: the live daemon only checks two fixed daily windows (9:30/14:30 bar
close), which starves this question of examples -- most real v5 trades are days/weeks apart, not
tightly clustered (see the AGQ trade table printed in conversation 2026-08-04). This script
deliberately decouples from that live constraint for research purposes only: every hourly bar can
fire an entry signal, not just the two live windows, giving many more chop opportunities to look
at. This is NOT a proposal to run live this way (manual/bridge execution can't act on every bar)
and no return/alpha claim from this script should be read as a live-viable number -- it's purely
to characterize how choppy each ticker's own price action naturally is at the actual bar
resolution available.

simulate_trail_exit_every_bar / simulate_trail_both_every_bar below are the exit-mechanics-only
copies of scripts/export_trades.py's simulate_trail_exit_chaos (zero miss rate) / annotated
mirrors, with the `if h != target_h0 and h != target_h1: continue` signal-window gate removed --
every other line of trade logic (SL, TP/trailing-arm, TIME, gap-through-trigger fills) is
unchanged from the trusted mirrors so this only isolates the effect of the gate.

For each real v5 ticker: builds the every-bar trade list, groups consecutive trades into
"clusters" wherever the gap between one trade's exit and the next trade's entry signal is <=
--cluster-gap-bars (default 8, ~1 trading day at hourly resolution), then reports win rate / mean
return by position-within-cluster (1st, 2nd, 3rd, 4th+) -- the direct test of "does the 3rd chop
tend to also win, given the first two did."

Usage: .venv/bin/python scripts/chop_cluster_scan.py [--tickers ...] [--watchlist-id 65]
       [--cluster-gap-bars 8] [--csv]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd
from scipy.stats import spearmanr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtester import prep_inputs, WIN, LOSS, TWIN, TLOSS, OPEN
import strategies
from scripts.export_trades import load_hourly

LIVE_DB = Path("cache/live/trading_live.db")
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def simulate_trail_exit_every_bar(p, take_profit, stop_loss, max_hours_to_hold,
                                   trail_pct, z_thresh, open_check=False):
    """Signal-window-degated copy of export_trades.simulate_trail_exit_chaos (miss_rate=0,
    so behaviorally identical to simulate_trail_exit_chaos(..., rng, 'drop', 0.0, 'drop', 0.0,
    open_check=open_check)) except every hourly bar can fire a signal, not just target_h0/h1.
    See module docstring -- research-only, not a live proposal."""
    prices, highs, lows, opens = p['prices'], p['highs'], p['lows'], p['opens']
    daily_idx, sma_arr, std_arr = p['daily_idx'], p['sma_arr'], p['std_arr']
    trend_arr, has_trend = p['trend_arr'], p['has_trend']

    trades = []
    in_trade = trailing = False
    entry_price = stop_price = tp_price = peak = 0.0
    entry_bar = held = 0

    n = len(prices)
    for i in range(n):
        cp, high, low, op = prices[i], highs[i], lows[i], opens[i]

        if in_trade:
            held += 1
            if trailing:
                trail_stop_gap = peak * (1.0 - trail_pct)
                if op <= trail_stop_gap:
                    pc = (op - entry_price) / entry_price
                    trades.append(dict(signal_i=entry_bar, entry_i=entry_bar, exit_i=i,
                                        held=held, result=WIN if pc > 0 else LOSS, ret=pc))
                    in_trade = trailing = False
                    continue
                if high > peak:
                    peak = high
                trail_stop = peak * (1.0 - trail_pct)
                sl_hit = low <= trail_stop
                time_hit = held >= max_hours_to_hold
                if sl_hit or time_hit:
                    exit_px = trail_stop if sl_hit else cp
                    pc = (exit_px - entry_price) / entry_price
                    trades.append(dict(signal_i=entry_bar, entry_i=entry_bar, exit_i=i,
                                        held=held, result=WIN if pc > 0 else LOSS, ret=pc))
                    in_trade = trailing = False
                continue
            sl_gap_hit = op <= stop_price
            sl_hit = low <= stop_price
            if sl_gap_hit or sl_hit:
                exit_px = op if sl_gap_hit else stop_price
                pc = (exit_px - entry_price) / entry_price
                trades.append(dict(signal_i=entry_bar, entry_i=entry_bar, exit_i=i,
                                    held=held, result=LOSS, ret=pc))
                in_trade = False
                continue
            if cp >= tp_price:
                trailing = True; peak = cp
                continue
            if held >= max_hours_to_hold:
                pc = (cp - entry_price) / entry_price
                trades.append(dict(signal_i=entry_bar, entry_i=entry_bar, exit_i=i,
                                    held=held, result=TWIN if pc > 0 else TLOSS, ret=pc))
                in_trade = False
                continue
            continue

        di = daily_idx[i]
        if di < 0:
            continue
        sma, std = sma_arr[di], std_arr[di]
        if std == 0.0:
            continue
        lower_band = sma - std * z_thresh
        fired_signal = False
        if open_check:
            signal_open = (op <= lower_band) and (op > trend_arr[di]) if has_trend else op <= lower_band
            if signal_open:
                fired_signal = True; fired_price = op
        if not fired_signal:
            signal = (cp <= lower_band) and (cp > trend_arr[di]) if has_trend else cp <= lower_band
            if signal:
                fired_signal = True; fired_price = cp
        if fired_signal:
            entry_price = fired_price
            tp_price = entry_price * (1.0 + take_profit)
            stop_price = entry_price * (1.0 - stop_loss)
            entry_bar = i; held = 0
            in_trade = True; trailing = False

    if in_trade:
        cp = prices[n - 1]
        pc = (cp - entry_price) / entry_price
        trades.append(dict(signal_i=entry_bar, entry_i=entry_bar, exit_i=n - 1,
                            held=held, result=OPEN, ret=pc))
    return trades


def simulate_trail_both_every_bar(p, take_profit, stop_loss, max_hours_to_hold,
                                   trail_buy_pct, trail_pct, z_thresh, open_check=False):
    """Signal-window-degated copy of export_trades.simulate_trail_both_annotated -- every
    hourly bar can fire a signal, not just target_h0/h1. See module docstring."""
    prices, highs, lows, opens = p['prices'], p['highs'], p['lows'], p['opens']
    daily_idx, sma_arr, std_arr = p['daily_idx'], p['sma_arr'], p['std_arr']
    trend_arr, has_trend = p['trend_arr'], p['has_trend']

    trades = []
    in_trade = waiting = trailing = False
    entry_price = stop_price = tp_price = peak = 0.0
    entry_bar = held = 0
    running_low = 0.0
    wait_bars = 0
    signal_bar = None

    n = len(prices)
    for i in range(n):
        cp, high, low, op = prices[i], highs[i], lows[i], opens[i]

        if in_trade:
            held += 1
            if trailing:
                trail_stop_gap = peak * (1.0 - trail_pct)
                if op <= trail_stop_gap:
                    pc = (op - entry_price) / entry_price
                    trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, exit_i=i,
                                        held=held, result=WIN if pc > 0 else LOSS, ret=pc))
                    in_trade = trailing = False
                    continue
                if high > peak:
                    peak = high
                trail_stop = peak * (1.0 - trail_pct)
                if low <= trail_stop or held >= max_hours_to_hold:
                    exit_px = trail_stop if low <= trail_stop else cp
                    pc = (exit_px - entry_price) / entry_price
                    trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, exit_i=i,
                                        held=held, result=WIN if pc > 0 else LOSS, ret=pc))
                    in_trade = trailing = False
                continue
            if op <= stop_price:
                pc = (op - entry_price) / entry_price
                trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, exit_i=i,
                                    held=held, result=LOSS, ret=pc))
                in_trade = False
                continue
            if low <= stop_price:
                pc = (stop_price - entry_price) / entry_price
                trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, exit_i=i,
                                    held=held, result=LOSS, ret=pc))
                in_trade = False
                continue
            if cp >= tp_price:
                trailing = True; peak = cp
                continue
            if held >= max_hours_to_hold:
                pc = (cp - entry_price) / entry_price
                trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, exit_i=i,
                                    held=held, result=TWIN if pc > 0 else TLOSS, ret=pc))
                in_trade = False
            continue

        if waiting:
            wait_bars += 1
            buy_trigger_gap = running_low * (1.0 + trail_buy_pct)
            if op >= buy_trigger_gap:
                entry_price = op
                tp_price = entry_price * (1.0 + take_profit)
                stop_price = entry_price * (1.0 - stop_loss)
                entry_bar = i; held = 0
                in_trade = True; waiting = trailing = False
                continue
            if low < running_low:
                running_low = low
            buy_trigger = running_low * (1.0 + trail_buy_pct)
            if high >= buy_trigger:
                entry_price = buy_trigger
                tp_price = entry_price * (1.0 + take_profit)
                stop_price = entry_price * (1.0 - stop_loss)
                entry_bar = i; held = 0
                in_trade = True; waiting = trailing = False
                continue
            if wait_bars >= max_hours_to_hold:
                waiting = False
            continue

        di = daily_idx[i]
        if di < 0:
            continue
        sma, std = sma_arr[di], std_arr[di]
        if std == 0.0:
            continue
        lower_band = sma - std * z_thresh
        fired = False
        if open_check:
            signal_open = (op <= lower_band) and (op > trend_arr[di]) if has_trend else op <= lower_band
            if signal_open:
                waiting = True; running_low = op; wait_bars = 0; signal_bar = i
                fired = True
        if not fired:
            signal = (cp <= lower_band) and (cp > trend_arr[di]) if has_trend else cp <= lower_band
            if signal:
                waiting = True; running_low = cp; wait_bars = 0; signal_bar = i

    if in_trade:
        cp = prices[n - 1]
        pc = (cp - entry_price) / entry_price
        trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, exit_i=n - 1,
                            held=held, result=OPEN, ret=pc))
    return trades


def load_nodes(watchlist_id, tickers=None):
    con = sqlite3.connect(LIVE_DB)
    q = """
        SELECT MIN(id), ticker, strategy, window, z_score_threshold, arm_sell_pct, take_profit,
               fixed_sl, trail_buy_pct, trail_sell_pct, max_hold_hours, entry_timing
        FROM watch_list WHERE watchlist_id=? AND mode='research'
    """
    params = [watchlist_id]
    if tickers:
        q += f" AND ticker IN ({','.join('?' * len(tickers))})"
        params += tickers
    q += " GROUP BY ticker"
    rows = con.execute(q, params).fetchall()
    con.close()
    cols = ["id", "ticker", "strategy", "window", "z", "arm_sell_pct", "take_profit", "fixed_sl",
            "trail_buy_pct", "trail_sell_pct", "max_hold_hours", "entry_timing"]
    nodes = [dict(zip(cols, r)) for r in rows]
    # take_profit holds the arm-sell threshold for TrailingExitZScoreBreakout nodes;
    # arm_sell_pct holds it for TrailingBothZScoreBreakout (never both populated on
    # the same row -- signals_db.py:983-988). See docs/research_log.md's 2026-08-04
    # correction entry -- the earlier bug hardcoded TP=disabled for every TrailingExit
    # node instead of reading its real value.
    for n in nodes:
        n["arm_pct"] = n["arm_sell_pct"] if n["strategy"] == "TrailingBothZScoreBreakout" else n["take_profit"]
    return nodes


def build_indicators(strategy_name, df_daily, window):
    strat_cls = getattr(strategies, strategy_name)
    strat = strat_cls(window=window)
    return strat.generate_daily_indicators(df_daily)


def get_every_bar_trades(node):
    df_h = load_hourly(node["ticker"])
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    ind = build_indicators(node["strategy"], df_daily, node["window"])
    p = prep_inputs(df_h, ind)

    open_check = node["entry_timing"] == "open_check"
    if node["strategy"] == "TrailingBothZScoreBreakout":
        trades = simulate_trail_both_every_bar(
            p, node["arm_pct"] / 100.0, node["fixed_sl"] / 100.0, node["max_hold_hours"],
            node["trail_buy_pct"] / 100.0, node["trail_sell_pct"] / 100.0, node["z"], open_check=open_check,
        )
    elif node["strategy"] == "TrailingExitZScoreBreakout":
        trades = simulate_trail_exit_every_bar(
            p, node["arm_pct"] / 100.0, node["fixed_sl"] / 100.0, node["max_hold_hours"],
            node["trail_sell_pct"] / 100.0, node["z"], open_check=open_check,
        )
    else:
        raise ValueError(f"unhandled strategy {node['strategy']}")
    return trades


def cluster_trades(trades, cluster_gap_bars):
    """Groups consecutive trades (sorted by signal_i) into clusters wherever the gap between
    one trade's exit_i and the next trade's signal_i is <= cluster_gap_bars."""
    trades = sorted([t for t in trades if t["result"] != OPEN], key=lambda t: t["signal_i"])
    clusters = []
    current = []
    prev_exit = None
    for t in trades:
        if prev_exit is not None and (t["signal_i"] - prev_exit) > cluster_gap_bars:
            clusters.append(current)
            current = []
        current.append(t)
        prev_exit = t["exit_i"]
    if current:
        clusters.append(current)
    return clusters


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", default=None)
    parser.add_argument("--watchlist-id", type=int, default=65)
    parser.add_argument("--cluster-gap-bars", type=int, default=8)
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()

    nodes = load_nodes(args.watchlist_id, args.tickers)
    position_rows = []      # every trade tagged with its cluster + position
    cluster_len_rows = []    # one row per cluster

    for node in nodes:
        try:
            trades = get_every_bar_trades(node)
        except Exception as e:
            print(f"{node['ticker']}: failed ({e})")
            continue

        clusters = cluster_trades(trades, args.cluster_gap_bars)
        for c in clusters:
            cluster_len_rows.append({"ticker": node["ticker"], "cluster_len": len(c)})
            for pos, t in enumerate(c, start=1):
                position_rows.append({
                    "ticker": node["ticker"], "cluster_len": len(c), "position": pos,
                    "win": t["ret"] > 0, "ret": t["ret"],
                })
        print(f"{node['ticker']}: {len(trades)} every-bar trades -> {len(clusters)} clusters "
              f"(gap<={args.cluster_gap_bars} bars)")

    df = pd.DataFrame(position_rows)
    cl_df = pd.DataFrame(cluster_len_rows)
    if df.empty:
        print("No trades found.")
        return

    if args.csv:
        OUTPUT_DIR.mkdir(exist_ok=True)
        df.to_csv(OUTPUT_DIR / "chop_cluster_positions.csv", index=False)
        print(f"\nWrote {OUTPUT_DIR / 'chop_cluster_positions.csv'}")

    print(f"\n--- Cluster length distribution (pooled, n={len(cl_df)} clusters) ---")
    print(cl_df["cluster_len"].value_counts().sort_index().to_string())

    print(f"\n--- Win rate / mean return by position-in-cluster (pooled across tickers) ---")
    # cap displayed position bucket at 4+ so thin tails don't fragment the table
    df["position_bucket"] = df["position"].clip(upper=4).astype(str)
    df.loc[df["position"] >= 4, "position_bucket"] = "4+"
    summary = df.groupby("position_bucket").agg(
        n=("win", "size"), win_rate=("win", "mean"), mean_ret=("ret", "mean"),
    ).reindex(["1", "2", "3", "4+"]).dropna(how="all")
    print(summary.to_string())

    # Direct test: within clusters of length >= 3, does position correlate with outcome?
    deep = df[df["cluster_len"] >= 3]
    if len(deep) >= 5:
        rho, p_val = spearmanr(deep["position"], deep["ret"])
        print(f"\nWithin clusters of length>=3 (n={len(deep)}): position vs ret Spearman "
              f"rho={rho:.3f} p={p_val:.3f}")
    else:
        print(f"\nToo few trades in clusters of length>=3 (n={len(deep)}) to correlate")

    print(f"\n--- Per-ticker cluster summary ---")
    trade_stats = df.groupby("ticker").agg(trades=("win", "size"), overall_win_rate=("win", "mean"))
    cluster_stats = cl_df.groupby("ticker").agg(
        clusters=("cluster_len", "size"), mean_cluster_len=("cluster_len", "mean"),
        max_cluster_len=("cluster_len", "max"),
    )
    per_ticker = trade_stats.join(cluster_stats)
    print(per_ticker.to_string())


if __name__ == "__main__":
    main()
