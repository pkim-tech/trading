"""
Research script: quantifies the real cost of "turn the algo off for December" (raised
2026-08-16, after scripts/seasonal_volatility_check.py found SOXL/TQQQ consistently quieter
in December across all 3 cached years, but AGQ's Dec 2025 spike -- a genuine +38.5% underlying
move, not noise -- broke the pattern and plausibly drove real strategy performance).

For each of the 10 real capital-at-stake watch_list nodes, runs a full-history backtest of
that EXACT live config (direct kernel call via run_backtest_dispatch, reusing
quarterly_rebalance_walkforward.py's dispatch_kwargs mapping), then compares the real
compounded return WITH every trade vs. WITH December trades removed (single-trade-removal
stress-test style, same technique as docs/overlay_parameter_robustness_process.md) -- the delta
is the estimated cost (or benefit) of having shut the algo off every December in the cached
history. A trade is classified by its Exit Time's month, matching how a real shutdown would
actually prevent that trade from ever closing (an already-open Dec 1 position started in
November would still need to close normally; not modeled here, this only removes trades whose
combined activity mostly falls in December -- see the entry/exit month print for the exact rule).

Usage: .venv/bin/python scripts/december_shutdown_impact_check.py
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import strategies
from backtester import run_backtest_dispatch, prep_inputs
from quarterly_rebalance_walkforward import dispatch_kwargs

LIVE_DB = Path("cache/live/trading_live.db")
CACHE_DIR = Path("cache/research")


def load_real_nodes():
    conn = sqlite3.connect(LIVE_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT ticker, account, strategy, window, z_score_threshold AS z, fixed_sl,
               arm_sell_pct, take_profit, trail_buy_pct, trail_sell_pct, max_hold_hours,
               entry_timing, starting_notional
        FROM watch_list
        WHERE ticker IN ('AGQ','ETHU','JNUG','DFEN','GDXU','KORU','DPST','NUGT','SOXL','SOXS')
          AND account IN ('brokerage','roth','ira') AND state='live'
    """).fetchall()
    nodes = []
    for r in rows:
        d = dict(r)
        # arm_pct is take_profit for TrailingExit, arm_sell_pct for TrailingBoth --
        # same convention as cross_apply_config_test.py/quarterly_rebalance_walkforward.py.
        d["arm_pct"] = d["arm_sell_pct"] if d["strategy"] == "TrailingBothZScoreBreakout" else d["take_profit"]
        nodes.append(d)
    return nodes


def run_full_history(node):
    import pandas as pd
    path = CACHE_DIR / f"{node['ticker']}_1h.csv"
    df_hourly = pd.read_csv(path, index_col=0, parse_dates=True).sort_index()
    df_hourly.index = pd.to_datetime(df_hourly.index).tz_localize(None)
    close_col = "Adj Close" if "Adj Close" in df_hourly.columns else "Close"
    df_daily = df_hourly.resample("D").last().dropna(subset=[close_col])
    strat_cls = getattr(strategies, node["strategy"])
    ind = strat_cls(window=node["window"], z_score_threshold=node["z"]).generate_daily_indicators(df_daily)
    prep = prep_inputs(df_hourly, ind)
    kwargs = dispatch_kwargs(node)
    trades = run_backtest_dispatch(df_hourly=None, df_daily_indicators=None, ticker=node["ticker"],
                                    prep=prep, **kwargs)
    return [t for t in trades if t["Result"] != "OPEN"]


def compounded(rets):
    if not rets:
        return 0.0
    return float(np.prod([1 + r for r in rets]) - 1) * 100.0


def daily_dec_vol(ticker, year):
    """Annualized realized vol for December `year` only, same measure as
    seasonal_volatility_check.py."""
    import pandas as pd
    path = CACHE_DIR / f"{ticker}_1h.csv"
    df = pd.read_csv(path, index_col=0, parse_dates=True).sort_index()
    close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    daily = df[close_col].resample("D").last().dropna()
    log_ret = np.log(daily / daily.shift(1)).dropna()
    sub = log_ret[(log_ret.index.year == year) & (log_ret.index.month == 12)]
    return (float(sub.std() * np.sqrt(252) * 100), len(sub)) if len(sub) > 1 else (None, len(sub))


def main():
    nodes = load_real_nodes()
    years = [2023, 2024, 2025]

    for node in nodes:
        trades = run_full_history(node)
        all_rets = [t["Return"] for t in trades]
        full_ret = compounded(all_rets)
        print(f"=== {node['ticker']} ({node['account']}, {node['strategy']}) -- full lifetime "
              f"return {full_ret:+.1f}%, {len(trades)} trades ===")
        print(f"  {'year':6s}{'dec_return_delta':>18s}{'dec_trades':>12s}{'dec_win%':>10s}"
              f"{'dec_vol':>10s}{'vol_n':>7s}")
        for year in years:
            dec_trades = [t for t in trades if t["Exit Time"].year == year and t["Exit Time"].month == 12]
            without_this_dec_rets = [t["Return"] for t in trades
                                      if not (t["Exit Time"].year == year and t["Exit Time"].month == 12)]
            delta = full_ret - compounded(without_this_dec_rets)
            dec_win = (sum(1 for t in dec_trades if t["Return"] > 0) / len(dec_trades) * 100.0) if dec_trades else float("nan")
            vol, vol_n = daily_dec_vol(node["ticker"], year)
            vol_s = f"{vol:.0f}%" if vol is not None else "no data"
            win_s = f"{dec_win:.0f}%" if dec_trades else "-"
            print(f"  {year:<6d}{delta:>+17.1f}%{len(dec_trades):>12d}{win_s:>10s}{vol_s:>10s}{vol_n:>7d}")
        print()

    print(f"'dec_return_delta' = full_return - (compounded return with THAT year's December "
          f"trades removed, all other trades incl. other years' Decembers left in place) -- "
          f"isolates each individual December's own contribution. 'dec_vol' is that same "
          f"December's annualized realized vol (daily log returns), for the side-by-side "
          f"return-vs-vol comparison.")


if __name__ == "__main__":
    main()
