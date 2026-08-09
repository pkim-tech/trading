"""Real-ticker daily-bar crash stress test for KORU/SOXL/AGQ -- uses ACTUAL historical
daily OHLC for these tickers (real ETF inception: AGQ 2008-12-04, SOXL 2010-03-11,
KORU 2013-04-10), not the synthetic underlying-proxy reconstruction
sim_bear_market_stress.py uses. This removes the reconstruction-uncertainty layer (no
SOXX/EWY/SLV leverage-math approximation) while keeping the same daily-bar
entry-signal-frequency caveat that test carries (one signal check per day can't see
the real hourly z-score triggers the live kernel trades on -- see this session's
research_log.md entries on the daily-bar-vs-real-kernel discrepancy found 2026-08-09).

Real consequence of using actual tickers instead of synthetic reconstruction: 2008 GFC
and the 2000 dotcom crash predate all 3 tickers' real inception (SOXL/AGQ/KORU didn't
exist yet) -- only 2020 COVID and the 2022 bear market are covered here. This is a
DIFFERENT, complementary tradeoff to sim_bear_market_stress.py, not a strict upgrade:
that script covers more crashes (via synthetic reconstruction), this one covers fewer
crashes but with zero reconstruction risk.

Usage: .venv/bin/python scripts/sim_real_ticker_daily_crash_stress.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sim_bear_market_stress import CRASHES, get_node_params, run_strategy_daily
from sim_skim_redeploy import build_equity_curve

REAL_TICKERS = ["SOXL", "AGQ", "KORU"]
# Only crashes that postdate every ticker's real inception are computable here.
COVERED_CRASHES = ["2020_covid", "2022_bear"]


def fetch_real_daily(ticker):
    df = yf.download(ticker, start="2005-01-01", progress=False, auto_adjust=True)
    if df.empty:
        raise RuntimeError(f"no data for {ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df[["Open", "High", "Low", "Close"]].dropna()


def main():
    print(f"{'Ticker':8s} {'Crash':12s} {'Trades':>7s} {'WinRate':>8s} {'StratRet':>10s} {'BuyHold':>10s}")
    rows = []
    for ticker in REAL_TICKERS:
        bars = fetch_real_daily(ticker)
        params = get_node_params(ticker)
        if params is None:
            print(f"{ticker}: no real v5 node params found")
            continue

        for crash in COVERED_CRASHES:
            start, bottom, recov_end = CRASHES[crash]
            start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(recov_end)
            if bars.index.min() > start_ts:
                print(f"{ticker:8s} {crash:12s}  -- predates real {ticker} inception ({bars.index.min().date()}), skipped")
                continue

            lookback_start = bars.index.searchsorted(start_ts) - params["window"] - 1
            sim_bars = bars.iloc[max(0, lookback_start):bars.index.searchsorted(end_ts) + 1]
            if len(sim_bars) < params["window"] + 5:
                continue
            win_start_i = sim_bars.index.searchsorted(start_ts)

            trades = run_strategy_daily(sim_bars, params)
            trades_in_window = [t for t in trades if t["entry_day"] >= win_start_i]
            eq_full = build_equity_curve(trades, len(sim_bars))
            strat_ret = eq_full[-1] / eq_full[win_start_i] - 1.0 if win_start_i < len(eq_full) else None

            bh_start = sim_bars["Close"].iloc[win_start_i]
            bh_end = sim_bars["Close"].iloc[-1]
            bh_ret = bh_end / bh_start - 1.0

            n = len(trades_in_window)
            win_rate = (sum(1 for t in trades_in_window if t["return"] > 0) / n) if n else None

            print(f"{ticker:8s} {crash:12s} {n:7d} "
                  f"{f'{win_rate:.1%}' if win_rate is not None else '—':>8s} "
                  f"{f'{strat_ret:+.1%}' if strat_ret is not None else '—':>10s} "
                  f"{bh_ret:+10.1%}")
            rows.append({"ticker": ticker, "crash": crash, "trades": n, "win_rate": win_rate,
                         "strategy_return": strat_ret, "buy_hold_return": bh_ret})

    Path("output").mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv("output/real_ticker_daily_crash_stress.csv", index=False)
    print("\nWrote output/real_ticker_daily_crash_stress.csv")
    print("\nNote: 2008 GFC and 2000 dotcom are NOT covered -- all 3 tickers postdate both crashes "
          "(AGQ inception 2008-12-04, SOXL 2010-03-11, KORU 2013-04-10).")


if __name__ == "__main__":
    main()
