"""
Research script: tests a specific hypothesis about the still-unexplained 2026 AGQ/SOXL divergence
(AGQ strategy +52.3% YTD despite price -58%; SOXL strategy -30.8% YTD despite price +197% -- see
docs/research_log.md's 2026-08-04 entries). Prior tests tonight (breach velocity, chop-cluster
persistence, vol/slope/crossing-rate regime regression) all found nothing using pre-entry regime
features. This tests a DIFFERENT kind of feature: not what the price was doing before entry, but
how close a losing trade came to actually reverting before its stop-loss/time exit fired.

Hypothesis: SOXL's 2026 losses are trades where price kept falling after entry with no real
attempt at reversion (consistent with a low-noise, grinding, monotonic rally where dips are
shallow but don't round-trip); AGQ's 2026 losses (what few there are) got closer to reverting
before the exit caught them (consistent with two-sided chop even within an overall decline).

For every LOSS/TLOSS trade, computes peak_unrealized_ret = the best price reached (max High)
between entry and exit, relative to entry price -- how close the position got to being profitable
before it was closed out. A value near 0 means "almost recovered"; a large negative value means
"never got close." Compares this distribution for AGQ vs SOXL, and each ticker's 2026 vs its own
full-history baseline (the within-ticker comparison is the real test -- it controls for any fixed
per-ticker difference in how this metric behaves, isolating whether 2026 specifically changed it).

Usage: .venv/bin/python scripts/near_miss_retracement_test.py [--tickers AGQ SOXL ...]
       [--watchlist-id 65] [--csv]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import mannwhitneyu

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtester import prep_inputs, OPEN, WIN, TWIN
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


def get_trades_and_highs(node):
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
    return trades, p


def annotate_near_miss(trades, p, ticker):
    highs, timestamps = p["highs"], p["timestamps"]
    rows = []
    for t in trades:
        if t["signal_i"] is None or t["result"] == OPEN:
            continue
        entry_i, exit_i, entry_p = t["entry_i"], t["exit_i"], t["entry_p"]
        best_high = float(np.max(highs[entry_i:exit_i + 1]))
        peak_unrealized_ret = best_high / entry_p - 1.0
        rows.append({
            "ticker": ticker,
            "entry_time": timestamps[entry_i],
            "win": t["result"] in (WIN, TWIN),
            "ret": t["ret"],
            "peak_unrealized_ret": peak_unrealized_ret,
        })
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", default=["AGQ", "SOXL"])
    parser.add_argument("--watchlist-id", type=int, default=65)
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()

    nodes = load_nodes(args.watchlist_id, args.tickers)
    all_rows = []
    for node in nodes:
        try:
            trades, p = get_trades_and_highs(node)
        except Exception as e:
            print(f"{node['ticker']}: failed ({e})")
            continue
        all_rows.extend(annotate_near_miss(trades, p, node["ticker"]))

    df = pd.DataFrame(all_rows)
    if df.empty:
        print("No trades found.")
        return
    df["year"] = df["entry_time"].dt.year
    df["is_2026"] = df["year"] == 2026

    if args.csv:
        OUTPUT_DIR.mkdir(exist_ok=True)
        df.to_csv(OUTPUT_DIR / "near_miss_retracement_test.csv", index=False)
        print(f"Wrote {OUTPUT_DIR / 'near_miss_retracement_test.csv'}\n")

    pd.set_option("display.width", 160)

    print("--- LOSS trades only: peak_unrealized_ret (how close to breakeven before exit) ---")
    losses = df[~df["win"]]
    summary = losses.groupby(["ticker", "is_2026"])["peak_unrealized_ret"].agg(
        n="size", mean="mean", median="median"
    ).round(4)
    print(summary.to_string())

    print("\n--- Within-ticker: 2026 vs pre-2026, LOSS trades (Mann-Whitney on peak_unrealized_ret) ---")
    for ticker, g in losses.groupby("ticker"):
        g2026 = g[g["is_2026"]]["peak_unrealized_ret"]
        gpre = g[~g["is_2026"]]["peak_unrealized_ret"]
        if len(g2026) < 5 or len(gpre) < 5:
            print(f"{ticker}: n_2026={len(g2026)} n_pre={len(gpre)}, too few to test")
            continue
        _, p_val = mannwhitneyu(g2026, gpre, alternative="two-sided")
        print(f"{ticker}: 2026 mean={g2026.mean():.4f} (n={len(g2026)})  "
              f"pre-2026 mean={gpre.mean():.4f} (n={len(gpre)})  Mann-Whitney p={p_val:.4f}")

    if "AGQ" in df["ticker"].values and "SOXL" in df["ticker"].values:
        print("\n--- AGQ vs SOXL, 2026 LOSS trades only ---")
        agq_2026 = losses[(losses["ticker"] == "AGQ") & (losses["is_2026"])]["peak_unrealized_ret"]
        soxl_2026 = losses[(losses["ticker"] == "SOXL") & (losses["is_2026"])]["peak_unrealized_ret"]
        if len(agq_2026) >= 5 and len(soxl_2026) >= 5:
            _, p_val = mannwhitneyu(agq_2026, soxl_2026, alternative="two-sided")
            print(f"AGQ 2026: mean={agq_2026.mean():.4f} median={agq_2026.median():.4f} n={len(agq_2026)}")
            print(f"SOXL 2026: mean={soxl_2026.mean():.4f} median={soxl_2026.median():.4f} n={len(soxl_2026)}")
            print(f"Mann-Whitney p={p_val:.4f}")
        else:
            print(f"AGQ n={len(agq_2026)}, SOXL n={len(soxl_2026)} -- too few for a direct test")


if __name__ == "__main__":
    main()
