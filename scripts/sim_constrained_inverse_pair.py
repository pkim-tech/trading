"""
v6 idea, capital-constrained secondary trade (2026-08-03): pure-Python mirror
of backtester._simulate_trail (the TrailingExitZScoreBreakout kernel, what
GDXD/DUST/ZSL are currently swept under), extended with a second ticker's
real open-position calendar as a hard constraint on the secondary/inverse
side:
  - the secondary may NOT open a new position on any bar where the primary
    already holds one (an overlapping entry is simply skipped, the signal is
    lost -- not queued/delayed);
  - if the secondary already holds a position and the primary opens a NEW
    one, the secondary is force-closed at that bar's Open (capital returned
    before the primary needs it), tagged result=FORCED, distinct from the
    normal SL/TP/TIME exit reasons.

This directly answers the "does this pair actually collide, and what happens
if we forbid it" question raised in this session's overlap check
(scripts/sim_v6_inverse_overlap_check.py found 0.6%-41% collision rates
depending on the inverse's own node) -- rather than filtering the
unconstrained backtest's realized trades after the fact (which can't recover
the entries the constraint would have suppressed, or the different state a
forced early exit would leave behind), this reruns the state machine bar-by-
bar with the constraint live, so waiting/holding state is never silently
wrong.

Stage 1 (single-node audit, per .claude/skills/backtest-change-rollout) of a
prospective new kernel path -- this is NOT wired into backtester.py or the
real sweep engine yet. Verify manually here first; only promote to a real
numba kernel + full grid sweep once this shows the constrained node looks
different enough from the unconstrained optimum to be worth it (the user's
own hunch: this might need to be a distinct strategy/version, not just "v5
with a filter").

Usage:
  .venv/bin/python scripts/sim_constrained_inverse_pair.py --primary GDXU --secondary GDXD
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import strategies
from backtester import prep_inputs, run_backtest_dispatch, WIN, LOSS, TWIN, TLOSS, OPEN
from scripts.export_trades import load_hourly

LIVE_DB = Path(__file__).resolve().parent.parent / "cache" / "live" / "trading_live.db"
DB_PATH = "cache/research/trading_universe.db"

FORCED = 5  # new result code, local to this script only -- not in backtester.WIN/LOSS/... set
_RESULT_NAMES = {WIN: 'WIN', LOSS: 'LOSS', TWIN: 'TWIN', TLOSS: 'TLOSS', OPEN: 'OPEN', FORCED: 'FORCED'}


def get_primary_node(ticker, watchlist_id=65):
    conn = sqlite3.connect(LIVE_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM watch_list WHERE ticker=? AND watchlist_id=?", (ticker, watchlist_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def primary_real_trades(ticker, node):
    strategy_class = getattr(strategies, node["strategy"])
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(node["strategy"])
    sl_axis_real_col = "trail_sell_pct" if sl_axis_col == "trail_pct" else sl_axis_col
    sl_raw = node[sl_axis_real_col]
    trail_pct_pct = node["trail_sell_pct"] if fourth_axis_col == "trail_pct" else 0.0
    take_profit = node.get("arm_sell_pct") or 0.0

    df_h = load_hourly(ticker)
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    strat = strategy_class(window=node["window"], z_score_threshold=node["z_score_threshold"])
    ind = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_h, ind)

    trades = run_backtest_dispatch(
        strategy_class, df_h, ind, ticker,
        take_profit=take_profit, sl_raw=sl_raw, max_hours_to_hold=node["max_hold_hours"],
        z_score_threshold=node["z_score_threshold"], fixed_sl=node["fixed_sl"],
        trail_pct_pct=trail_pct_pct, entry_timing=node.get("entry_timing", "close"), prep=p,
    )
    return trades


def busy_calendar(primary_trades, secondary_timestamps):
    """Per-secondary-bar: is the primary already holding a position (busy), and
    is this bar the exact entry bar of a NEW primary trade (force_exit)?"""
    n = len(secondary_timestamps)
    busy = np.zeros(n, dtype=bool)
    force_exit = np.zeros(n, dtype=bool)
    ts_index = pd.Index(secondary_timestamps)
    for t in primary_trades:
        entry_ts, exit_ts = t["Entry Time"], t["Exit Time"]
        mask = (ts_index >= entry_ts) & (ts_index <= exit_ts)
        busy |= mask
        entry_pos = ts_index.searchsorted(entry_ts)
        if entry_pos < n and ts_index[entry_pos] == entry_ts:
            force_exit[entry_pos] = True
    return busy, force_exit


def simulate_trail_constrained(prices, highs, lows, opens, hours, daily_idx, sma_arr, std_arr,
                                trend_arr, has_trend, take_profit, stop_loss, max_hours_to_hold,
                                trail_pct, target_h0, target_h1, z_thresh, busy, force_exit,
                                open_check_entry_timing=False):
    """Pure-Python mirror of backtester._simulate_trail, gated by `busy`
    (blocks new entries) and `force_exit` (closes an open position at that
    bar's Open, tagged FORCED, before the normal SL/TP/TIME checks run)."""
    trades = []
    in_trade = False
    trailing = False
    entry_price = stop_price = tp_price = peak = 0.0
    entry_bar = held = 0
    n = len(prices)

    for i in range(n):
        cp, op, high, low = prices[i], opens[i], highs[i], lows[i]

        if in_trade:
            held += 1

            if force_exit[i]:
                exit_px = op
                pc = (exit_px - entry_price) / entry_price
                trades.append({
                    "Entry Time": None, "entry_bar": entry_bar, "exit_bar": i,
                    "Entry Price": entry_price, "Exit Price": exit_px,
                    "Hours Held": held, "Result": FORCED, "Return": pc,
                })
                in_trade = False
                trailing = False
                continue

            if trailing:
                trail_stop_gap = peak * (1.0 - trail_pct)
                if op <= trail_stop_gap:
                    pc = (op - entry_price) / entry_price
                    trades.append({"entry_bar": entry_bar, "exit_bar": i, "Entry Price": entry_price,
                                    "Exit Price": op, "Hours Held": held,
                                    "Result": WIN if pc > 0 else LOSS, "Return": pc})
                    in_trade = trailing = False
                    continue
                if high > peak:
                    peak = high
                trail_stop = peak * (1.0 - trail_pct)
                if low <= trail_stop or held >= max_hours_to_hold:
                    exit_px = trail_stop if low <= trail_stop else cp
                    pc = (exit_px - entry_price) / entry_price
                    trades.append({"entry_bar": entry_bar, "exit_bar": i, "Entry Price": entry_price,
                                    "Exit Price": exit_px, "Hours Held": held,
                                    "Result": WIN if pc > 0 else LOSS, "Return": pc})
                    in_trade = trailing = False
                continue

            if op <= stop_price:
                pc = (op - entry_price) / entry_price
                trades.append({"entry_bar": entry_bar, "exit_bar": i, "Entry Price": entry_price,
                                "Exit Price": op, "Hours Held": held, "Result": LOSS, "Return": pc})
                in_trade = False
                continue
            if low <= stop_price:
                pc = (stop_price - entry_price) / entry_price
                trades.append({"entry_bar": entry_bar, "exit_bar": i, "Entry Price": entry_price,
                                "Exit Price": stop_price, "Hours Held": held, "Result": LOSS, "Return": pc})
                in_trade = False
                continue

            if cp >= tp_price:
                trailing = True
                peak = cp
                continue

            if held >= max_hours_to_hold:
                pc = (cp - entry_price) / entry_price
                trades.append({"entry_bar": entry_bar, "exit_bar": i, "Entry Price": entry_price,
                                "Exit Price": cp, "Hours Held": held,
                                "Result": TWIN if pc > 0 else TLOSS, "Return": pc})
                in_trade = False
            continue

        if busy[i]:
            continue  # primary holds a position -- secondary may not enter this bar

        h = hours[i]
        if h != target_h0 and h != target_h1:
            continue
        di = daily_idx[i]
        if di < 0:
            continue
        sma, std = sma_arr[di], std_arr[di]
        if std == 0.0:
            continue
        lower_band = sma - std * z_thresh

        fired = False
        if open_check_entry_timing:
            signal_open = (op <= lower_band) and (op > trend_arr[di]) if has_trend else op <= lower_band
            if signal_open:
                in_trade, trailing = True, False
                entry_price, tp_price, stop_price = op, op * (1 + take_profit), op * (1 - stop_loss)
                entry_bar, held, fired = i, 0, True

        if not fired:
            signal = (cp <= lower_band) and (cp > trend_arr[di]) if has_trend else cp <= lower_band
            if signal:
                in_trade, trailing = True, False
                entry_price, tp_price, stop_price = cp, cp * (1 + take_profit), cp * (1 - stop_loss)
                entry_bar, held = i, 0

    if in_trade:
        cp = prices[n - 1]
        pc = (cp - entry_price) / entry_price
        trades.append({"entry_bar": entry_bar, "exit_bar": n - 1, "Entry Price": entry_price,
                        "Exit Price": cp, "Hours Held": held, "Result": OPEN, "Return": pc})

    return trades


def run_one(secondary, p, window, z, tp, sl_pct, hold, busy, force_exit, entry_timing):
    strat = strategies.TrailingExitZScoreBreakout(window=window, z_score_threshold=z)
    df_daily = None  # indicators already baked into p via prep_inputs by caller
    return simulate_trail_constrained(
        p["prices"], p["highs"], p["lows"], p["opens"], p["hours"], p["daily_idx"],
        p["sma_arr"], p["std_arr"], p["trend_arr"], p["has_trend"],
        take_profit=tp / 100.0, stop_loss=sl_pct / 100.0, max_hours_to_hold=hold,
        trail_pct=sl_pct / 100.0, target_h0=9, target_h1=14, z_thresh=z,
        busy=busy, force_exit=force_exit, open_check_entry_timing=(entry_timing == "open_check"),
    )


def summarize(trades):
    if not trades:
        return {"trades": 0}
    rets = [t["Return"] for t in trades]
    compounded = 1.0
    for r in rets:
        compounded *= (1 + r)
    wins = sum(1 for t in trades if t["Result"] in (WIN, TWIN))
    forced = sum(1 for t in trades if t["Result"] == FORCED)
    return {
        "trades": len(trades),
        "forced_exits": forced,
        "compounded_return_pct": (compounded - 1) * 100,
        "win_rate_pct": wins / len(trades) * 100,
        "mean_return_pct": (sum(rets) / len(rets)) * 100,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--primary", required=True)
    ap.add_argument("--secondary", required=True)
    args = ap.parse_args()

    p_node = get_primary_node(args.primary)
    if p_node is None:
        print(f"No watchlist_id=65 node for {args.primary}")
        return
    primary_trades = primary_real_trades(args.primary, p_node)
    print(f"{args.primary}: {len(primary_trades)} real trades ({p_node['strategy']})")

    df_h = load_hourly(args.secondary)
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])

    entry_timing = "open_check"
    windows = [10, 20]
    zs = [1.0, 1.5, 2.0]
    sls = [1, 2, 3]
    tps = [15, 20, 25, 30]
    holds = [28, 49, 70]

    results = []
    for window in windows:
        strat = strategies.TrailingExitZScoreBreakout(window=window, z_score_threshold=2.0)
        ind = strat.generate_daily_indicators(df_daily)
        p = prep_inputs(df_h, ind)
        busy, force_exit = busy_calendar(primary_trades, p["timestamps"])
        print(f"  window={window}: busy on {busy.sum()}/{len(busy)} bars, "
              f"{force_exit.sum()} forced-exit trigger bars")

        for z in zs:
            # regenerate indicators/prep for this z is unnecessary (SMA/Std don't
            # depend on z), but busy/force_exit are window-independent-timestamp-
            # aligned so this is fine to reuse across z/sl/tp/hold
            for sl_pct in sls:
                for tp in tps:
                    for hold in holds:
                        trades = run_one(args.secondary, p, window, z, tp, sl_pct, hold,
                                          busy, force_exit, entry_timing)
                        s = summarize(trades)
                        s.update({"window": window, "z": z, "sl": sl_pct, "tp": tp, "hold": hold})
                        results.append(s)

    out = pd.DataFrame(results)
    out = out[out["trades"] > 5].sort_values("compounded_return_pct", ascending=False)
    print(f"\nTop 10 constrained-node candidates for {args.secondary} "
          f"(gated on {args.primary}'s real open windows):")
    print(out.head(10).to_string(index=False))
    out_path = Path("output") / f"constrained_{args.primary}_{args.secondary}.csv"
    out.to_csv(out_path, index=False)
    print(f"\n-> {out_path}")


if __name__ == "__main__":
    main()
