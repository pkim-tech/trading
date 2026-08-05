"""
Diagnoses why real paper trading fires MORE entries than the hourly-bar backtest replay
predicts for the same node config and date window (found 2026-08-04 very late via
scripts/paper_vs_backtest_reconcile.py -- HIBL/USD/SOXL/YANG all showed real trade-count gaps,
paper consistently higher). Ruled out: node config drift (scripts/check_ticker_audit_history.py
showed no parameter changes since node creation); a stale-exit-price bug in strategies.py's
check_exit (that would misprice an existing exit, not create additional entries).

Hypothesis: active_signals._scan_buy_signals checks the CURRENT REAL-TIME price at every poll
within the ~15-minute signal window (roughly every 30-40s, so ~20-30 checks), while the
hourly-bar backtest only evaluates the bar's Close (or Open for open_check) once. Real
intra-window price noise could cross the entry threshold briefly and recover before the hourly
bar's recorded value -- a real signal paper would catch and the coarse hourly-bar backtest
structurally cannot see, independent of any bug.

Ground truth, not an approximation: paper's real signal_price IS captured at the moment each
signal fires (signals_db.open_position/log_trade_entry, paper_positions/paper_trade_log's
signal_price column) -- this script pulls that real recorded value directly rather than
re-deriving an estimate from 5-min bars. Compares it against the hourly bar's own Close/Open at
that timestamp: if paper's real signal_price crossed the entry threshold while the hourly bar's
value did not, that's direct, non-approximated confirmation of the intra-window-noise
hypothesis for that specific trade.

Usage: .venv/bin/python scripts/paper_signal_intrabar_check.py --ticker YANG [--start 2026-07-22]
       [--end 2026-08-04] [--watchlist-id 65]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import strategies
from scripts.export_trades import load_hourly, simulate_trail_exit_chaos
from scripts.drought_overlay_test import load_nodes

LIVE_DB = Path("cache/live/trading_live.db")


def _evaluated_bar_hour(signal_hour):
    """Maps a real signal's wall-clock hour to the HOURLY BAR the live daemon's signal check
    actually evaluates -- these are NOT the same hour for the ambient close-window checks.
    Hourly bars are labeled by start time (CLAUDE.md); the daemon's two ambient windows
    (10:25-10:40, 15:25-15:40) evaluate the CLOSE of the prior bar (9:30, 14:30 respectively),
    while the two open_check windows (9:31-9:40, 14:31-14:40) evaluate the OPEN of the current
    bar (9:30, 14:30). So real signal hour 10 or 15 -> evaluated bar hour 9 or 14; real signal
    hour 9 or 14 -> evaluated bar hour 9 or 14 (unchanged). Getting this wrong (comparing
    against the wrong hourly bar) produced a real, wrong conclusion 2026-08-04 very late --
    found by Opus review, not caught while writing the original version of this script."""
    if signal_hour in (10, 15):
        return signal_hour - 1
    return signal_hour


def get_real_paper_signals(ticker, start, end):
    """Pulls every real captured signal_price/signal_time for this ticker's paper activity
    in the window, from both closed trades (paper_trade_log) and any still-pending buy
    (paper_pending_buys) -- the actual ground-truth values paper trading saw, not an estimate."""
    con = sqlite3.connect(LIVE_DB)
    closed = con.execute("""
        SELECT signal_price, entry_time AS signal_time, 'closed' AS source
        FROM paper_trade_log WHERE ticker=? AND entry_time >= ? AND entry_time <= ?
    """, (ticker, start, end)).fetchall()
    pending = con.execute("""
        SELECT signal_price, signal_time, 'pending' AS source
        FROM paper_pending_buys WHERE ticker=? AND signal_time >= ? AND signal_time <= ?
    """, (ticker, start, end)).fetchall()
    con.close()
    return [dict(zip(["signal_price", "signal_time", "source"], r)) for r in closed + pending]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--start", default="2026-07-22")
    parser.add_argument("--end", default=None)
    parser.add_argument("--watchlist-id", type=int, default=65)
    args = parser.parse_args()
    end = args.end or pd.Timestamp.now().strftime("%Y-%m-%d")

    # end is a YYYY-MM-DD date; extend to end-of-day or trades entered ON the end date are
    # silently excluded by the lexical string comparison (found by Opus review 2026-08-04 very late).
    signals = get_real_paper_signals(args.ticker, args.start, f"{end} 23:59:59")
    if not signals:
        print(f"No real paper signals for {args.ticker} in this window.")
        return

    node = load_nodes(args.watchlist_id, [args.ticker])[0]
    print(f"Node config: strategy={node['strategy']} window={node['window']} z={node['z']} "
          f"entry_timing={node['entry_timing']} fixed_sl={node['fixed_sl']} "
          f"trail_sell_pct={node['trail_sell_pct']} arm_pct={node['arm_pct']}")
    df_h = load_hourly(args.ticker)
    daily = df_h.resample("D").last().dropna(subset=["Close"])
    strat_cls = getattr(strategies, node["strategy"])
    ind = strat_cls(window=node["window"]).generate_daily_indicators(daily)
    z_thresh = node["z"]

    for sig in signals:
        ts = pd.Timestamp(sig["signal_time"])
        day = ts.normalize()
        prior_days = ind.index[ind.index < day]
        if len(prior_days) == 0:
            print(f"{ts}: no prior completed day's SMA/Std, skipping")
            continue
        sma, std = ind.loc[prior_days[-1], ["SMA", "Std"]]
        lower_band = sma - std * z_thresh

        real_price = sig["signal_price"]
        real_z = (real_price - sma) / std
        real_crossed = real_price <= lower_band

        eval_hour = _evaluated_bar_hour(ts.hour)
        hour_bar = df_h[df_h.index.hour == eval_hour][df_h[df_h.index.hour == eval_hour].index.normalize() == day]
        if hour_bar.empty:
            hourly_close, hourly_z, hourly_crossed = None, None, None
        else:
            hourly_close = hour_bar.iloc[0]["Close"]
            hourly_z = (hourly_close - sma) / std
            hourly_crossed = hourly_close <= lower_band

        print(f"\n{args.ticker} paper signal ({sig['source']}) at {ts} (real signal hour={ts.hour}, "
              f"evaluated bar hour={eval_hour}):")
        print(f"  real captured signal_price=${real_price:.4f}  z={real_z:.2f}  "
              f"crossed_threshold={real_crossed}  (lower_band=${lower_band:.4f})")
        if hourly_close is not None:
            print(f"  hourly bar Close=${hourly_close:.4f}  z={hourly_z:.2f}  "
                  f"crossed_threshold={hourly_crossed}")
            if real_crossed and not hourly_crossed:
                print(f"  --> CONFIRMED: paper's real price crossed the threshold, the evaluated "
                      f"bar's Close did not. Real intra-window noise, not visible to the backtest.")
            elif real_crossed and hourly_crossed:
                print(f"  --> Both agree a real crossing happened on the bar the daemon actually "
                      f"evaluates -- if the full trade-count still doesn't reconcile, the cause is "
                      f"elsewhere (config mismatch, prior open position, boundary-condition drift), "
                      f"not bar-timing.")
        else:
            print(f"  no matching hourly bar found for this timestamp's hour")

    print(f"\n--- prep_inputs daily_idx trace for the real signal timestamps ---")
    from backtester import prep_inputs
    df_h3 = load_hourly(args.ticker)
    daily3 = df_h3.resample("D").last().dropna(subset=["Close"])
    ind3 = strat_cls(window=node["window"]).generate_daily_indicators(daily3)
    p = prep_inputs(df_h3, ind3)
    for sig in signals:
        ts = pd.Timestamp(sig["signal_time"])
        eval_hour = _evaluated_bar_hour(ts.hour)
        matching_bars = [i for i, t in enumerate(p["timestamps"]) if t.normalize() == ts.normalize() and t.hour == eval_hour]
        if not matching_bars:
            print(f"  {ts}: NO MATCHING HOURLY BAR in prep_inputs at all (evaluated_hour={eval_hour})")
            continue
        i = matching_bars[0]
        di = p["daily_idx"][i]
        if di < 0:
            print(f"  {ts}: bar exists, but daily_idx={di} (<0, entry check SKIPPED -- 'continue' branch)")
        else:
            sma, std = p["sma_arr"][di], p["std_arr"][di]
            print(f"  {ts}: bar_hour={p['timestamps'][i].hour}  daily_idx={di}  "
                  f"maps_to_day={ind3.index[di].date() if di < len(ind3) else 'OUT OF RANGE'}  "
                  f"SMA={sma:.4f} Std={std:.4f}")

    print(f"\n--- Real trace, using the actual trusted simulate_trail_exit_chaos function ---")
    if node["strategy"] == "TrailingExitZScoreBreakout":
        rng = np.random.default_rng(0)
        simulate_trail_exit_chaos(
            p, node["arm_pct"] / 100.0, node["fixed_sl"] / 100.0, node["max_hold_hours"],
            node["trail_sell_pct"] / 100.0, 9, 14, node["z"],
            rng, "drop", 0.0, "drop", 0.0, open_check=(node["entry_timing"] == "open_check"),
            trace=(pd.Timestamp(args.start), pd.Timestamp(end) + pd.Timedelta(days=1)),
        )
    else:
        print(f"  (trace not yet wired for {node['strategy']})")

    print(f"\n--- Raw backtest replay trades for {args.ticker} (full history) ---")
    from scripts.drought_overlay_test import get_trades_and_bars
    trades, df_h2 = get_trades_and_bars(node)
    window_trades = [t for t in trades if t["signal_i"] is not None
                      and pd.Timestamp(args.start) <= df_h2.index[t["signal_i"]] <= pd.Timestamp(end)]
    if not window_trades:
        print(f"  ZERO backtest trades in {args.start} to {end} -- confirmed divergence from real paper activity.")
        real_trades = [t for t in trades if t["signal_i"] is not None]
        before = [t for t in real_trades if df_h2.index[t["signal_i"]] < pd.Timestamp(args.start)]
        after = [t for t in real_trades if df_h2.index[t["signal_i"]] > pd.Timestamp(end)]
        if before:
            t = before[-1]
            print(f"  Last backtest trade BEFORE window: signal={df_h2.index[t['signal_i']]}  "
                  f"exit={df_h2.index[t['exit_i']]}  ret={t['ret']:.4f}  result={t['result']}  "
                  f"(in_trade from signal to exit -- does this span the window?)")
        if after:
            t = after[0]
            print(f"  First backtest trade AFTER window: signal={df_h2.index[t['signal_i']]}")
    else:
        for t in window_trades:
            print(f"  signal_i time={df_h2.index[t['signal_i']]}  ret={t['ret']:.4f}  result={t['result']}")


if __name__ == "__main__":
    main()
