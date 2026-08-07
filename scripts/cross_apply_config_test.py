"""
Research script: 4th pass on the 2026-08-04 evening thread (SOXL weak in its own uptrend vs. AGQ
strong in its downturn). Three earlier passes (z-entry-velocity audit, chop-cluster scan,
structural-regime regression) all found nothing -- none of breach shape, chop persistence, or
realized-vol/slope/crossing-rate/saturation explains the difference. Checking node configs
directly surfaced something the earlier passes never controlled for: AGQ and SOXL don't just
differ by underlying asset -- they run genuinely different strategies (AGQ:
TrailingExitZScoreBreakout, immediate market-buy entry; SOXL: TrailingBothZScoreBreakout,
trailing-buy-wait entry) with different z-thresholds/hold times/exit params. Every regime feature
tested so far was computed the same way regardless of strategy, so a real config-driven effect
could have been hiding under "no signal."

This directly isolates asset vs. config: runs each of a ticker pair's real config against BOTH
tickers' own price data, using run_backtest_dispatch (the actual trusted dispatcher, not a
pure-Python mirror -- this needs the real numba kernel's aggregate stats, not bar-level
annotations). Cross-applying AGQ's config to SOXL's price data (and vice versa) asks: does the
ticker's own edge persist under a different strategy/config, or does it depend on the specific
config it was tuned with? If AGQ's config on SOXL's data (or SOXL's config on AGQ's data) looks
nothing like either ticker's "home" result, the config is doing more of the work than the asset.

Reports compounded return, win rate, and trade count for both tickers under both configs (4 cells);
the "home" (own config/own data) cells are the reference to compare cross-applied cells against.

Usage: .venv/bin/python scripts/cross_apply_config_test.py [--tickers AGQ SOXL] [--watchlist-id 65]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtester import prep_inputs, run_backtest_dispatch
import strategies
from scripts.export_trades import load_hourly

LIVE_DB = Path("cache/live/trading_live.db")


def load_node(ticker, watchlist_id):
    con = sqlite3.connect(LIVE_DB)
    row = con.execute("""
        SELECT ticker, strategy, window, z_score_threshold, arm_sell_pct, take_profit, fixed_sl,
               trail_buy_pct, trail_sell_pct, max_hold_hours, entry_timing
        FROM watch_list WHERE watchlist_id=? AND state='paper' AND ticker=?
        ORDER BY id LIMIT 1
    """, (watchlist_id, ticker)).fetchone()
    con.close()
    if row is None:
        raise ValueError(f"no paper-state node for {ticker} on watchlist {watchlist_id}")
    cols = ["ticker", "strategy", "window", "z", "arm_sell_pct", "take_profit", "fixed_sl",
            "trail_buy_pct", "trail_sell_pct", "max_hold_hours", "entry_timing"]
    node = dict(zip(cols, row))
    # take_profit holds the arm-sell threshold for TrailingExitZScoreBreakout nodes;
    # arm_sell_pct holds it for TrailingBothZScoreBreakout (never both populated on
    # the same row -- signals_db.py:983-988). See docs/research_log.md's 2026-08-04
    # correction entry -- the earlier bug hardcoded TP=disabled for every TrailingExit
    # node instead of reading its real value.
    node["arm_pct"] = node["arm_sell_pct"] if node["strategy"] == "TrailingBothZScoreBreakout" else node["take_profit"]
    return node


def dispatch_kwargs(node):
    """Maps a real watch_list node's stored params to run_backtest_dispatch's percent-scale
    kwargs, mirroring run_optimization_sweep.py's own per-strategy grid-axis meaning
    (see backtester.run_backtest_dispatch's docstring)."""
    strat_cls = getattr(strategies, node["strategy"])
    if node["strategy"] == "TrailingBothZScoreBreakout":
        return dict(
            strategy_class=strat_cls, take_profit=node["arm_pct"], sl_raw=node["trail_buy_pct"],
            fixed_sl=node["fixed_sl"], trail_pct_pct=node["trail_sell_pct"],
            max_hours_to_hold=node["max_hold_hours"], z_score_threshold=node["z"],
            entry_timing=node["entry_timing"],
        )
    if node["strategy"] == "TrailingExitZScoreBreakout":
        return dict(
            strategy_class=strat_cls, take_profit=node["arm_pct"], sl_raw=node["trail_sell_pct"],
            fixed_sl=node["fixed_sl"], trail_pct_pct=0.0,
            max_hours_to_hold=node["max_hold_hours"], z_score_threshold=node["z"],
            entry_timing=node["entry_timing"],
        )
    raise ValueError(f"unhandled strategy {node['strategy']}")


def build_prep(ticker, strategy_name, window):
    df_h = load_hourly(ticker)
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    strat_cls = getattr(strategies, strategy_name)
    ind = strat_cls(window=window).generate_daily_indicators(df_daily)
    return prep_inputs(df_h, ind)


def run_combo(data_ticker, config_node):
    """Runs config_node's strategy/params against data_ticker's own price series."""
    p = build_prep(data_ticker, config_node["strategy"], config_node["window"])
    kwargs = dispatch_kwargs(config_node)
    trades = run_backtest_dispatch(df_hourly=None, df_daily_indicators=None, ticker=data_ticker,
                                    prep=p, **kwargs)
    rets = [t["Return"] for t in trades if t["Result"] != "OPEN"]
    if not rets:
        return dict(trades=0, win_rate=float("nan"), compounded=float("nan"))
    wins = sum(r > 0 for r in rets)
    compounded = float(np.prod([1 + r for r in rets]) - 1)
    return dict(trades=len(rets), win_rate=wins / len(rets), compounded=compounded)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs=2, default=["AGQ", "SOXL"])
    parser.add_argument("--watchlist-id", type=int, default=65)
    args = parser.parse_args()
    t1, t2 = args.tickers

    n1 = load_node(t1, args.watchlist_id)
    n2 = load_node(t2, args.watchlist_id)
    print(f"{t1} config: {n1['strategy']}, window={n1['window']}, z={n1['z']}, "
          f"fixed_sl={n1['fixed_sl']}, entry_timing={n1['entry_timing']}")
    print(f"{t2} config: {n2['strategy']}, window={n2['window']}, z={n2['z']}, "
          f"fixed_sl={n2['fixed_sl']}, entry_timing={n2['entry_timing']}")

    rows = []
    for data_ticker in (t1, t2):
        for config_ticker, config_node in ((t1, n1), (t2, n2)):
            result = run_combo(data_ticker, config_node)
            rows.append({
                "data": data_ticker, "config": config_ticker,
                "home": data_ticker == config_ticker, **result,
            })

    df = pd.DataFrame(rows)
    df["win_rate"] = df["win_rate"].round(3)
    df["compounded"] = (df["compounded"] * 100).round(1)
    pd.set_option("display.width", 160)
    print("\n--- Cross-applied results (compounded % over full history, no compounding cap) ---")
    print(df.to_string(index=False))
    print("\n('home' rows are each ticker's own real config on its own data -- the baseline "
          "to compare the cross-applied rows against.)")


if __name__ == "__main__":
    main()
