"""
v6 idea, generalized (2026-07-22): rather than assuming SPY is the right
"park idle capital" vehicle, extract every real EOD-exit-to-next-entry gap
window across all 10 watchlist-65 tickers (using each ticker's real v5 node
and the parity-verified simulate_trail_both_annotated trade list), then scan
the whole cached ticker universe (~1,445 tickers on file -- broad market,
sector, and crypto-linked leveraged ETFs included) for which one would have
made the most money if parked during exactly those windows. Framed as: "we
had spare capital across ~2 years of real gaps -- what would have made
money with it?" -- not a recommendation to actually trade every one of
these, just a scan to see what's worth a closer look.

Each window is priced independently (nearest Close at-or-before the window's
start/end timestamp) -- this does NOT model overlapping-window capital
constraints (two tickers can have gaps open at the same time; this script
doesn't try to net that out), so the "compounded across all windows"
number is a simplification: "if you always had capital free for every
window," not a literal single-account backtest. Flagged in the output.

Usage:
  .venv/bin/python scripts/sim_v6_parking_vehicle_sweep.py --extract-only
  .venv/bin/python scripts/sim_v6_parking_vehicle_sweep.py [--top N] [--candidates T1 T2 ...]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

import strategies
from backtester import prep_inputs, run_backtest_dispatch
from scripts.export_trades import load_hourly

LIVE_DB = Path(__file__).resolve().parent.parent / "cache" / "live" / "trading_live.db"
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "research"
WATCHLIST_65_TICKERS = ["AGQ", "DPST", "GDXU", "HIBL", "KORU", "NUGT", "SOXL", "UDOW", "USD", "YANG"]
GAP_WINDOWS_CSV = Path("output") / "v6_gap_windows.csv"


def get_node(ticker, watchlist_id=65):
    conn = sqlite3.connect(LIVE_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM watch_list WHERE ticker=? AND watchlist_id=?", (ticker, watchlist_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def extract_gap_windows(ticker, eod_only=False):
    """Every real exit -> next-entry window for one ticker's active v5 node --
    idle capital exists between ANY exit and the next entry, not just the
    rare case where the exit happens to land on the day's genuinely last bar
    (that EOD-only restriction is what made the original 2026-07-22 pass so
    thin -- 62 windows total across all 10 tickers, since a true end-of-day
    coincidence is inherently rare for an opportunistic SL/trailing-stop/TIME
    exit). Set eod_only=True to reproduce the original restricted definition.
    Uses run_backtest_dispatch (the same strategy-aware single source of
    truth run_optimization_sweep.py uses) rather than assuming every
    watchlist-65 node is TrailingBothZScoreBreakout -- 6 of the 10 are
    actually TrailingExitZScoreBreakout."""
    node = get_node(ticker)
    if node is None:
        print(f"  {ticker}: no watchlist_id=65 node, skipping")
        return []
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

    raw_trades = run_backtest_dispatch(
        strategy_class, df_h, ind, ticker,
        take_profit=take_profit, sl_raw=sl_raw, max_hours_to_hold=node["max_hold_hours"],
        z_score_threshold=node["z_score_threshold"], fixed_sl=node["fixed_sl"],
        trail_pct_pct=trail_pct_pct, entry_timing=node.get("entry_timing", "close"), prep=p,
    )

    timestamps = p["timestamps"]
    last_bar_of_day = pd.Series(timestamps).groupby(pd.Series(timestamps).dt.date).max()
    last_bar_set = set(last_bar_of_day.values)

    windows = []
    prev_exit_ts = None
    for t in raw_trades:
        entry_ts = t["Entry Time"]
        exit_ts = t["Exit Time"]
        if prev_exit_ts is not None and prev_exit_ts < entry_ts:
            is_eod = prev_exit_ts in last_bar_set
            if not eod_only or is_eod:
                windows.append({"source_ticker": ticker, "start": prev_exit_ts, "end": entry_ts,
                                 "was_eod": is_eod})
        prev_exit_ts = exit_ts
    return windows


def extract_all_windows(eod_only=False):
    all_windows = []
    for ticker in WATCHLIST_65_TICKERS:
        w = extract_gap_windows(ticker, eod_only=eod_only)
        label = "EOD" if eod_only else "all"
        print(f"  {ticker}: {len(w)} {label} gap windows")
        all_windows.extend(w)
    df = pd.DataFrame(all_windows)
    GAP_WINDOWS_CSV.parent.mkdir(exist_ok=True)
    df.to_csv(GAP_WINDOWS_CSV, index=False)
    print(f"Total: {len(df)} gap windows across {len(WATCHLIST_65_TICKERS)} tickers -> {GAP_WINDOWS_CSV}")
    return df


def nearest_price(df, ts):
    idx = df.index[df.index <= ts]
    if len(idx) == 0:
        return None
    return float(df.loc[idx[-1], "Close"])


def candidate_window_returns(candidate, windows_df):
    """Per-window return for one candidate, aligned to windows_df's row order
    (None for any window the candidate lacks data for) -- shared by
    score_candidate and the out-of-sample split check so both use the exact
    same pricing logic."""
    path = CACHE_DIR / f"{candidate}_1h.csv"
    if not path.exists():
        return None
    try:
        df = load_hourly(candidate)
    except Exception:
        return None
    rets = []
    for _, w in windows_df.iterrows():
        start_p = nearest_price(df, w["start"])
        end_p = nearest_price(df, w["end"])
        rets.append((end_p / start_p) - 1 if start_p and end_p else None)
    return rets


def score_candidate(candidate, windows_df):
    all_rets = candidate_window_returns(candidate, windows_df)
    if all_rets is None:
        return None
    rets = [r for r in all_rets if r is not None]
    if len(rets) < len(windows_df) * 0.8:  # candidate missing data for too many windows
        return None
    compounded = 1.0
    for r in rets:
        compounded *= (1 + r)
    wins = sum(1 for r in rets if r > 0)
    return {
        "candidate": candidate,
        "windows_covered": len(rets),
        "compounded_return_pct": (compounded - 1) * 100,
        "mean_window_return_pct": (sum(rets) / len(rets)) * 100,
        "win_rate_pct": wins / len(rets) * 100,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract-only", action="store_true")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--candidates", nargs="*", default=None,
                     help="limit the scan to these tickers instead of the full cached universe")
    args = ap.parse_args()

    if GAP_WINDOWS_CSV.exists() and not args.extract_only:
        windows_df = pd.read_csv(GAP_WINDOWS_CSV, parse_dates=["start", "end"])
        print(f"Reusing {len(windows_df)} cached gap windows from {GAP_WINDOWS_CSV} "
              f"(delete the file to re-extract)")
    else:
        print("Extracting real gap windows from the 10 watchlist-65 tickers...")
        windows_df = extract_all_windows()

    if args.extract_only:
        return

    candidates = args.candidates or sorted(
        p.stem[:-3] for p in CACHE_DIR.glob("*_1h.csv")
        if p.stem[:-3] not in WATCHLIST_65_TICKERS
    )
    print(f"Scanning {len(candidates)} candidate vehicles against {len(windows_df)} gap windows "
          f"(excluding the {len(WATCHLIST_65_TICKERS)} source tickers themselves -- scoring a "
          f"source ticker against its own gap windows measures 'should we not have exited', "
          f"not the parking question)...")

    results = []
    for i, c in enumerate(candidates):
        r = score_candidate(c, windows_df)
        if r:
            results.append(r)
        if (i + 1) % 200 == 0:
            print(f"  ...{i + 1}/{len(candidates)} scanned")

    out = pd.DataFrame(results).sort_values("compounded_return_pct", ascending=False)
    out_path = Path("output") / "v6_vehicle_sweep_results.csv"
    out.to_csv(out_path, index=False)
    print(f"\nTop {args.top} by compounded return over the real gap windows:")
    print(out.head(args.top).to_string(index=False))
    print(f"\nFull results ({len(out)} candidates with sufficient data) -> {out_path}")
    print("\nNote: this compounds each candidate across ALL pooled windows chronologically, "
          "as if capital were always free for every window -- it does NOT model overlapping "
          "windows across tickers sharing one real capital pool. Treat as a screen, not a plan.")


if __name__ == "__main__":
    main()
