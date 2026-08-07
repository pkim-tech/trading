"""
Research script: 5th pass on the 2026-08-04 evening thread, reframed after user clarification.
The observation motivating the whole thread wasn't a general trade-level pattern -- it's specific
to 2026 YTD: AGQ down -58.5% YTD while its strategy performs well, SOXL up +196.5% YTD while its
strategy is relatively weak. The first 4 passes (breach velocity, chop-cluster persistence,
trade-level regime regression using a 20-bar lookback, config cross-apply) all tested trade-level
features or the whole ~2-year history undifferentiated -- none of them could have found a
*calendar-period*-level effect like "the strategy does worse during a ticker's own sustained rally
and better during its own sustained decline."

This tests that directly: splits each ticker's full history into calendar quarters, computes (a)
the ticker's own price return over the quarter (real trend direction/magnitude) and (b) the
strategy's compounded return from trades entered in that quarter, then regresses strategy return
on price return with ticker fixed effects (same anti-pooling-artifact control as the earlier
regime-regression pass). A significant NEGATIVE coefficient would directly confirm the user's
observation as a real, generalizable pattern (strategy return moves opposite to price trend);
this also explicitly checks AGQ's and SOXL's own 2026 YTD quarters against the pooled result.

Usage: .venv/bin/python scripts/period_trend_vs_strategy_return.py [--tickers ...] [--watchlist-id 65]
       [--period Q|M] [--csv]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy.stats import spearmanr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtester import prep_inputs, OPEN
import strategies
from scripts.export_trades import (
    load_hourly,
    simulate_trail_both_annotated,
    simulate_trail_exit_chaos,
)

LIVE_DB = Path("cache/live/trading_live.db")
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
TARGET_H0, TARGET_H1 = 9, 14


def load_nodes(watchlist_id, tickers=None):
    con = sqlite3.connect(LIVE_DB)
    q = """
        SELECT MIN(id), ticker, strategy, window, z_score_threshold, arm_sell_pct, take_profit,
               fixed_sl, trail_buy_pct, trail_sell_pct, max_hold_hours, entry_timing
        FROM watch_list WHERE watchlist_id=? AND state='paper'
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


def get_trades(node):
    df_h = load_hourly(node["ticker"])
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    ind = build_indicators(node["strategy"], df_daily, node["window"])
    p = prep_inputs(df_h, ind)

    open_check = node["entry_timing"] == "open_check"
    if node["strategy"] == "TrailingBothZScoreBreakout":
        trades = simulate_trail_both_annotated(
            p, node["arm_pct"] / 100.0, node["fixed_sl"] / 100.0, node["max_hold_hours"],
            node["trail_buy_pct"] / 100.0, node["trail_sell_pct"] / 100.0,
            TARGET_H0, TARGET_H1, node["z"], open_check=open_check,
        )
    elif node["strategy"] == "TrailingExitZScoreBreakout":
        rng = np.random.default_rng(0)
        trades = simulate_trail_exit_chaos(
            p, node["arm_pct"] / 100.0, node["fixed_sl"] / 100.0, node["max_hold_hours"],
            node["trail_sell_pct"] / 100.0, TARGET_H0, TARGET_H1, node["z"],
            rng, "drop", 0.0, "drop", 0.0, open_check=open_check,
        )
    else:
        raise ValueError(f"unhandled strategy {node['strategy']}")

    timestamps = p["timestamps"]
    out = []
    for t in trades:
        if t["signal_i"] is None or t["result"] == OPEN:
            continue  # still-open mark-to-market row, not a completed trade
        out.append({"signal_time": timestamps[t["signal_i"]], "ret": t["ret"]})
    return out, df_daily


def period_label(ts, freq):
    return ts.to_period(freq)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", default=None)
    parser.add_argument("--watchlist-id", type=int, default=65)
    parser.add_argument("--period", choices=["Q", "M"], default="Q")
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()

    nodes = load_nodes(args.watchlist_id, args.tickers)
    rows = []
    for node in nodes:
        try:
            trades, df_daily = get_trades(node)
        except Exception as e:
            print(f"{node['ticker']}: failed ({e})")
            continue
        if not trades:
            continue
        tdf = pd.DataFrame(trades)
        tdf["period"] = tdf["signal_time"].apply(lambda t: period_label(t, args.period))

        close = df_daily["Close"]
        close_period = close.groupby(close.index.to_period(args.period))

        for period, g in tdf.groupby("period"):
            if period not in close_period.groups:
                continue
            period_close = close_period.get_group(period)
            if len(period_close) < 2:
                continue
            price_ret = float(period_close.iloc[-1] / period_close.iloc[0] - 1)
            strat_ret = float(np.prod([1 + r for r in g["ret"]]) - 1)
            rows.append({
                "ticker": node["ticker"], "period": str(period),
                "price_ret": price_ret, "strat_ret": strat_ret, "n_trades": len(g),
            })

    df = pd.DataFrame(rows)
    if df.empty:
        print("No period data.")
        return
    pd.set_option("display.width", 160)
    pd.set_option("display.max_rows", None)

    if args.csv:
        OUTPUT_DIR.mkdir(exist_ok=True)
        df.to_csv(OUTPUT_DIR / "period_trend_vs_strategy_return.csv", index=False)
        print(f"Wrote {OUTPUT_DIR / 'period_trend_vs_strategy_return.csv'}\n")

    print("--- AGQ / SOXL periods (the observation that motivated this test) ---")
    check = df[df["ticker"].isin(["AGQ", "SOXL"])].sort_values(["ticker", "period"])
    print(check.to_string(index=False))

    print(f"\n--- All periods, all tickers (n={len(df)}) ---")
    print(df.sort_values(["ticker", "period"]).to_string(index=False))

    print("\n--- Pooled Spearman (price_ret vs strat_ret) ---")
    rho, p = spearmanr(df["price_ret"], df["strat_ret"])
    print(f"n={len(df)}  rho={rho:.3f}  p={p:.4f}  (negative rho = strategy return moves "
          f"opposite the ticker's own price trend, matching the observation)")

    print("\n--- Per-ticker Spearman ---")
    for ticker, g in df.groupby("ticker"):
        if len(g) < 5:
            print(f"{ticker}: n={len(g)}, too few periods")
            continue
        rho, p = spearmanr(g["price_ret"], g["strat_ret"])
        print(f"{ticker}: n={len(g)}  rho={rho:+.3f}  p={p:.3f}")

    print("\n--- Pooled OLS: strat_ret ~ price_ret + ticker fixed effects ---")
    X = df[["price_ret"]].copy()
    X["price_ret"] = (X["price_ret"] - X["price_ret"].mean()) / X["price_ret"].std()
    dummies = pd.get_dummies(df["ticker"], prefix="tk", drop_first=True, dtype=float)
    X = pd.concat([X, dummies], axis=1)
    X = sm.add_constant(X)
    model = sm.OLS(df["strat_ret"].astype(float), X.astype(float)).fit()
    print(model.summary().tables[1])
    print(f"\nR-squared={model.rsquared:.4f}  n={int(model.nobs)}")


if __name__ == "__main__":
    main()
