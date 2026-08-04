"""Bear-market stress test for the z-score mean-reversion strategy, run on synthetic
daily-bar leveraged-ETF prices reconstructed from long-history 1x underlyings.

Why synthetic: yfinance's hourly-bar retention is ~2 years (our real cache only goes
back to 2023-07/2025-06), and most of these leveraged ETFs didn't exist through 2008
anyway. Since a daily-reset leveraged ETF is *defined* as leverage x the underlying's
daily return, we can reconstruct a plausible daily price series from decades of real
underlying index/ETF data and run a daily-bar approximation of the real strategy logic
against it. This is NOT the live hourly kernel (backtester.py::_simulate_trail_both) --
it's a coarser, self-contained mirror (single signal check per day, no fill-optimism
resolutions, no expense-ratio/financing drag) built to answer "does this class of
strategy survive a real crash," not "exactly how the live version would have performed."

Usage: .venv/bin/python scripts/sim_bear_market_stress.py [--tickers T...]
"""
import argparse
import sqlite3
import sys

import numpy as np
import pandas as pd
import yfinance as yf

LIVE_DB = "cache/live/trading_live.db"

# ticker -> (underlying proxy, leverage, note)
PROXIES = {
    "SOXL":  ("SOXX", 3,  None),
    "SOXS":  ("SOXX", -3, None),
    "USD":   ("SOXX", 2,  None),
    "GDXU":  ("GDX",  3,  None),
    "NUGT":  ("GDX",  2,  None),
    "AGQ":   ("SLV",  2,  None),
    "YANG":  ("FXI",  -3, None),
    "UDOW":  ("DIA",  3,  None),
    "KORU":  ("EWY",  3,  None),
    "DPST":  ("KRE",  3,  None),
    "HIBL":  ("QQQ",  4,  "no direct high-beta-index ETF with long history; QQQ used "
                          "as a higher-beta SPY proxy, leverage bumped 3->4 to "
                          "approximate high-beta amplification -- rough, flagged"),
    "SH":    ("SPY",  -1, None),
    "SPY":   ("SPY",  1,  None),
    "QQQ":   ("QQQ",  1,  None),
}

CRASHES = {
    # (crash_start, crash_bottom -- start/end of the decline leg, recovery_end -- real
    # date SPY reclaimed its prior all-time high)
    "2008_gfc":    ("2007-10-01", "2009-03-09", "2013-03-31"),
    "2020_covid":  ("2020-01-01", "2020-03-23", "2020-08-31"),
    "2022_bear":   ("2022-01-01", "2022-10-12", "2024-01-31"),
    # S&P 500 peak -> bottom -> reclaimed its 2000 nominal high. NOTE: several proxies
    # (SOXX launched 2001-07, SLV launched 2006-04) postdate this crash's decline leg --
    # results for tickers using those proxies will be truncated/missing, flagged at runtime.
    "2000_dotcom": ("2000-03-24", "2002-10-09", "2007-05-30"),
}

BARS_PER_DAY = 7  # 9:30..15:30 hourly bars -> convert max_hold_hours to trading days


