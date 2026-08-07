"""
Research script: fixes a selection-bias bug in the earlier regime_structural_scan.py. That
script measured vol_20 only in the 20 bars before an actual trade signal -- structurally blind
to volatility during stretches with NO trade at all, which turned out to be exactly the mechanism
behind SOXL's weak 2026 (a 6-week smooth rally with zero signals fired, not bad trades during it).

Two different questions require two different units of analysis (raised directly by the user):
  - trade-conditional: given a trade occurred, what predicts ITS outcome (tonight's earlier
    passes -- velocity, overshoot, chop-cluster, the original regime_structural_scan.py)
  - calendar-time: given a time window, what predicts the strategy's return OVER THAT WINDOW,
    including the possibility that it trades zero times (this script)

For each ticker, bins time into weekly calendar periods (unconditional -- every week counts,
whether or not a trade fired), computes (a) realized volatility of the ticker's own price that
week and (b) the strategy's compounded return from whatever trades (zero or more) were entered
that week, then regresses strat_ret on volatility with ticker fixed effects.

Usage: .venv/bin/python scripts/period_volatility_regression.py [--tickers ...] [--watchlist-id 65]
       [--csv]
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
    for n in nodes:
        n["arm_pct"] = n["arm_sell_pct"] if n["strategy"] == "TrailingBothZScoreBreakout" else n["take_profit"]
    return nodes


def build_indicators(strategy_name, df_daily, window):
    strat_cls = getattr(strategies, strategy_name)
    return strat_cls(window=window).generate_daily_indicators(df_daily)


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
    return trades, df_h


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", default=None)
    parser.add_argument("--watchlist-id", type=int, default=65)
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()

    nodes = load_nodes(args.watchlist_id, args.tickers)
    rows = []
    for node in nodes:
        try:
            trades, df_h = get_trades(node)
        except Exception as e:
            print(f"{node['ticker']}: failed ({e})")
            continue

        log_ret = pd.Series(np.diff(np.log(df_h["Close"].values)), index=df_h.index[1:])
        weekly_vol = log_ret.groupby(log_ret.index.to_period("W")).std()

        trade_rets = []
        for t in trades:
            if t["signal_i"] is None or t["result"] == OPEN:
                continue
            trade_rets.append({"week": df_h.index[t["signal_i"]].to_period("W"), "ret": t["ret"]})
        tdf = pd.DataFrame(trade_rets)
        strat_by_week = (
            tdf.groupby("week")["ret"].apply(lambda s: float(np.prod(1 + s) - 1))
            if not tdf.empty else pd.Series(dtype=float)
        )
        n_trades_by_week = tdf.groupby("week").size() if not tdf.empty else pd.Series(dtype=int)

        for week, vol in weekly_vol.items():
            if pd.isna(vol) or vol == 0:
                continue
            rows.append({
                "ticker": node["ticker"], "week": str(week), "vol": float(vol),
                "strat_ret": float(strat_by_week.get(week, 0.0)),
                "n_trades": int(n_trades_by_week.get(week, 0)),
            })

    df = pd.DataFrame(rows)
    if df.empty:
        print("No data.")
        return
    pd.set_option("display.width", 160)
    pd.set_option("display.max_rows", None)

    if args.csv:
        OUTPUT_DIR.mkdir(exist_ok=True)
        df.to_csv(OUTPUT_DIR / "period_volatility_regression.csv", index=False)
        print(f"Wrote {OUTPUT_DIR / 'period_volatility_regression.csv'}\n")

    print("--- AGQ crash weeks (2026-02) / SOXL rally weeks (2026-04/05) sanity check ---")
    check = df[
        ((df["ticker"] == "AGQ") & (df["week"] >= "2026-01-26") & (df["week"] <= "2026-03-02")) |
        ((df["ticker"] == "SOXL") & (df["week"] >= "2026-03-30") & (df["week"] <= "2026-05-18"))
    ]
    print(check.to_string(index=False))

    print(f"\n--- Pooled Spearman (vol vs strat_ret), n={len(df)} weeks, {df['ticker'].nunique()} tickers ---")
    rho, p = spearmanr(df["vol"], df["strat_ret"])
    print(f"rho={rho:.3f}  p={p:.4f}")

    print("\n--- Per-ticker Spearman ---")
    for ticker, g in df.groupby("ticker"):
        if len(g) < 10:
            print(f"{ticker}: n={len(g)}, too few weeks")
            continue
        rho, p = spearmanr(g["vol"], g["strat_ret"])
        zero_trade_weeks = (g["n_trades"] == 0).sum()
        print(f"{ticker}: n={len(g)}  rho={rho:+.3f}  p={p:.3f}  "
              f"(zero-trade weeks: {zero_trade_weeks}/{len(g)})")

    print("\n--- Pooled OLS: strat_ret ~ vol + ticker fixed effects ---")
    X = df[["vol"]].copy()
    X["vol"] = (X["vol"] - X["vol"].mean()) / X["vol"].std()
    dummies = pd.get_dummies(df["ticker"], prefix="tk", drop_first=True, dtype=float)
    X = pd.concat([X, dummies], axis=1)
    X = sm.add_constant(X)
    model = sm.OLS(df["strat_ret"].astype(float), X.astype(float)).fit()
    print(model.summary().tables[1])
    print(f"\nR-squared={model.rsquared:.4f}  n={int(model.nobs)}")


if __name__ == "__main__":
    main()
