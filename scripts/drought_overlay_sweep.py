"""
Real grid search over the drought-overlay's risk-management parameters, following this
project's established sweep discipline (docs/design.md's "SOXL drought-overlay parameter
sweep" section) -- fixed_sl%, arm_pct%, trail_sell_pct%, and confirm_days as 4 INDEPENDENT
axes, not the coarse lockstep manual sweep (`docs/research_log.md`'s 2026-08-04 very late
entry) that moved SL/trail together and never isolated which axis actually mattered.

Reuses drought_overlay_test.py's real building blocks directly (get_trades_and_bars,
find_drought_windows, simulate_overlay) rather than reimplementing any of them -- the
entry/exit mechanics are the same TrailingBothZScoreBreakout arm-then-trail state machine
already proven out there; this script only adds the grid loop, pooling, and cliff-safety
screen around it.

Scope, decided 2026-08-05: the 10 v5 watchlist tickers only (AGQ/DPST/GDXU/HIBL/KORU/NUGT/
SOXL/UDOW/USD/YANG) -- NOT the "big 6" broad-index comparison group (SPY/SSO/UPRO/QQQ/QLD/
TQQQ) from the 2026-08-04 research finding, since those aren't on any watchlist and would
need a separate backtest_cache lookup path. That comparison is a follow-up, not built here.

Cliff-safety, decided 2026-08-05: grid-neighbor robustness (this project's standard
island/cliff-safety convention -- does a candidate cell's immediate neighbors along each
axis hold up, not just the best cell in isolation), NOT the MIN(possible, pessimistic,
certain) intrabar-fill-ambiguity resolution the core backtester kernel uses -- that would be
a much bigger addition to simulate_overlay itself (currently single-resolution) and wasn't
what this sweep needed to answer the real question raised by the manual sweep (SOXL's result
being 2-of-13-trades fragile is a small-sample/parameter-instability problem, exactly what
grid-neighbor robustness catches).

confirm_days is swept as a real 4th axis (decided 2026-08-05, not held constant) -- droughts/
entry points depend on confirm_days, so windows are recomputed once per (ticker, confirm_days)
pair via find_drought_windows and reused across every (sl, arm, trail) combo at that
confirm_days, rather than recomputed per grid cell.

--big6 mode (added 2026-08-05, closing the follow-up (2) noted in docs/research_log.md's
2026-08-04 (very late) entry): SPY/SSO/UPRO/QQQ/QLD/TQQQ aren't on any watch_list, so their
node config is pulled directly from backtest_cache instead -- the same best-cliff-safe-else-
best-any selection scripts/campaign_comparison_table.py already uses (winner picked across
both v5 strategies at fixed_sl in {1,2,3}, entry_timing='open_check', the standard v5 grid --
confirmed present for all 6 tickers before use). "big 6" is 3 leverage families, not 6
independent ideas: SPY/SSO/UPRO (S&P 500, 1x/2x/3x) and QQQ/QLD/TQQQ (Nasdaq-100, 1x/2x/3x) --
labeling only, each still gets its own independent overlay sweep run.

Usage: .venv/bin/python scripts/drought_overlay_sweep.py [--tickers ...] [--watchlist-id 65]
       [--top-n 15] [--csv]
       .venv/bin/python scripts/drought_overlay_sweep.py --big6 [--top-n 15] [--csv]
"""
import argparse
import sqlite3
import sys
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.drought_detection_test import load_nodes
from scripts.drought_overlay_test import find_drought_windows, get_trades_and_bars, simulate_overlay

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
UNIVERSE_DB = Path(__file__).resolve().parent.parent / "cache" / "research" / "trading_universe.db"

BIG6_TICKERS = ["SPY", "SSO", "UPRO", "QQQ", "QLD", "TQQQ"]
BIG6_FIXED_SLS = [1, 2, 3]
BIG6_STRATEGIES = ["TrailingBothZScoreBreakout", "TrailingExitZScoreBreakout"]
BIG6_VERSION = "v5"
BIG6_ENTRY_TIMING = "open_check"
BIG6_CLIFF_RADIUS = 2

