"""
Research script: does the strategy's edge track regime structure (realized volatility, trend
slope, how often z crosses the trigger, how much time z spends pinned past it) rather than raw
price direction? Raised 2026-08-04 evening, reasoning about SOXL's weak uptrend performance vs.
AGQ's strength in downturns -- two narrower follow-ons (z-entry-velocity audit, chop-cluster scan)
came back negative/non-robust; this is the direct structural-regime test discussed the same
evening.

For each real trade (real v5 watchlist nodes, the same two-daily-signal-window trade lists
entry_timing_seasonality.py trusts -- NOT the chop-cluster scan's every-bar research variant),
computes 4 regime features using only bars strictly BEFORE the entry signal (no leakage):
  - vol_20:       std of hourly log returns over the 20 bars before signal
  - slope_20:     OLS trend slope over those same 20 bars, normalized to %/bar
  - cross_rate_60: how often z crossed down through -z_thresh in the 60 bars before signal
  - sat_frac_60:   fraction of those 60 bars spent with z already <= -z_thresh (pinned/saturated)

Reports (a) a Spearman correlation matrix among the 4 features + return, to see how collinear
they are, (b) VIF for each feature (formal collinearity check), (c) per-ticker univariate
correlations against return, and (d) a pooled OLS regression of return on all 4 features PLUS
ticker fixed effects (dummy per ticker) -- the fixed effects are the direct fix for the
cross-ticker pooling artifact the chop-cluster scan ran into (different tickers have different
baseline win rates; without controlling for ticker identity a pooled regression can manufacture
a spurious coefficient).

Usage: .venv/bin/python scripts/regime_structural_scan.py [--tickers ...] [--watchlist-id 65] [--csv]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy.stats import spearmanr
from statsmodels.stats.outliers_influence import variance_inflation_factor

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
VOL_WINDOW = 20
CROSS_WINDOW = 60


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


def get_trades_and_series(node):
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

    daily_idx, sma_arr, std_arr, prices = p["daily_idx"], p["sma_arr"], p["std_arr"], p["prices"]
    valid = daily_idx >= 0
    z = np.full(len(prices), np.nan)
    z[valid] = (prices[valid] - sma_arr[daily_idx[valid]]) / std_arr[daily_idx[valid]]
    log_ret = np.full(len(prices), np.nan)
    log_ret[1:] = np.diff(np.log(prices))

    return trades, z, log_ret, prices, node["z"]


def compute_features(si, z, log_ret, prices, z_thresh):
    if si < CROSS_WINDOW:
        return None
    # vol_20 / slope_20: 20 bars strictly before the signal bar
    win_v = slice(si - VOL_WINDOW, si)
    rets_v = log_ret[win_v]
    px_v = prices[win_v]
    if np.isnan(rets_v).any() or np.isnan(px_v).any():
        return None
    vol_20 = float(np.std(rets_v))
    slope = float(np.polyfit(np.arange(VOL_WINDOW), px_v, 1)[0])
    slope_20 = slope / float(np.mean(px_v))

    # cross_rate_60 / sat_frac_60: 60 bars strictly before the signal bar
    win_c = slice(si - CROSS_WINDOW, si)
    z_c = z[win_c]
    if np.isnan(z_c).any():
        return None
    below = z_c <= -z_thresh
    crossings = int(np.sum(below[1:] & ~below[:-1]))
    cross_rate_60 = crossings / CROSS_WINDOW
    sat_frac_60 = float(np.mean(below))

    return dict(vol_20=vol_20, slope_20=slope_20, cross_rate_60=cross_rate_60, sat_frac_60=sat_frac_60)


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
            trades, z, log_ret, prices, z_thresh = get_trades_and_series(node)
        except Exception as e:
            print(f"{node['ticker']}: failed ({e})")
            continue
        for t in trades:
            si = t["signal_i"]
            if si is None or t["result"] == OPEN:
                continue
            feats = compute_features(si, z, log_ret, prices, z_thresh)
            if feats is None:
                continue
            feats.update(ticker=node["ticker"], ret=t["ret"], win=t["ret"] > 0)
            rows.append(feats)

    df = pd.DataFrame(rows)
    if df.empty:
        print("No trades found.")
        return
    print(f"n={len(df)} trades across {df['ticker'].nunique()} tickers\n")

    if args.csv:
        OUTPUT_DIR.mkdir(exist_ok=True)
        df.to_csv(OUTPUT_DIR / "regime_structural_scan.csv", index=False)
        print(f"Wrote {OUTPUT_DIR / 'regime_structural_scan.csv'}\n")

    feature_cols = ["vol_20", "slope_20", "cross_rate_60", "sat_frac_60"]

    print("--- Spearman correlation matrix (features + ret), pooled ---")
    corr = df[feature_cols + ["ret"]].corr(method="spearman")
    print(corr.round(3).to_string())

    print("\n--- VIF (collinearity check; VIF>5 = concerning, >10 = severe) ---")
    X = df[feature_cols].to_numpy(dtype=np.float64)
    X = (X - X.mean(axis=0)) / X.std(axis=0)
    for i, col in enumerate(feature_cols):
        vif = variance_inflation_factor(X, i)
        print(f"{col}: VIF={vif:.2f}")

    print("\n--- Per-ticker univariate Spearman (feature vs ret) ---")
    for ticker, g in df.groupby("ticker"):
        if len(g) < 10:
            print(f"{ticker}: n={len(g)}, too few")
            continue
        parts = []
        for col in feature_cols:
            rho, p = spearmanr(g[col], g["ret"])
            parts.append(f"{col}: rho={rho:+.3f} p={p:.3f}")
        print(f"{ticker} (n={len(g)}): " + "  ".join(parts))

    print("\n--- Pooled OLS: ret ~ vol_20 + slope_20 + cross_rate_60 + sat_frac_60 + ticker fixed effects ---")
    X = df[feature_cols].copy()
    for col in feature_cols:
        X[col] = (X[col] - X[col].mean()) / X[col].std()  # standardize for comparable coefficients
    ticker_dummies = pd.get_dummies(df["ticker"], prefix="tk", drop_first=True, dtype=float)
    X = pd.concat([X, ticker_dummies], axis=1)
    X = sm.add_constant(X)
    model = sm.OLS(df["ret"].astype(float), X.astype(float)).fit()
    print(model.summary().tables[1])
    print(f"\nR-squared={model.rsquared:.4f}  n={int(model.nobs)}")


if __name__ == "__main__":
    main()
