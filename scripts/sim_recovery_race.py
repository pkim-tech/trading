"""General version of sim_soxl_2025_recovery_race.py -- buy-and-hold vs real
v5(+drought where the real node has it enabled) strategy, hourly kernel vs
daily-resampled-bars-same-kernel, over a ticker's own real peak/trough/
recovery window, extended to today. Reuses the real kernel functions
(_simulate_trail / _simulate_trail_both) and the real drought-overlay
functions (find_drought_windows/simulate_overlay) -- not reimplemented.

Usage: .venv/bin/python scripts/sim_recovery_race.py TICKER
Reads the ticker's real state='live' watch_list config directly -- no
hand-typed params, so this can't drift from what's actually live.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import signals_db
import signals_compute as compute
from backtester import prep_inputs, _simulate_trail_both, _simulate_trail
from scripts.drought_overlay_test import find_drought_windows, simulate_overlay, build_indicators

TARGET_H0, TARGET_H1 = 9, 14


def real_node(ticker):
    nodes = [n for n in signals_db.get_watchlist() if n['ticker'] == ticker and n['state'] == 'live']
    if not nodes:
        raise SystemExit(f"no state='live' node for {ticker}")
    if len(nodes) > 1:
        print(f"warning: {len(nodes)} live nodes for {ticker}, using the first ({nodes[0]['account']})")
    return nodes[0]


def find_window(ticker, start='2024-11-01', end='2025-12-31'):
    df, daily = compute._load_cache(ticker)
    daily = daily['Close'].dropna()
    seg = daily[start:end]
    roll_max = seg.cummax()
    dd = (seg - roll_max) / roll_max
    trough = dd.idxmin()
    peak = seg[:trough].idxmax()
    after = daily[trough:]
    rec = after[after >= seg[peak]]
    recovery = rec.index[0] if not rec.empty else None
    return peak, trough, recovery


def run_kernel(node, df_h, df_daily, open_check):
    ind = build_indicators(node['strategy'], df_daily, node['window'])
    p = prep_inputs(df_h, ind)
    z = float(node['z_score_threshold'])
    fixed_sl = float(node['fixed_sl']) / 100.0
    max_hold = int(node['max_hold_hours'])
    if node['strategy'] == 'TrailingBothZScoreBreakout':
        arm = float(node['arm_sell_pct']) / 100.0
        trail_buy = float(node['trail_buy_pct']) / 100.0
        trail_sell = float(node['trail_sell_pct']) / 100.0
        ei, xi, ep, xp, held, res, ret = _simulate_trail_both(
            p['prices'], p['highs'], p['lows'], p['hours'], p['daily_idx'],
            p['sma_arr'], p['std_arr'], p['trend_arr'], p['has_trend'],
            arm, fixed_sl, max_hold, trail_buy, trail_sell,
            TARGET_H0, TARGET_H1, z, p['opens'], open_check,
        )[:7]
    elif node['strategy'] == 'TrailingExitZScoreBreakout':
        take_profit = float(node['take_profit']) / 100.0
        trail_sell = float(node['trail_sell_pct']) / 100.0
        ei, xi, ep, xp, held, res, ret = _simulate_trail(
            p['prices'], p['highs'], p['lows'], p['opens'], p['hours'], p['daily_idx'],
            p['sma_arr'], p['std_arr'], p['trend_arr'], p['has_trend'],
            take_profit, fixed_sl, max_hold, trail_sell,
            TARGET_H0, TARGET_H1, z, open_check,
        )
    else:
        raise ValueError(f"unhandled strategy {node['strategy']}")
    trades = []
    for k in range(len(ei)):
        trades.append({
            'entry_i': int(ei[k]), 'exit_i': int(xi[k]),
            'entry_time': p['timestamps'][ei[k]], 'exit_time': p['timestamps'][xi[k]],
            'ret': float(ret[k]), 'signal_i': int(ei[k]),
        })
    return trades


def apply_drought(node, trades, df_h):
    if not node.get('drought_overlay_enabled'):
        return list(trades)
    confirm_days = int(node['drought_confirm_days'])
    fixed_sl = float(node['fixed_sl'])
    arm = float(node['arm_sell_pct']) if node.get('arm_sell_pct') is not None else float(node['take_profit'])
    trail_sell = float(node['trail_sell_pct'])
    drought_trades = []
    for entry_i, gap_end in find_drought_windows(trades, df_h, confirm_days):
        result = simulate_overlay(df_h, entry_i, gap_end, fixed_sl, arm, trail_sell)
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


def equity_curve(trades, start_capital, window_start, window_end):
    trades = sorted([t for t in trades if window_start <= t['entry_time'] <= window_end],
                     key=lambda t: t['entry_time'])
    equity = start_capital
    points = [(window_start, equity)]
    for t in trades:
        equity *= (1.0 + t['ret'])
        points.append((t['exit_time'], equity))
    points.append((window_end, equity))
    return points, trades


def to_daily_series(points, start, end):
    s = pd.Series({ts: eq for ts, eq in points}).sort_index()
    idx = pd.date_range(start, end, freq='D')
    return s.reindex(s.index.union(idx)).ffill().reindex(idx)


def recovery_date(points, target):
    dropped_below = False
    for ts, eq in points:
        if eq < target:
            dropped_below = True
        elif dropped_below and eq >= target:
            return ts, eq
    return None, points[-1][1]


def leader_changes(series_a, series_b):
    leader = None
    changes = []
    for ts in series_a.index:
        a, b = series_a[ts], series_b[ts]
        if pd.isna(a) or pd.isna(b):
            continue
        cur = 'A' if a >= b else 'B'
        if cur != leader:
            changes.append((ts, cur, a, b))
            leader = cur
    return changes


def main():
    ticker = sys.argv[1]
    node = real_node(ticker)
    peak, trough, native_recovery = find_window(ticker)
    end = pd.Timestamp.now().normalize()
    start_capital = 10_000.0

    df_h, df_daily = compute._load_cache(ticker)
    start_price = float(df_daily['Close'].asof(peak))

    print(f"=== {ticker} — real node id={node['id']} account={node['account']} strategy={node['strategy']} ===")
    print(f"window: peak {peak.date()} (${start_price:.2f}) -> trough {trough.date()} -> "
          f"buy-hold recovers {native_recovery.date() if native_recovery is not None else 'N/A'}")
    print(f"config: window={node['window']} z={node['z_score_threshold']} fixed_sl={node['fixed_sl']}% "
          f"max_hold_hours={node['max_hold_hours']} entry_timing={node['entry_timing']} "
          f"drought={'on (confirm_days=' + str(node['drought_confirm_days']) + ')' if node.get('drought_overlay_enabled') else 'off'}")
    print(f"starting capital: ${start_capital:,.0f} at {peak.date()}\n")

    bh_prices = df_daily['Close'][peak:end]
    bh_points = [(ts, start_capital * (p / start_price)) for ts, p in bh_prices.items()]

    open_check = node['entry_timing'] == 'open_check'
    trades_hourly = run_kernel(node, df_h, df_daily, open_check)
    combined_hourly = apply_drought(node, trades_hourly, df_h)
    points_hourly, _ = equity_curve(combined_hourly, start_capital, peak, end)

    df_daily_bars = df_h.resample('D').agg(
        {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'}
    ).dropna(subset=['Close'])
    df_daily_bars.index = df_daily_bars.index.normalize() + pd.Timedelta(hours=TARGET_H0)
    p_daily_probe = prep_inputs(df_daily_bars, build_indicators(node['strategy'], df_daily, node['window']))
    p_daily_probe['hours'] = np.full(len(df_daily_bars), TARGET_H0, dtype=np.int64)
    trades_daily = run_kernel(node, df_daily_bars, df_daily, open_check=False)
    combined_daily = apply_drought(node, trades_daily, df_daily_bars)
    points_daily, _ = equity_curve(combined_daily, start_capital, peak, end)

    def _fmt(label, points):
        rec_ts, _ = recovery_date(points, start_capital)
        final_eq = points[-1][1]
        min_eq = min(e for _, e in points)
        rec_str = rec_ts.date().isoformat() if rec_ts is not None else f"NOT recovered by {end.date()}"
        days = (rec_ts - peak).days if rec_ts is not None else None
        print(f"{label:32s} min=${min_eq:>10,.0f} ({(min_eq/start_capital-1)*100:+6.1f}%)  "
              f"final=${final_eq:>10,.0f} ({(final_eq/start_capital-1)*100:+6.1f}%)  "
              f"recovered: {rec_str}" + (f"  ({days}d from peak)" if days else ""))

    bh_recovery_ts, _ = recovery_date(bh_points, start_capital)
    _fmt("Buy-and-hold", bh_points)
    _fmt("v5 (HOURLY kernel)", points_hourly)
    _fmt("v5 (DAILY bars, same kernel)", points_daily)

    bh_daily = to_daily_series(bh_points, peak, end)
    hourly_daily = to_daily_series(points_hourly, peak, end)
    daily_daily = to_daily_series(points_daily, peak, end)

    print(f"\nlead changes, HOURLY vs buy-hold:")
    for ts, who, a, b in leader_changes(hourly_daily, bh_daily):
        print(f"  {ts.date()} {'strategy' if who=='A' else 'buyhold':10s} strat=${a:,.0f} bh=${b:,.0f}")


if __name__ == '__main__':
    main()