def fetch_underlying(proxy):
    df = yf.download(proxy, start="2000-01-01", progress=False, auto_adjust=True)
    if df.empty:
        raise RuntimeError(f"no data for {proxy}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df[["Open", "High", "Low", "Close"]].dropna()


def synthesize_leveraged(underlying, leverage):
    u = underlying
    prev_close = u["Close"].shift(1)
    open_ret = u["Open"] / prev_close - 1.0
    high_ret = u["High"] / prev_close - 1.0
    low_ret = u["Low"] / prev_close - 1.0
    close_ret = u["Close"] / prev_close - 1.0

    if leverage < 0:
        # inverse product's High tracks the underlying's Low move, and vice versa
        high_ret, low_ret = low_ret, high_ret

    lev_close_ret = leverage * close_ret
    lev_open_ret = leverage * open_ret
    lev_high_ret = leverage * high_ret
    lev_low_ret = leverage * low_ret

    close = 100.0 * (1.0 + lev_close_ret).cumprod()
    close.iloc[0] = 100.0
    prev_lev_close = close.shift(1).fillna(100.0)

    out = pd.DataFrame({
        "Open": prev_lev_close * (1.0 + lev_open_ret),
        "High": prev_lev_close * (1.0 + np.maximum(lev_high_ret, lev_low_ret)),
        "Low": prev_lev_close * (1.0 + np.minimum(lev_high_ret, lev_low_ret)),
        "Close": close,
    })
    return out.dropna()


def get_node_params(ticker):
    """Real live/research node params for `ticker` from watch_list, preferring a
    real-money live node, else the research-mode v5 node."""
    conn = sqlite3.connect(LIVE_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """SELECT strategy, window, z_score_threshold, take_profit, fixed_sl,
                  trail_buy_pct, trail_sell_pct, arm_sell_pct, max_hold_hours
           FROM watch_list WHERE ticker=? AND watchlist_id=65
           ORDER BY (mode='live') DESC, id DESC LIMIT 1""",
        (ticker,),
    )
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def run_strategy_daily(bars, params):
    """Daily-bar approximation of TrailingBothZScoreBreakout / TrailingExitZScoreBreakout.
    One signal check per day at Close; entries/exits resolved same-day where triggered
    intraday via High/Low. Returns a list of trade dicts."""
    close = bars["Close"].to_numpy()
    high = bars["High"].to_numpy()
    low = bars["Low"].to_numpy()
    open_ = bars["Open"].to_numpy()
    n = len(bars)

    window = int(params["window"])
    z_thresh = float(params["z_score_threshold"])
    fixed_sl = float(params["fixed_sl"]) / 100.0
    trail_buy_pct = float(params["trail_buy_pct"] or 0.0) / 100.0
    trail_sell_pct = float(params["trail_sell_pct"] or 0.0) / 100.0
    arm_pct = params["arm_sell_pct"]
    take_profit = params["take_profit"]
    arm_trigger = (float(arm_pct) / 100.0 if arm_pct is not None
                   else (float(take_profit) / 100.0 if take_profit else None))
    max_hold_days = max(1, round(float(params["max_hold_hours"]) / BARS_PER_DAY))

    sma = pd.Series(close).rolling(window).mean().to_numpy()
    std = pd.Series(close).rolling(window).std().to_numpy()

    trades = []
    in_trade = False
    waiting = False
    trailing = False
    entry_price = stop_price = peak = running_low = 0.0
    entry_day = held = wait_days = 0

    for i in range(window, n):
        if not in_trade and not waiting:
            if std[i] > 0:
                z = (close[i] - sma[i]) / std[i]
                if z <= -z_thresh:
                    if trail_buy_pct > 0:
                        waiting = True
                        running_low = low[i]
                        wait_days = 0
                    else:
                        # immediate next-bar market buy (approximated as this bar's own next open)
                        if i + 1 < n:
                            entry_price = open_[i + 1]
                            in_trade = True
                            entry_day = i + 1
                            held = 0
                            stop_price = entry_price * (1 - fixed_sl)
                            trailing = False
                            peak = entry_price
            continue

        if waiting:
            running_low = min(running_low, low[i])
            trigger = running_low * (1 + trail_buy_pct)
            wait_days += 1
            if high[i] >= trigger:
                entry_price = max(trigger, open_[i])
                in_trade = True
                waiting = False
                entry_day = i
                held = 0
                stop_price = entry_price * (1 - fixed_sl)
                trailing = False
                peak = entry_price
            elif wait_days > 10:
                waiting = False  # abandoned, unfilled -- mirrors entry-abandon timeout
            continue

        # in_trade
        held += 1
        exit_price = None
        reason = None

        if not trailing and arm_trigger is not None and high[i] >= entry_price * (1 + arm_trigger):
            trailing = True
            peak = max(peak, high[i])

        if trailing:
            peak = max(peak, high[i])
            trail_stop = peak * (1 - trail_sell_pct)
            if open_[i] <= trail_stop:
                exit_price, reason = open_[i], "TRAIL"
            elif low[i] <= trail_stop:
                exit_price, reason = trail_stop, "TRAIL"

        if exit_price is None:
            if open_[i] <= stop_price:
                exit_price, reason = open_[i], "SL"
            elif low[i] <= stop_price:
                exit_price, reason = stop_price, "SL"

        if exit_price is None and held >= max_hold_days:
            exit_price, reason = close[i], "TIME"

        if exit_price is not None:
            trades.append({
                "entry_day": entry_day, "exit_day": i,
                "entry_price": entry_price, "exit_price": exit_price,
                "held_days": held, "reason": reason,
                "return": (exit_price - entry_price) / entry_price,
            })
            in_trade = False

    return trades


def summarize(trades, bars, window_start, window_end):
    if not trades:
        result = {"trades": 0, "compounded_return": 0.0, "max_drawdown": None, "win_rate": None}
    else:
        equity = 1.0
        peak_equity = 1.0
        max_dd = 0.0
        wins = 0
        for t in trades:
            equity *= (1.0 + t["return"])
            peak_equity = max(peak_equity, equity)
            dd = (equity - peak_equity) / peak_equity
            max_dd = min(max_dd, dd)
            if t["return"] > 0:
                wins += 1
        result = {
            "trades": len(trades),
            "compounded_return": equity - 1.0,
            "max_drawdown": max_dd,
            "win_rate": wins / len(trades),
        }
    start_i = min(bars.index.searchsorted(window_start), len(bars) - 1)
    end_i = min(bars.index.searchsorted(window_end), len(bars) - 1)
    bh_start = bars["Close"].iloc[start_i]
    bh_end = bars["Close"].iloc[end_i]
    result["buy_hold_return"] = (bh_end / bh_start) - 1.0
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=list(PROXIES.keys()))
    args = ap.parse_args()

    underlying_cache = {}
    rows = []
    for ticker in args.tickers:
        if ticker not in PROXIES:
            print(f"{ticker}: no proxy mapping, skipping", file=sys.stderr)
            continue
        proxy, leverage, note = PROXIES[ticker]
        params = get_node_params(ticker)
        if params is None:
            print(f"{ticker}: no watch_list node found, skipping", file=sys.stderr)
            continue

        if proxy not in underlying_cache:
            underlying_cache[proxy] = fetch_underlying(proxy)
        synth = synthesize_leveraged(underlying_cache[proxy], leverage)

        for crash, (start, bottom, recov_end) in CRASHES.items():
            start_ts, bottom_ts, end_ts = pd.Timestamp(start), pd.Timestamp(bottom), pd.Timestamp(recov_end)
            window = synth.loc[start:recov_end]
            if len(window) < params["window"] + 5:
                rows.append({"ticker": ticker, "crash": crash, "phase": "combined", "trades": 0,
                             "note": "insufficient data in window"})
                continue
            # include lookback bars before the window so the rolling z-score is warm
            lookback_start = synth.index.searchsorted(start_ts) - params["window"] - 1
            sim_bars = synth.iloc[max(0, lookback_start):synth.index.searchsorted(end_ts) + 1]
            all_trades = run_strategy_daily(sim_bars, params)
            win_start_i = sim_bars.index.searchsorted(start_ts)
            bottom_i = sim_bars.index.searchsorted(bottom_ts)
            all_trades = [t for t in all_trades if t["entry_day"] >= win_start_i]
            decline_trades = [t for t in all_trades if t["entry_day"] < bottom_i]
            recovery_trades = [t for t in all_trades if t["entry_day"] >= bottom_i]

            for phase, trades, seg_start, seg_end in [
                ("decline", decline_trades, start_ts, bottom_ts),
                ("recovery", recovery_trades, bottom_ts, end_ts),
                ("combined", all_trades, start_ts, end_ts),
            ]:
                summary = summarize(trades, sim_bars, seg_start, seg_end)
                summary.update({"ticker": ticker, "crash": crash, "phase": phase,
                                 "strategy": params["strategy"], "proxy": proxy, "leverage": leverage})
                if note:
                    summary["note"] = note
                rows.append(summary)

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 20)
    for crash in CRASHES:
        for phase in ["decline", "recovery", "combined"]:
            sub = df[(df["crash"] == crash) & (df["phase"] == phase)]
            print(f"\n=== {crash} / {phase} ===")
            print(sub[["ticker", "strategy", "trades", "compounded_return", "max_drawdown",
                        "win_rate", "buy_hold_return"]].to_string(index=False))

    out_path = "output/bear_market_stress.csv"
    df.to_csv(out_path, index=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