SL_GRID = [1, 2, 3, 5, 7, 10, 15, 18]
ARM_GRID = [5, 10, 15, 20, 30, 40]
TRAIL_GRID = [1, 2, 3, 5, 7, 10, 15]
CONFIRM_DAYS_GRID = [5, 10, 15, 20]


ROBUST_ALPHA_SQL = (
    "MIN(alpha_vs_spy, COALESCE(alpha_vs_spy_pessimistic, alpha_vs_spy), "
    "COALESCE(alpha_vs_spy_certain, alpha_vs_spy))"
)


def _best_cell(con, ticker, strategy, fixed_sl):
    """Best robust-alpha row for one (ticker, strategy, fixed_sl) at the standard v5/
    open_check grid, plus its worst-neighbor (window/z/arm/trail_sell_pct/max_hold_hours
    +-BIG6_CLIFF_RADIUS) for cliff-safety -- same convention as
    scripts/campaign_comparison_table.py's best_node(). arm_col: TrailingExitZScoreBreakout
    nodes store their arm/take-profit value in the take_profit column, not arm_sell_pct
    (arm_sell_pct is NULL for that strategy in backtest_cache) -- the same real bug found
    and fixed across 5 research scripts in docs/backlog_cache.md's 2026-08-04 late entry;
    reading the wrong column here would silently zero out this axis for TrailingExit."""
    arm_col = "take_profit" if strategy == "TrailingExitZScoreBreakout" else "arm_sell_pct"
    row = con.execute(f"""
        SELECT window, z_score_threshold, {arm_col}, trail_buy_pct,
               trail_sell_pct, max_hold_hours, {ROBUST_ALPHA_SQL} AS robust_alpha, trades, win_rate
        FROM backtest_cache
        WHERE version=? AND ticker=? AND strategy=? AND entry_timing=? AND stop_loss=? AND trades>0
        ORDER BY robust_alpha DESC LIMIT 1
    """, (BIG6_VERSION, ticker, strategy, BIG6_ENTRY_TIMING, fixed_sl)).fetchone()
    if not row:
        return None
    (window, z, arm, trail_buy_pct, trail_sell_pct, max_hold_hours,
     best_alpha, trades, win_rate) = row
    r = BIG6_CLIFF_RADIUS
    worst = con.execute(f"""
        SELECT MIN({ROBUST_ALPHA_SQL}) FROM backtest_cache
        WHERE version=? AND ticker=? AND strategy=? AND entry_timing=? AND stop_loss=?
          AND window=? AND z_score_threshold=?
          AND {arm_col} BETWEEN ? AND ?
          AND trail_sell_pct BETWEEN ? AND ?
          AND max_hold_hours BETWEEN ? AND ? AND trades>0
    """, (BIG6_VERSION, ticker, strategy, BIG6_ENTRY_TIMING, fixed_sl, window, z,
          arm - r, arm + r,
          trail_sell_pct - r, trail_sell_pct + r, max_hold_hours - 7, max_hold_hours + 7)).fetchone()[0]
    worst_neighbor = float(worst) if worst is not None else 0.0
    return {
        "ticker": ticker, "strategy": strategy, "window": window, "z": z,
        "arm_pct": arm, "fixed_sl": fixed_sl,
        "trail_buy_pct": trail_buy_pct, "trail_sell_pct": trail_sell_pct,
        "max_hold_hours": max_hold_hours, "entry_timing": BIG6_ENTRY_TIMING,
        "best_alpha": best_alpha, "worst_neighbor": worst_neighbor,
        "safe": worst_neighbor >= 0, "trades": trades, "win_rate": win_rate,
    }


