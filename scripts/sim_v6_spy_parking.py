"""
v6 idea (raised 2026-07-22): when a real exit lands on the trading day's last
bar, don't let the freed capital sit idle in cash until the next signal --
buy SPY at the exit price, hold it continuously through however many days
pass, and market-sell it the instant the next real trailing-buy signal
fires (SPY is liquid enough that this adds only a few seconds of friction,
modeled here as zero slippage/delay). Mid-day exits are unaffected -- they
stay cash (0% during the gap), matching the existing backtest's implicit
assumption, since the idea is specifically about *overnight* idle capital.

This is a pure capital-utilization question, same family as the trailing-buy
sizing/idle-capital research already done -- backtest-only, no live kernel
or strategies.py change. Uses the real, parity-verified
simulate_trail_both_annotated trade list (not a reimplementation) for entry/
exit timing, and real SPY hourly Close prices to compute the actual gap
return. Bars are hour-labeled by *start* time (target_hours=(9,14)) -- the
14:xx bar is the trading day's last checked bar, so an exit on that bar is
the "end of day" case the idea describes.

Usage: .venv/bin/python scripts/sim_v6_spy_parking.py TICKER [--watchlist-id 65]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

import strategies
from backtester import prep_inputs
from scripts.export_trades import simulate_trail_both_annotated, load_hourly

LIVE_DB = Path(__file__).resolve().parent.parent / "cache" / "live" / "trading_live.db"


def get_node(ticker, watchlist_id):
    conn = sqlite3.connect(LIVE_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM watch_list WHERE ticker=? AND watchlist_id=?", (ticker, watchlist_id)
    ).fetchone()
    conn.close()
    if row is None:
        raise SystemExit(f"no watch_list row for {ticker} in watchlist_id={watchlist_id}")
    return dict(row)


def nearest_price(df, ts):
    """SPY's Close at or before ts -- 'nearest, not after' since we're pricing
    a real buy/sell that can only happen once that bar is actually known."""
    idx = df.index[df.index <= ts]
    if len(idx) == 0:
        return None
    return float(df.loc[idx[-1], "Close"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--watchlist-id", type=int, default=65)
    args = ap.parse_args()
    ticker = args.ticker

    node = get_node(ticker, args.watchlist_id)
    df_h = load_hourly(ticker)
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    df_spy = load_hourly("SPY")

    strat = strategies.TrailingBothZScoreBreakout(window=node["window"],
                                                    z_score_threshold=node["z_score_threshold"])
    ind = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_h, ind)

    raw_trades = simulate_trail_both_annotated(
        p, node["arm_sell_pct"] / 100.0, node["fixed_sl"] / 100.0, node["max_hold_hours"],
        node["trail_buy_pct"] / 100.0, node["trail_sell_pct"] / 100.0, 9, 14,
        node["z_score_threshold"],
    )
    timestamps = p["timestamps"]

    # "End of day" = this bar is genuinely the last one on file for its calendar
    # date -- not a fixed hour cutoff. The exit-check loop runs on every bar
    # including the excluded-from-new-entries 15:30 bar, so a real EOD exit can
    # land on either the 14:xx or 15:xx bar depending on what data exists for
    # that specific day (holidays/early closes shift this too).
    last_bar_of_day = pd.Series(timestamps).groupby(pd.Series(timestamps).dt.date).max()
    last_bar_set = set(last_bar_of_day.values)

    baseline_mult = 1.0
    v6_mult = 1.0
    rows = []
    prev_exit_ts = None
    prev_was_eod = False

    for t in raw_trades:
        entry_ts = timestamps[t["entry_i"]]
        exit_ts = timestamps[t["exit_i"]]
        trade_ret = t["ret"]

        gap_ret = 0.0
        if prev_was_eod and prev_exit_ts is not None:
            spy_start = nearest_price(df_spy, prev_exit_ts)
            spy_end = nearest_price(df_spy, entry_ts)
            if spy_start and spy_end:
                gap_ret = (spy_end / spy_start) - 1

        baseline_mult *= (1 + trade_ret)
        v6_mult *= (1 + gap_ret) * (1 + trade_ret)

        rows.append({
            "entry_time": entry_ts, "exit_time": exit_ts, "trade_return_pct": trade_ret * 100,
            "prior_gap_spy_return_pct": gap_ret * 100, "prior_gap_was_eod_park": prev_was_eod,
        })

        prev_exit_ts = exit_ts
        prev_was_eod = exit_ts in last_bar_set

    df_out = pd.DataFrame(rows)
    n_eod = int(df_out["prior_gap_was_eod_park"].sum())
    print(f"{ticker} -- {len(raw_trades)} trades, {n_eod} preceded by an EOD-exit SPY-parking gap")
    print(f"  baseline compounded return (idle cash during every gap): {(baseline_mult - 1) * 100:,.1f}%")
    print(f"  v6 compounded return (SPY-parked during EOD gaps only):   {(v6_mult - 1) * 100:,.1f}%")
    print(f"  v6 / baseline multiplier ratio: {v6_mult / baseline_mult:.4f}")
    out_path = Path("output") / f"v6_spy_parking_{ticker}.csv"
    out_path.parent.mkdir(exist_ok=True)
    df_out.to_csv(out_path, index=False)
    print(f"  per-trade detail written to {out_path}")


if __name__ == "__main__":
    main()
