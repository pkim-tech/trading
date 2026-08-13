"""One-off analysis: does SOXL's real v5+drought strategy recover faster than
buy-and-hold through the real 2025-01-22 -> 2025-04-08 -> 2025-10-01
peak/trough/recovery cycle (the leveraged version of the documented real
SPY -19% Feb-May 2025 correction), or does it lag an abrupt V-shaped rally?

Three legs, same window, same starting capital:
  1. buy-and-hold SOXL
  2. v5 core + drought overlay, REAL hourly kernel (same code the live daemon
     and every other v5 backtest number on file uses)
  3. v5 core + drought overlay, SAME kernel code, fed DAILY-resampled bars
     (one evaluation per day at a fixed target-hour instead of ~7/day) --
     isolates bar-granularity/checking-frequency as the only variable.

Real SOXL v5 node config (wl_id=92, ira): window=10, z=1.0, trail_buy_pct=3.0,
fixed_sl=2.0, trail_sell_pct=1.0, arm_sell_pct=30.0, entry_timing=open_check,
max_hold_hours=70. Drought: confirm_days=3, vol_gate=0.4.

Caveat, stated not hidden: max_hold_hours=70 means 70 HOURLY bars in the
hourly run (~10 trading days) but 70 DAILY bars in the daily run (~70
trading days, ~3.5 months) -- feeding the same numeric config to a coarser
bar grid changes what it means. Reported as-is; not rescaled, since the
open question is "checking frequency," and silently rescaling would hide
that real a consequence of running strategy logic off daily bars.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import signals_compute as compute
from backtester import prep_inputs, _simulate_trail_both
from strategies import TrailingBothZScoreBreakout
from scripts.drought_overlay_test import find_drought_windows, simulate_overlay, build_indicators

PEAK = pd.Timestamp('2025-01-22')
TROUGH = pd.Timestamp('2025-04-08')
RECOVERY = pd.Timestamp('2025-10-01')
END = pd.Timestamp.now().normalize()

WINDOW = 10
Z = 1.0
TRAIL_BUY_PCT = 0.03
FIXED_SL = 0.02
TRAIL_SELL_PCT = 0.01
ARM_SELL_PCT = 0.30
MAX_HOLD_HOURS = 70
CONFIRM_DAYS = 3
TARGET_H0, TARGET_H1 = 9, 14


def _annotated_trades_from_kernel(p, open_check):
    ei, xi, ep, xp, held, res, ret = _simulate_trail_both(
        p['prices'], p['highs'], p['lows'], p['hours'], p['daily_idx'],
        p['sma_arr'], p['std_arr'], p['trend_arr'], p['has_trend'],
        ARM_SELL_PCT, FIXED_SL, MAX_HOLD_HOURS, TRAIL_BUY_PCT, TRAIL_SELL_PCT,
        TARGET_H0, TARGET_H1, Z, p['opens'], open_check,
    )[:7]  # 'possible' resolution columns only
    trades = []
    for k in range(len(ei)):
        trades.append({
            'entry_i': int(ei[k]), 'exit_i': int(xi[k]),
            'entry_time': p['timestamps'][ei[k]], 'exit_time': p['timestamps'][xi[k]],
            'entry_price': float(ep[k]), 'exit_price': float(xp[k]),
            'ret': float(ret[k]), 'signal_i': int(ei[k]), 'result': int(res[k]),
        })
    return trades


def equity_curve(trades, start_capital, window_start, window_end):
    """Compounds start_capital through trades whose entry falls in
    [window_start, window_end], flat (uninvested) between trades."""
    trades = sorted([t for t in trades if window_start <= t['entry_time'] <= window_end],
                     key=lambda t: t['entry_time'])
    equity = start_capital
    points = [(window_start, equity)]
    for t in trades:
        equity *= (1.0 + t['ret'])
        points.append((t['exit_time'], equity))
    points.append((window_end, equity))
    return points, trades


def apply_drought(trades, df_h, start_equity_points):
    """Layers drought-overlay trades (from real gaps between core trades) onto
    the existing equity curve, same compounding basis."""
    drought_trades = []
    for entry_i, gap_end in find_drought_windows(trades, df_h, CONFIRM_DAYS):
        result = simulate_overlay(df_h, entry_i, gap_end, FIXED_SL * 100, ARM_SELL_PCT * 100, TRAIL_SELL_PCT * 100)
        drought_trades.append({
            'entry_time': df_h.index[entry_i + 1], 'exit_time': df_h.index[result['exit_i']],
            'ret': result['ret'],
        })
    combined = sorted(
        [{'entry_time': t['entry_time'], 'exit_time': t['exit_time'], 'ret': t['ret']} for t in trades]
        + drought_trades,
        key=lambda t: t['entry_time'],
    )
    return combined


def to_daily_series(points, start, end):
    """Step-function equity (flat between trade events) reindexed to a daily
    grid, for comparing two curves with different native event cadences."""
    s = pd.Series({ts: eq for ts, eq in points}).sort_index()
    idx = pd.date_range(start, end, freq='D')
    return s.reindex(s.index.union(idx)).ffill().reindex(idx)


def crossover_date(series_a, series_b, label_a, label_b):
    """First date A >= B after having been < B (skips the trivial equal start)."""
    below = False
    for ts in series_a.index:
        a, b = series_a[ts], series_b[ts]
        if pd.isna(a) or pd.isna(b):
            continue
        if a < b:
            below = True
        elif below and a >= b:
            return ts
    return None


def recovery_date(points, target):
    """First point >= target AFTER equity has actually dropped below target --
    the initial point trivially equals target (start of the window), so
    searching from index 0 would report the start date as 'recovered'."""
    dropped_below = False
    for ts, eq in points:
        if eq < target:
            dropped_below = True
        elif dropped_below and eq >= target:
            return ts, eq
    return None, points[-1][1]


def main():
    df_h, df_daily = compute._load_cache('SOXL')
    start_price = float(df_daily['Close'].asof(PEAK))
    start_capital = 10_000.0

    # ---------- 1. Buy-and-hold ----------
    bh_prices = df_daily['Close'][PEAK:END]
    bh_points = [(ts, start_capital * (p / start_price)) for ts, p in bh_prices.items()]
    bh_recovery_ts, _ = recovery_date(bh_points, start_capital)

    # ---------- 2. v5 + drought, HOURLY kernel ----------
    ind = build_indicators('TrailingBothZScoreBreakout', df_daily, WINDOW)
    p_hourly = prep_inputs(df_h, ind)
    trades_hourly = _annotated_trades_from_kernel(p_hourly, open_check=True)
    combined_hourly = apply_drought(trades_hourly, df_h, None)
    points_hourly, _ = equity_curve(combined_hourly, start_capital, PEAK, END)
    hourly_recovery_ts, _ = recovery_date(points_hourly, start_capital)

    # ---------- 3. v5 + drought, DAILY-resampled bars, same kernel ----------
    df_daily_bars = df_h.resample('D').agg(
        {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'}
    ).dropna(subset=['Close'])
    df_daily_bars.index = df_daily_bars.index.normalize() + pd.Timedelta(hours=TARGET_H0)
    ind_d = build_indicators('TrailingBothZScoreBreakout', df_daily, WINDOW)
    p_daily = prep_inputs(df_daily_bars, ind_d)
    p_daily['hours'] = np.full(len(df_daily_bars), TARGET_H0, dtype=np.int64)
    trades_daily = _annotated_trades_from_kernel(p_daily, open_check=False)
    combined_daily = apply_drought(trades_daily, df_daily_bars, None)
    points_daily, _ = equity_curve(combined_daily, start_capital, PEAK, END)
    daily_recovery_ts, _ = recovery_date(points_daily, start_capital)

    # ---------- Report ----------
    print(f"Window: peak {PEAK.date()} (${start_price:.2f}) -> trough {TROUGH.date()} -> "
          f"buy-hold recovers {RECOVERY.date()}")
    print(f"Starting capital: ${start_capital:,.0f} at {PEAK.date()}\n")

    def _fmt(label, points, rec_ts):
        final_eq = points[-1][1]
        min_eq = min(e for _, e in points)
        rec_str = rec_ts.date().isoformat() if rec_ts is not None else "NOT recovered by " + str(END.date())
        days = (rec_ts - PEAK).days if rec_ts is not None else None
        print(f"{label:32s} min=${min_eq:>10,.0f} ({(min_eq/start_capital-1)*100:+6.1f}%)  "
              f"final=${final_eq:>10,.0f} ({(final_eq/start_capital-1)*100:+6.1f}%)  "
              f"recovered: {rec_str}" + (f"  ({days}d from peak)" if days else ""))

    _fmt("Buy-and-hold", bh_points, bh_recovery_ts)
    _fmt("v5+drought (HOURLY kernel)", points_hourly, hourly_recovery_ts)
    _fmt("v5+drought (DAILY bars, same kernel)", points_daily, daily_recovery_ts)

    print(f"\nTrade counts in window: hourly={len(combined_hourly)}  daily={len(combined_daily)}")

    bh_daily = to_daily_series(bh_points, PEAK, END)
    hourly_daily = to_daily_series(points_hourly, PEAK, END)
    daily_daily = to_daily_series(points_daily, PEAK, END)

    print("\n--- Crossover vs buy-and-hold (first date A's equity overtakes buy-hold's, after trailing it) ---")
    for label, series in [("v5+drought HOURLY", hourly_daily), ("v5+drought DAILY-bars", daily_daily)]:
        xo = crossover_date(series, bh_daily, label, "buy-and-hold")
        if xo is not None:
            print(f"{label:28s} overtakes buy-and-hold on {xo.date()}  "
                  f"(strategy=${series[xo]:,.0f} vs buy-hold=${bh_daily[xo]:,.0f})")
        else:
            print(f"{label:28s} never overtakes buy-and-hold through {END.date()} "
                  f"(strategy=${series[END]:,.0f} vs buy-hold=${bh_daily[END]:,.0f})")


if __name__ == '__main__':
    main()
