"""
Verifies the v4 kernel's three fill-optimism resolutions (possible/pessimistic/certain,
see backtester._simulate_trail_both) for a ticker's live watch_list node:

  1. possible >= pessimistic on the aggregate compounded return (the one guaranteed
     ordering between the three — see the kernel's own docstring for why certain
     isn't bound either direction).
  2. possible matches the exact matching v3.x backtest_cache row for the SAME node
     (same axis_tp/stop_loss/trail_buy_pct/trail_sell_pct/window/hold/z — a query
     that doesn't filter axis_tp will silently match a grab-bag of different TP
     values and look like a false mismatch, see 2026-07-14 session).
  3. If (2) doesn't match exactly, truncates today's cached hourly data back to the
     v3.x row's run_timestamp and reruns — if that reproduces the old number exactly,
     the gap is just new price data accumulating since the old row was computed, not
     a kernel regression. Confirms 'possible' is byte-for-byte unchanged from before
     the v4 pass.

Usage: .venv/bin/python scripts/verify_v4_fill_bounds.py SOXL [KORU ...]
"""
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd
import strategies
from backtester import prep_inputs, run_backtest_v110
from scripts.export_trades import get_node, load_hourly

DB_PATH = Path(__file__).resolve().parent.parent / "cache" / "research" / "trading_universe.db"


def _compounded(trades):
    closed = [t for t in trades if t['Result'] in ('WIN', 'LOSS', 'TWIN', 'TLOSS')]
    if not closed:
        return 0.0, 0
    comp = ((pd.Series([t['Return'] for t in closed]) + 1).prod() - 1) * 100
    return float(comp), len(closed)


def _run_kernel(df_hourly, node, entry_timing='close', return_bounds=False):
    df_daily = df_hourly.resample("D").last().dropna(subset=["Close"])
    strat = strategies.TrailingBothZScoreBreakout(window=node["window"], z_score_threshold=node["z_score_threshold"])
    ind = strat.generate_daily_indicators(df_daily)
    return run_backtest_v110(
        df_hourly, ind, "verify", target_hours=(9, 14),
        take_profit=node["arm_sell_pct"] / 100.0, stop_loss=node["fixed_sl"] / 100.0,
        max_hours_to_hold=node["max_hold_hours"], z_score_threshold=node["z_score_threshold"],
        trail_buy_pct=node["trail_buy_pct"] / 100.0, trail_pct=node["trail_sell_pct"] / 100.0,
        entry_timing=entry_timing, return_bounds=return_bounds,
    )


def verify_ticker(ticker):
    node = get_node(ticker)
    df_h = load_hourly(ticker)
    print(f"\n=== {ticker} — live node: {node} ===")

    possible, pessimistic, certain = _run_kernel(df_h, node, return_bounds=True)
    c_pos, n_pos = _compounded(possible)
    c_pess, n_pess = _compounded(pessimistic)
    c_cert, n_cert = _compounded(certain)
    print(f"  possible:    n={n_pos:<4} compounded={c_pos:>10.2f}%")
    print(f"  pessimistic: n={n_pess:<4} compounded={c_pess:>10.2f}%")
    print(f"  certain:     n={n_cert:<4} compounded={c_cert:>10.2f}%")

    ok_bound = c_pos >= c_pess
    print(f"  [{'OK' if ok_bound else 'FAIL'}] possible >= pessimistic")

    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT version, strategy_return, trades, run_timestamp FROM backtest_cache
        WHERE ticker=? AND strategy='TrailingBothZScoreBreakout'
          AND window=? AND max_hold_hours=? AND fixed_sl=? AND trail_buy_pct=? AND trail_sell_pct=?
          AND z_score_threshold=? AND axis_tp=?
          AND version LIKE 'v3.%'
        ORDER BY version DESC LIMIT 1
    """, (ticker, node["window"], node["max_hold_hours"], node["fixed_sl"], node["trail_buy_pct"],
          node["trail_sell_pct"], node["z_score_threshold"], node["arm_sell_pct"])).fetchone()
    conn.close()

    if not row:
        print("  [SKIP] no exact-match v3.x row on file for this node")
        return ok_bound

    old_version, old_return, old_trades, old_ts = row
    if abs(old_return - c_pos) < 1e-6 and old_trades == n_pos:
        print(f"  [OK] exact match vs {old_version} ({old_return:.4f}%, {old_trades} trades)")
        return ok_bound

    print(f"  [DIFF] {old_version} on file: {old_return:.4f}% ({old_trades} trades) vs new possible "
          f"{c_pos:.4f}% ({n_pos} trades) — checking if this is just newer price data...")
    df_trunc = df_h[df_h.index < old_ts]
    trunc_trades = _run_kernel(df_trunc, node)
    c_trunc, n_trunc = _compounded(trunc_trades)
    match = abs(old_return - c_trunc) < 1e-4 and old_trades == n_trunc
    print(f"  Truncated-to-{old_ts} rerun: n={n_trunc} compounded={c_trunc:.4f}% "
          f"— {'[OK] matches, data drift confirmed as sole cause' if match else '[FAIL] does NOT match — possible kernel regression, investigate'}")
    return ok_bound and match


if __name__ == "__main__":
    tickers = sys.argv[1:] or ["SOXL"]
    results = {t: verify_ticker(t) for t in tickers}
    print("\n=== Summary ===")
    for t, ok in results.items():
        print(f"  {t}: {'PASS' if ok else 'FAIL'}")
    sys.exit(0 if all(results.values()) else 1)
