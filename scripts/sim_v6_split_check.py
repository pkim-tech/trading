"""
Out-of-sample robustness check for the v6 parking-vehicle idea (2026-07-22):
splits the real gap windows chronologically into folds and scores each
candidate independently in every fold -- a candidate whose edge only shows
up in some folds is noise dressed up as a result, not a real hedge-direction
effect. Run scripts/sim_v6_parking_vehicle_sweep.py --extract-only first if
output/v6_gap_windows.csv doesn't exist yet.

Usage:
  .venv/bin/python scripts/sim_v6_split_check.py --split 50-50 [--candidates T1 T2 ...]
  .venv/bin/python scripts/sim_v6_split_check.py --split 70-30
  .venv/bin/python scripts/sim_v6_split_check.py --split 5fold
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from scripts.sim_v6_parking_vehicle_sweep import GAP_WINDOWS_CSV, candidate_window_returns, WATCHLIST_65_TICKERS

DEFAULT_CANDIDATES = [
    "SPY", "QQQ", "SH", "SDS", "SPXU", "SPXS", "PSQ", "SQQQ", "QID", "BIL",
    "MTUL", "LABU", "BBC", "SMH", "KSTR", "PILL", "SOXX",
]


def summarize(rets):
    rets = [r for r in rets if r is not None]
    if not rets:
        return None
    compounded = 1.0
    for r in rets:
        compounded *= (1 + r)
    wins = sum(1 for r in rets if r > 0)
    return {
        "n": len(rets),
        "compounded_pct": (compounded - 1) * 100,
        "win_rate_pct": wins / len(rets) * 100,
    }


def fold_boundaries(n, split):
    """List of (start_idx, end_idx) index pairs for the requested split."""
    if split == "5fold":
        k = 5
        size = n // k
        bounds = [(i * size, (i + 1) * size if i < k - 1 else n) for i in range(k)]
    else:
        frac = {"50-50": 0.5, "70-30": 0.7}[split]
        cut = int(n * frac)
        bounds = [(0, cut), (cut, n)]
    return bounds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["50-50", "70-30", "5fold"], default="50-50")
    ap.add_argument("--candidates", nargs="*", default=DEFAULT_CANDIDATES)
    args = ap.parse_args()

    if not GAP_WINDOWS_CSV.exists():
        raise SystemExit(f"{GAP_WINDOWS_CSV} not found -- run sim_v6_parking_vehicle_sweep.py --extract-only first")
    windows_df = pd.read_csv(GAP_WINDOWS_CSV, parse_dates=["start", "end"]).sort_values("start").reset_index(drop=True)
    n = len(windows_df)
    bounds = fold_boundaries(n, args.split)

    print(f"{n} total windows ({windows_df['start'].min().date()} to {windows_df['start'].max().date()}), split={args.split}")
    for i, (a, b) in enumerate(bounds):
        fold_df = windows_df.iloc[a:b]
        print(f"  fold {i + 1}: {b - a} windows, {fold_df['start'].min().date()} to {fold_df['start'].max().date()}")
    print()

    rows = []
    for c in args.candidates:
        if c in WATCHLIST_65_TICKERS:
            continue  # a source ticker scored against its own windows isn't the parking question
        all_rets = candidate_window_returns(c, windows_df)
        if all_rets is None:
            print(f"{c}: no data, skipping")
            continue
        fold_summaries = [summarize(all_rets[a:b]) for a, b in bounds]
        if any(s is None for s in fold_summaries):
            print(f"{c}: insufficient data in one fold, skipping")
            continue
        s_all = summarize(all_rets)
        signs = [s["compounded_pct"] > 0 for s in fold_summaries]
        row = {"candidate": c, "full_compounded_pct": s_all["compounded_pct"],
               "full_win_rate_pct": s_all["win_rate_pct"]}
        for i, s in enumerate(fold_summaries):
            row[f"fold{i + 1}_compounded_pct"] = s["compounded_pct"]
            row[f"fold{i + 1}_win_rate_pct"] = s["win_rate_pct"]
        row["all_folds_same_sign"] = all(sg == signs[0] for sg in signs)
        rows.append(row)

    out = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    print(out.to_string(index=False))
    print()
    print("all_folds_same_sign=False means the full-sample result is being carried by only some "
          "folds -- not a consistent effect, don't trust it.")


if __name__ == "__main__":
    main()
