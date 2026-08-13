"""Backtest of the user's actual proposed real-money allocation, quarterly rebalanced
25/25/25/25 (own-ticker/SSO/TQQQ/Cash) within each account bucket, same core-only
daily-bar approximation and data-floor handling as sim_1m_four_bucket_portfolio.py
(reused directly, not reimplemented) -- same stated limitations apply: core strategy
only (no drought/add-on), no expense-ratio/financing drag, Cash bucket flat 0%.

Real buckets (per the user's 2026-08-09 conversation, $410K total):
  brokerage-AGQ    $50,000   AGQ
  roth-SOXL        $30,000   SOXL
  roth-KORU        $30,000   KORU
  soxl_ira-SOXL   $200,000   SOXL
  ira-KORU        $100,000   KORU

Each bucket rebalances only against its own SSO/TQQQ/Cash holdings, same "no
cross-account transfers" rule as the original 3-account sim. Compared against a
100%-SSO baseline at the same $410K total (buy-and-hold, no rebalancing) -- this is
the "SSO/QLD safety net alone" alternative the user was originally sizing against.

Usage: .venv/bin/python scripts/sim_real_portfolio_rebalance.py [--start-year 1995] [--end-year 2024]
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.sim_1m_four_bucket_portfolio import (
    fetch_underlying_full,
    build_leg_series,
    quarterly_rebalance_multi,
    value_at_horizon,
    value_at_calendar_year_end,
)

HORIZONS = [5, 10, 15]

BUCKETS = {
    "brokerage-AGQ": ("AGQ", 50_000.0),
    "roth-SOXL": ("SOXL", 30_000.0),
    "roth-KORU": ("KORU", 30_000.0),
    "soxl_ira-SOXL": ("SOXL", 200_000.0),
    "ira-KORU": ("KORU", 100_000.0),
}
TOTAL_CAPITAL = sum(p for _, p in BUCKETS.values())


def real_portfolio_buckets(legs, master_index, start_idx):
    """Returns {bucket_label: curve} or None if any required ticker predates this
    cohort's start."""
    weights_template = {"SSO": 0.25, "TQQQ": 0.25, "CASH": 0.25}
    buckets = {}
    for label, (ticker, principal) in BUCKETS.items():
        acc_weights = dict(weights_template)
        acc_weights[ticker] = 0.25
        ratio_curves = {
            "SSO": legs["SSO"].to_numpy()[start_idx:],
            "TQQQ": legs["TQQQ"].to_numpy()[start_idx:],
            "CASH": legs["CASH"].to_numpy()[start_idx:],
            ticker: legs[ticker].to_numpy()[start_idx:],
        }
        if np.isnan(ratio_curves[ticker][0]) or np.isnan(ratio_curves["SSO"][0]) \
                or np.isnan(ratio_curves["TQQQ"][0]):
            return None
        buckets[label] = quarterly_rebalance_multi(ratio_curves, acc_weights, principal)
    return buckets


def real_portfolio_curve(legs, master_index, start_idx):
    buckets = real_portfolio_buckets(legs, master_index, start_idx)
    if buckets is None:
        return None
    return sum(buckets.values())


def single_asset_curve_scaled(legs, name, start_idx, principal):
    s = legs[name].to_numpy()[start_idx:]
    if len(s) == 0 or np.isnan(s[0]):
        return None
    return principal * (s / s[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-year", type=int, default=1995)
    ap.add_argument("--end-year", type=int, default=2024)
    args = ap.parse_args()

    print("Fetching underlying data and building leg equity curves (one-time)...")
    spy_full = fetch_underlying_full("SPY")
    master_index = spy_full.index
    today_last_idx = len(master_index) - 1

    legs = build_leg_series(master_index)
    print(f"Master calendar: {master_index[0].date()} .. {master_index[-1].date()} ({len(master_index)} trading days)")
    for name, s in legs.items():
        first_valid = s.first_valid_index()
        print(f"  {name:6s} first available: {first_valid.date() if first_valid is not None else 'N/A'}")

    print(f"\nBuckets (${TOTAL_CAPITAL:,.0f} total):")
    for label, (ticker, principal) in BUCKETS.items():
        print(f"  {label:16s} {ticker:5s} ${principal:>10,.0f}")

    rows = []
    for year in range(args.start_year, args.end_year + 1):
        start_date = pd.Timestamp(year, 1, 1)
        start_idx = master_index.searchsorted(start_date)
        if start_idx >= len(master_index):
            continue

        portfolios = {
            "Real portfolio (5-bucket, 25/25/25/25 each, core-only)": real_portfolio_curve(legs, master_index, start_idx),
            f"100% SSO (${TOTAL_CAPITAL:,.0f})": single_asset_curve_scaled(legs, "SSO", start_idx, TOTAL_CAPITAL),
            f"100% SPY (${TOTAL_CAPITAL:,.0f})": single_asset_curve_scaled(legs, "SPY", start_idx, TOTAL_CAPITAL),
        }

        for pname, curve in portfolios.items():
            row = {"cohort_start_year": year, "portfolio": pname}
            for h in HORIZONS:
                val, note = value_at_horizon(curve, master_index, start_idx, h, today_last_idx)
                row[f"value_+{h}y"] = val
                row[f"return_+{h}y"] = (val / TOTAL_CAPITAL - 1.0) if val is not None else None
                if note:
                    row[f"note_+{h}y"] = note
            rows.append(row)

    df = pd.DataFrame(rows)
    Path("output").mkdir(exist_ok=True)
    df.to_csv("output/sim_real_portfolio_rebalance.csv", index=False)
    print(f"\nWrote output/sim_real_portfolio_rebalance.csv ({len(df)} rows)")

    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 200)
    value_cols = [f"value_+{h}y" for h in HORIZONS]
    print(f"\n=== Dollar value at each horizon (starting ${TOTAL_CAPITAL:,.0f}) ===")
    print(df[["cohort_start_year", "portfolio"] + value_cols].to_string(
        index=False, float_format=lambda x: f"${x:,.0f}" if pd.notna(x) else "—"))

    print("\n=== Per-bucket breakdown for the most recent full 15y-eligible cohort ===")
    eligible_years = [y for y in range(args.start_year, args.end_year + 1)
                       if real_portfolio_buckets(legs, master_index, master_index.searchsorted(pd.Timestamp(y, 1, 1))) is not None]
    if eligible_years:
        # most recent cohort with 15 real years of forward data if possible, else most recent with data at all
        candidates = [y for y in eligible_years if value_at_horizon(
            real_portfolio_curve(legs, master_index, master_index.searchsorted(pd.Timestamp(y, 1, 1))),
            master_index, master_index.searchsorted(pd.Timestamp(y, 1, 1)), 15, today_last_idx)[0] is not None]
        y = candidates[-1] if candidates else eligible_years[-1]
        start_idx = master_index.searchsorted(pd.Timestamp(y, 1, 1))
        buckets = real_portfolio_buckets(legs, master_index, start_idx)
        print(f"Cohort start {y}:")
        for label, curve in buckets.items():
            ticker, principal = BUCKETS[label]
            for h in HORIZONS:
                val, note = value_at_horizon(curve, master_index, start_idx, h, today_last_idx)
                if val is not None:
                    print(f"  {label:16s} (${principal:>10,.0f} -> {ticker}): +{h}y = ${val:>12,.0f}  ({(val/principal-1)*100:+.1f}%)")
                elif note:
                    print(f"  {label:16s} +{h}y: {note}")


if __name__ == "__main__":
    main()