def load_big6_nodes(tickers=None):
    """One node per ticker (best cliff-safe candidate across both v5 strategies x
    fixed_sl in {1,2,3}, falling back to best-any if nothing is cliff-safe -- same
    fallback convention as campaign_comparison_table.py), shaped to match load_nodes()'s
    output so run_ticker_sweep()/get_trades_and_bars() need no changes."""
    con = sqlite3.connect(UNIVERSE_DB)
    nodes = []
    for ticker in (tickers or BIG6_TICKERS):
        candidates = [
            _best_cell(con, ticker, strategy, fixed_sl)
            for strategy in BIG6_STRATEGIES for fixed_sl in BIG6_FIXED_SLS
        ]
        candidates = [c for c in candidates if c]
        if not candidates:
            print(f"  {ticker}: no v5/open_check backtest_cache data, skipped")
            continue
        safe = [c for c in candidates if c["safe"]]
        winner = max(safe, key=lambda c: c["best_alpha"]) if safe else max(candidates, key=lambda c: c["best_alpha"])
        nodes.append(winner)
        print(f"  {ticker}: picked {winner['strategy']} sl{winner['fixed_sl']} "
              f"(robust_alpha={winner['best_alpha']:.1f}%, safe={winner['safe']})")
    con.close()
    return nodes


def _compounded(rets):
    return float(np.prod([1 + r for r in rets]) - 1) if rets else float("nan")


def run_ticker_sweep(node):
    """Returns {(confirm_days, sl, arm, trail): [rets]} for one ticker -- windows are
    computed once per confirm_days and reused across every (sl, arm, trail) combo at that
    confirm_days, since window-finding doesn't depend on the exit-risk parameters at all."""
    trades, df_h = get_trades_and_bars(node)
    if len(trades) < 2:
        return {}

    cells = {}
    for confirm_days in CONFIRM_DAYS_GRID:
        windows = find_drought_windows(trades, df_h, confirm_days)
        if not windows:
            continue
        for sl, arm, trail in product(SL_GRID, ARM_GRID, TRAIL_GRID):
            rets = [simulate_overlay(df_h, entry_i, gap_end, sl, arm, trail)["ret"]
                    for entry_i, gap_end in windows]
            cells[(confirm_days, sl, arm, trail)] = rets
    return cells


# --- per-ticker mode (2026-08-05): independent SL/arm/trail/confirm_days optimization per
# ticker instead of one shared pooled cell, PLUS an intraday realized-vol entry gate as a
# 5th independent axis -- closes the "is the intraday-vol entry filter real, and does it
# hold per-ticker" question raised in conversation. Deliberately accepts real overfitting
# risk (10 independent 5-axis searches instead of 1 shared 4-axis one) per the user's
# explicit call ("I'm ok with a little overfit here").
#
# Vol measure: rolling stdev of hourly log returns over the trailing VOL_LOOKBACK_BARS
# non-9:30 bars, annualized -- the 9:30 bar is excluded because it's a near-instantaneous
# overnight repricing (the whole prior session's gap lands in one print), not accumulated
# intraday trading activity, and including it was shown (2026-08-05 conversation) to mask
# the real signal: a 5-day close-to-close measure looked like a calm trough for a SOXL
# trade that intraday (9:30-excluded) hourly vol showed was still >100% annualized, elevated
# for over a day before entry. VOL_GATE_GRID entries are percentile thresholds against the
# ticker's OWN historical intraday-vol distribution (not an absolute level, since baseline
# vol differs hugely by ticker) -- None means no gate (the original, ungated behavior).
VOL_LOOKBACK_BARS = 12  # ~2 trading days of non-9:30 bars (6/day)
VOL_GATE_GRID = [None, 0.3, 0.4, 0.5, 0.6, 0.7]


def get_ivol_series(ticker, window_bars=VOL_LOOKBACK_BARS):
    """Rolling annualized realized vol off hourly log returns, 9:30 bars excluded.
    Returns the full non-9:30 dataframe with an 'ivol' column (NaN for the first
    window_bars-1 rows) -- callers look up the nearest prior reading via .loc/searchsorted."""
    df = pd.read_csv(f"cache/research/{ticker}_1h.csv", index_col=0, parse_dates=True)
    close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    if close_col != "Close":
        df["Close"] = df[close_col]
    df["hret"] = np.log(df["Close"] / df["Close"].shift(1))
    non_open = df[df.index.hour != 9].copy()
    non_open["ivol"] = non_open["hret"].rolling(window_bars).std() * np.sqrt(252 * 6) * 100
    return non_open


