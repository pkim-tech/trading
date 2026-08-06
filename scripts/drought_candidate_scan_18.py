"""Batch drought-overlay candidate scan across all 18 v4-universe tickers, extending
the 2026-08-07 SOXL/AGQ/KORU/DPST deep-dive methodology to the full universe.

For each ticker: pull its real config (watch_list node if it has one on watchlist 65,
else the best cliff-safe-else-best-any v5 node from backtest_cache via
three_layer_summary.best_v5_node -- same source used throughout today's session), scan
confirm_days 1-15 using the ticker's OWN core sl/arm/trail (no re-tuning), find any
positive candidate day that isn't an isolated one-day spike (both immediate day-1/day+1
neighbors also positive -- the same "plateau not spike" bar SOXL's real signal had to
clear), then run the vol-gate sweep + chronological fit/test out-of-sample split on each
such candidate. A ticker only "passes" if at least one (confirm_days, vol_gate) cell is
positive on BOTH the fit and test halves independently -- the same bar SOXL cleared and
everything else failed today.

Usage: .venv/bin/python scripts/drought_candidate_scan_18.py [--csv]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.drought_detection_test import load_nodes
from scripts.drought_overlay_test import get_trades_and_bars, find_drought_windows, simulate_overlay
from scripts.drought_overlay_sweep import VOL_GATE_GRID, get_ivol_series, _entry_vol_pctile
from scripts.three_layer_summary import V4_TICKERS, best_v5_node, UNIVERSE_DB, ENTRY_TIMING

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def get_node_for_ticker(conn, ticker):
    """Prefer the real deployed watchlist-65 node; fall back to the best v5
    backtest_cache node (same source three_layer_summary.py uses) for tickers not on
    that watchlist."""
    wl_nodes = load_nodes(65, [ticker])
    if wl_nodes:
        return wl_nodes[0]
    best = best_v5_node(conn, ticker)
    if best is None:
        return None
    return {
        "ticker": ticker, "strategy": best["strategy"], "window": best["window"], "z": best["z"],
        "arm_pct": best["arm_pct"], "fixed_sl": best["fixed_sl"], "trail_buy_pct": best["trail_buy_pct"] or 0.0,
        "trail_sell_pct": best["trail_sell_pct"], "max_hold_hours": best["max_hold_hours"],
        "entry_timing": ENTRY_TIMING,
    }


def compounded(rets):
    return float(np.prod([1 + r for r in rets]) - 1) if rets else None


def gate(windows, df_h, ivol_series, vg):
    if vg is None:
        return windows
    out = []
    for ei, ge in windows:
        et = df_h.index[ei + 1] if ei + 1 < len(df_h) else df_h.index[ei]
        p = _entry_vol_pctile(et, ivol_series)
        if p is not None and p < vg:
            out.append((ei, ge))
    return out


def day_scan(trades, df_h, sl, arm, trail, max_day=15):
    results = {}
    for cd in range(1, max_day + 1):
        windows = find_drought_windows(trades, df_h, cd)
        if not windows:
            results[cd] = None
            continue
        rets = [simulate_overlay(df_h, ei, ge, sl, arm, trail)["ret"] for ei, ge in windows]
        results[cd] = (compounded(rets), len(rets))
    return results


_OFF_GRID = object()  # sentinel: distinct from results[cd]=None (in-range, no windows)


def find_plateau_candidates(results):
    """Days where the day itself AND both neighbors are positive -- same bar as
    SOXL's real signal (days 3-5), rejects isolated one-day spikes.

    A neighbor day genuinely OUTSIDE the scanned range (cd=1's left, cd=max_day's
    right) is a free pass, by construction -- there's no day 0 to check. A neighbor
    day INSIDE the range with zero drought windows (results[cd]=None) is real
    no-data, not a free pass -- these were previously indistinguishable
    (results.get() returns None for both), so a day flanked by two no-data days could
    pass as a "plateau" on a single data point. Caught 2026-08-08."""
    candidates = []
    for cd, r in results.items():
        if r is None or r[0] is None or r[0] < 0:
            continue
        left, right = results.get(cd - 1, _OFF_GRID), results.get(cd + 1, _OFF_GRID)
        left_ok = left is _OFF_GRID or (left is not None and left[0] is not None and left[0] >= 0)
        right_ok = right is _OFF_GRID or (right is not None and right[0] is not None and right[0] >= 0)
        if left_ok and right_ok:
            candidates.append(cd)
    return candidates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(UNIVERSE_DB)
    rows = []
    for ticker in V4_TICKERS:
        node = get_node_for_ticker(conn, ticker)
        if node is None:
            print(f"{ticker}: no config found, skipped")
            continue
        try:
            trades, df_h = get_trades_and_bars(node)
        except Exception as e:
            print(f"{ticker}: failed to load trades ({e})")
            continue
        if len(trades) < 2:
            print(f"{ticker}: too few core trades, skipped")
            continue
        sl, arm, trail = node["fixed_sl"], node["arm_pct"], node["trail_sell_pct"]

        results = day_scan(trades, df_h, sl, arm, trail)
        candidates = find_plateau_candidates(results)
        if not candidates:
            print(f"{ticker}: no plateau candidates (default={results.get(10)})")
            rows.append({"ticker": ticker, "passed": False, "best_cd": None, "best_vg": None,
                         "fit_pct": None, "test_pct": None})
            continue

        ivol_series = get_ivol_series(ticker)
        midpoint = df_h.index[0] + (df_h.index[-1] - df_h.index[0]) / 2

        best_pass = None
        for cd in candidates:
            windows = find_drought_windows(trades, df_h, cd)
            for vg in VOL_GATE_GRID:
                gw = gate(windows, df_h, ivol_series, vg)
                fit_w = [w for w in gw if df_h.index[w[0]] < midpoint]
                test_w = [w for w in gw if df_h.index[w[0]] >= midpoint]
                if len(fit_w) < 3 or len(test_w) < 3:
                    continue
                fc = compounded([simulate_overlay(df_h, ei, ge, sl, arm, trail)["ret"] for ei, ge in fit_w])
                tc = compounded([simulate_overlay(df_h, ei, ge, sl, arm, trail)["ret"] for ei, ge in test_w])
                if fc is not None and tc is not None and fc >= 0 and tc >= 0:
                    if best_pass is None or (fc + tc) > (best_pass[3] + best_pass[4]):
                        best_pass = (cd, vg, len(fit_w), fc, tc, len(test_w))

        if best_pass:
            cd, vg, fn, fc, tc, tn = best_pass
            print(f"{ticker}: PASS  cd={cd} vol_gate={vg}  fit(n={fn})={fc*100:+.1f}%  test(n={tn})={tc*100:+.1f}%")
            rows.append({"ticker": ticker, "passed": True, "best_cd": cd, "best_vg": vg,
                         "fit_pct": fc * 100, "test_pct": tc * 100})
        else:
            print(f"{ticker}: no candidate survives fit+test split (had {len(candidates)} plateau days: {candidates})")
            rows.append({"ticker": ticker, "passed": False, "best_cd": None, "best_vg": None,
                         "fit_pct": None, "test_pct": None})

    df = pd.DataFrame(rows)
    print("\n" + df.to_string(index=False))
    if args.csv:
        OUTPUT_DIR.mkdir(exist_ok=True)
        df.to_csv(OUTPUT_DIR / "drought_candidate_scan_18.csv", index=False)
        print(f"\nWrote {OUTPUT_DIR / 'drought_candidate_scan_18.csv'}")


if __name__ == "__main__":
    main()