def _entry_vol_pctile(entry_time, ivol_series):
    """Percentile rank of the intraday vol reading nearest at-or-before entry_time,
    against the ticker's own full historical ivol distribution. None if no reading
    exists yet (too early in history for a full lookback window)."""
    prior = ivol_series[ivol_series.index <= entry_time]
    if len(prior) == 0 or pd.isna(prior["ivol"].iloc[-1]):
        return None
    ivol_at_entry = prior["ivol"].iloc[-1]
    hist = ivol_series["ivol"].dropna()
    return float((hist < ivol_at_entry).mean())


def run_ticker_sweep_vol_gated(node, ivol_series):
    """Returns {(confirm_days, vol_gate, sl, arm, trail): [rets]} -- vol_gate filters
    which drought windows are eligible for entry (skip if the entry-time intraday-vol
    percentile is >= the gate threshold), computed once per (confirm_days, vol_gate) pair
    and reused across every (sl, arm, trail) combo, mirroring run_ticker_sweep's existing
    windows-computed-once-per-confirm_days pattern."""
    trades, df_h = get_trades_and_bars(node)
    if len(trades) < 2:
        return {}

    cells = {}
    for confirm_days in CONFIRM_DAYS_GRID:
        windows = find_drought_windows(trades, df_h, confirm_days)
        if not windows:
            continue
        for vol_gate in VOL_GATE_GRID:
            if vol_gate is None:
                gated_windows = windows
            else:
                gated_windows = []
                for entry_i, gap_end in windows:
                    entry_time = df_h.index[entry_i + 1] if entry_i + 1 < len(df_h) else df_h.index[entry_i]
                    pctile = _entry_vol_pctile(entry_time, ivol_series)
                    if pctile is not None and pctile < vol_gate:
                        gated_windows.append((entry_i, gap_end))
            if not gated_windows:
                continue
            for sl, arm, trail in product(SL_GRID, ARM_GRID, TRAIL_GRID):
                rets = [simulate_overlay(df_h, entry_i, gap_end, sl, arm, trail)["ret"]
                        for entry_i, gap_end in gated_windows]
                cells[(confirm_days, vol_gate, sl, arm, trail)] = rets
    return cells


def pool_cells(per_ticker_cells):
    """per_ticker_cells: {ticker: {(cd, sl, arm, trail): [rets]}} -> pooled
    {(cd, sl, arm, trail): [rets across all tickers]}."""
    pooled = {}
    for cells in per_ticker_cells.values():
        for key, rets in cells.items():
            pooled.setdefault(key, []).extend(rets)
    return pooled


def cliff_safety(pooled, key):
    """Grid-neighbor robustness: the worst pooled compounded return among this cell's
    immediate neighbors (one step in each of the 4 axes, using the real grid value lists --
    not a fixed +/-1 offset, so edge cells and uneven grid spacing are both handled
    correctly). A neighbor missing from `pooled` (e.g. zero drought windows at that
    confirm_days) is skipped, not treated as a failure -- absence of data isn't evidence of
    a cliff. Returns (worst_neighbor_compounded, n_neighbors_checked); NaN/0 if no
    neighbors exist at all (shouldn't happen for an interior cell)."""
    cd, sl, arm, trail = key
    grids = {0: CONFIRM_DAYS_GRID, 1: SL_GRID, 2: ARM_GRID, 3: TRAIL_GRID}
    neighbor_comps = []
    for axis, grid in grids.items():
        idx = grid.index(key[axis])
        for step in (-1, 1):
            n_idx = idx + step
            if not (0 <= n_idx < len(grid)):
                continue
            n_key = list(key)
            n_key[axis] = grid[n_idx]
            n_key = tuple(n_key)
            if n_key in pooled and pooled[n_key]:
                neighbor_comps.append(_compounded(pooled[n_key]))
    if not neighbor_comps:
        return float("nan"), 0
    return min(neighbor_comps), len(neighbor_comps)


def cliff_safety_5axis(cells, key):
    """Same grid-neighbor convention as cliff_safety(), extended to the 5-axis
    (confirm_days, vol_gate, sl, arm, trail) per-ticker cell space -- checked against
    that ONE ticker's own cells dict, not a cross-ticker pooled one."""
    cd, vg, sl, arm, trail = key
    grids = {0: CONFIRM_DAYS_GRID, 1: VOL_GATE_GRID, 2: SL_GRID, 3: ARM_GRID, 4: TRAIL_GRID}
    neighbor_comps = []
    for axis, grid in grids.items():
        idx = grid.index(key[axis])
        for step in (-1, 1):
            n_idx = idx + step
            if not (0 <= n_idx < len(grid)):
                continue
            n_key = list(key)
            n_key[axis] = grid[n_idx]
            n_key = tuple(n_key)
            if n_key in cells and cells[n_key]:
                neighbor_comps.append(_compounded(cells[n_key]))
    if not neighbor_comps:
        return float("nan"), 0
    return min(neighbor_comps), len(neighbor_comps)


def run_per_ticker_mode(nodes, top_n, write_csv):
    """Independent 5-axis (confirm_days, vol_gate, sl, arm, trail) sweep per ticker --
    no pooling across tickers at all. Reports each ticker's own best cliff-safe (falling
    back to best-any) cell. Real overfitting risk accepted deliberately (2026-08-05,
    user's explicit call) -- 10 independent 5-axis searches instead of 1 shared 4-axis
    pooled one."""
    all_rows = []
    winners = []
    for node in nodes:
        ticker = node["ticker"]
        try:
            ivol_series = get_ivol_series(ticker)
            cells = run_ticker_sweep_vol_gated(node, ivol_series)
        except Exception as e:
            print(f"{ticker}: failed ({e})")
            continue
        if not cells:
            print(f"  {ticker}: no droughts found, skipped")
            continue
        print(f"  {ticker}: {len(cells)} cells computed")

        rows = []
        for key, rets in cells.items():
            cd, vg, sl, arm, trail = key
            worst_neighbor, n_neighbors = cliff_safety_5axis(cells, key)
            comp = _compounded(rets)
            rows.append({
                "ticker": ticker, "confirm_days": cd, "vol_gate": vg, "fixed_sl": sl,
                "arm_pct": arm, "trail_sell_pct": trail, "n": len(rets),
                "win_rate": (np.array(rets) > 0).mean() if rets else float("nan"),
                "compounded_pct": comp * 100 if rets else float("nan"),
                "worst_neighbor_compounded_pct": worst_neighbor * 100 if not np.isnan(worst_neighbor) else float("nan"),
                "n_neighbors": n_neighbors,
                "cliff_safe": bool(not np.isnan(worst_neighbor) and worst_neighbor > 0),
            })
        tdf = pd.DataFrame(rows).sort_values("compounded_pct", ascending=False)
        all_rows.append(tdf)

        safe = tdf[tdf["cliff_safe"]]
        winner = safe.iloc[0] if not safe.empty else tdf.iloc[0]
        winners.append(winner)

    if not all_rows:
        print("No droughts found for any ticker.")
        return

    full_df = pd.concat(all_rows, ignore_index=True)
    pd.set_option("display.width", 220)

    if write_csv:
        OUTPUT_DIR.mkdir(exist_ok=True)
        full_df.to_csv(OUTPUT_DIR / "drought_overlay_sweep_per_ticker_5axis.csv", index=False)
        print(f"\nWrote {OUTPUT_DIR / 'drought_overlay_sweep_per_ticker_5axis.csv'}")

    print(f"\n--- Per-ticker winners (best cliff-safe, falling back to best-any) ---")
    wdf = pd.DataFrame(winners)[["ticker", "confirm_days", "vol_gate", "fixed_sl", "arm_pct",
                                  "trail_sell_pct", "n", "win_rate", "compounded_pct",
                                  "worst_neighbor_compounded_pct", "cliff_safe"]]
    print(wdf.round(3).to_string(index=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", default=None)
    parser.add_argument("--watchlist-id", type=int, default=65)
    parser.add_argument("--big6", action="store_true",
                         help="SPY/SSO/UPRO/QQQ/QLD/TQQQ from backtest_cache instead of watch_list")
    parser.add_argument("--per-ticker", action="store_true",
                         help="independent 5-axis sweep per ticker (adds a vol-gate axis), no cross-ticker pooling")
    parser.add_argument("--top-n", type=int, default=15)
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()

    nodes = load_big6_nodes(args.tickers) if args.big6 else load_nodes(args.watchlist_id, args.tickers)

    if args.per_ticker:
        run_per_ticker_mode(nodes, args.top_n, args.csv)
        return

    per_ticker_cells = {}
    for node in nodes:
        try:
            cells = run_ticker_sweep(node)
        except Exception as e:
            print(f"{node['ticker']}: failed ({e})")
            continue
        if cells:
            per_ticker_cells[node["ticker"]] = cells
        print(f"  {node['ticker']}: {len(cells)} cells computed")

    if not per_ticker_cells:
        print("No droughts found for any ticker.")
        return

    pooled = pool_cells(per_ticker_cells)

    rows = []
    for key, rets in pooled.items():
        cd, sl, arm, trail = key
        worst_neighbor, n_neighbors = cliff_safety(pooled, key)
        comp = _compounded(rets)
        rows.append({
            "confirm_days": cd, "fixed_sl": sl, "arm_pct": arm, "trail_sell_pct": trail,
            "n": len(rets), "win_rate": (np.array(rets) > 0).mean() if rets else float("nan"),
            "compounded_pct": comp * 100 if rets else float("nan"),
            "worst_neighbor_compounded_pct": worst_neighbor * 100 if not np.isnan(worst_neighbor) else float("nan"),
            "n_neighbors": n_neighbors,
            "cliff_safe": bool(not np.isnan(worst_neighbor) and worst_neighbor > 0),
        })
    df = pd.DataFrame(rows).sort_values("compounded_pct", ascending=False)
    pd.set_option("display.width", 200)

    if args.csv:
        OUTPUT_DIR.mkdir(exist_ok=True)
        df.to_csv(OUTPUT_DIR / "drought_overlay_sweep_pooled.csv", index=False)
        per_ticker_rows = []
        for ticker, cells in per_ticker_cells.items():
            for key, rets in cells.items():
                cd, sl, arm, trail = key
                per_ticker_rows.append({
                    "ticker": ticker, "confirm_days": cd, "fixed_sl": sl, "arm_pct": arm,
                    "trail_sell_pct": trail, "n": len(rets),
                    "win_rate": (np.array(rets) > 0).mean() if rets else float("nan"),
                    "compounded_pct": _compounded(rets) * 100 if rets else float("nan"),
                })
        pd.DataFrame(per_ticker_rows).to_csv(OUTPUT_DIR / "drought_overlay_sweep_per_ticker.csv", index=False)
        print(f"\nWrote {OUTPUT_DIR / 'drought_overlay_sweep_pooled.csv'} and "
              f"{OUTPUT_DIR / 'drought_overlay_sweep_per_ticker.csv'}")

    print(f"\n--- Top {args.top_n} pooled cells by compounded return "
          f"({len(pooled)} total cells, {sum(len(v) for v in pooled.values())} total trade-observations) ---")
    print(df.head(args.top_n).round(2).to_string(index=False))

    cliff_safe_df = df[df["cliff_safe"]]
    print(f"\n--- Top {args.top_n} CLIFF-SAFE pooled cells (worst neighbor also net positive) ---")
    if cliff_safe_df.empty:
        print("None -- no pooled cell has an entirely net-positive neighborhood.")
    else:
        print(cliff_safe_df.head(args.top_n).round(2).to_string(index=False))

    # SOXL in isolation for the best overall pooled cell, per the design doc's explicit ask
    # (SOXL's result reported both in isolation and pooled, since it was the one ticker with
    # a fragile-but-positive result in the earlier manual sweep).
    if not df.empty and "SOXL" in per_ticker_cells:
        best = df.iloc[0]
        key = (int(best["confirm_days"]), best["fixed_sl"], best["arm_pct"], best["trail_sell_pct"])
        soxl_rets = per_ticker_cells["SOXL"].get(key, [])
        print(f"\n--- SOXL in isolation at the best pooled cell {key} ---")
        if soxl_rets:
            print(f"n={len(soxl_rets)}  win_rate={(np.array(soxl_rets) > 0).mean():.3f}  "
                  f"compounded={_compounded(soxl_rets)*100:.1f}%")
        else:
            print("SOXL has zero drought windows at this cell's confirm_days.")


if __name__ == "__main__":
    main()
