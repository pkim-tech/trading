"""Ad hoc variant comparator for sim_1m_four_bucket_portfolio.py's Portfolio A structure
-- lets you swap the per-account weight scheme (e.g. drop Cash/TQQQ, go 50/50 SSO+ticker)
and/or the Cash leg's yield assumption (flat 0% vs real ^IRX T-bill rate), without
duplicating the whole 30-cohort harness each time. Same core-strategy-only /
daily-bar-approximation caveats as that script -- see its module docstring.

Usage: .venv/bin/python scripts/sim_portfolio_variant_compare.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ itself, for sim_bear_market_stress etc.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sim_1m_four_bucket_portfolio import (
    ACCOUNTS, HORIZONS, STARTING_CAPITAL, build_leg_series, fetch_underlying_full,
    quarterly_rebalance_multi, single_asset_curve, value_at_horizon,
)


def build_tbill_cash_leg(master_index):
    irx = yf.download("^IRX", start="1990-01-01", progress=False, auto_adjust=True)
    if isinstance(irx.columns, pd.MultiIndex):
        irx.columns = irx.columns.get_level_values(0)
    irx_yield = irx["Close"].reindex(master_index, method="ffill").bfill()
    daily_rate = (irx_yield / 100.0) / 365.0
    return (1.0 + daily_rate).cumprod()


def account_curve(legs, master_index, start_idx, ticker, weights_template):
    """weights_template: dict with 'TICKER' as a placeholder key, e.g. {'SSO':0.5,'TICKER':0.5}."""
    weights = {k if k != "TICKER" else ticker: v for k, v in weights_template.items()}
    n = len(master_index) - start_idx
    ratio_curves = {}
    for name in weights:
        arr = legs[name].to_numpy()[start_idx:]
        if np.isnan(arr[0]):
            return None
        ratio_curves[name] = arr
    return quarterly_rebalance_multi(ratio_curves, weights, STARTING_CAPITAL / 3.0)


def portfolio_curve(legs, master_index, start_idx, weights_template):
    total = None
    for _label, ticker in ACCOUNTS.items():
        c = account_curve(legs, master_index, start_idx, ticker, weights_template)
        if c is None:
            return None
        total = c if total is None else total + c
    return total


def max_dd(curve, master_index, start_idx, today_last_idx):
    if curve is None:
        return None
    end = today_last_idx - start_idx + 1
    v = curve[:end]
    if len(v) < 2:
        return None
    peak = np.maximum.accumulate(v)
    return float(np.min(v / peak - 1.0))


def evaluate_variant(legs, master_index, today_last_idx, weights_template, label, start_year=1995, end_year=2024):
    rows = []
    for year in range(start_year, end_year + 1):
        start_date = pd.Timestamp(year, 1, 1)
        start_idx = master_index.searchsorted(start_date)
        if start_idx >= len(master_index):
            continue
        port = portfolio_curve(legs, master_index, start_idx, weights_template)
        sso = single_asset_curve(legs, "SSO", start_idx)
        dd_port = max_dd(port, master_index, start_idx, today_last_idx)
        dd_sso = max_dd(sso, master_index, start_idx, today_last_idx)
        for h in HORIZONS:
            vp, _ = value_at_horizon(port, master_index, start_idx, h, today_last_idx)
            vs, _ = value_at_horizon(sso, master_index, start_idx, h, today_last_idx)
            if vp is not None and vs is not None:
                rows.append({"cohort": year, "h": h, "portfolio": vp, "sso": vs, "beats_sso": vp > vs})
        if dd_port is not None:
            rows.append({"cohort": year, "h": "max_dd", "portfolio": dd_port, "sso": dd_sso,
                         "beats_sso": (dd_port > dd_sso) if dd_sso is not None else None})

    df = pd.DataFrame(rows)
    val_rows = df[df.h != "max_dd"]
    dd_rows = df[df.h == "max_dd"]
    print(f"\n=== {label} ===")
    print(f"  Terminal value: beats 100% SSO in {val_rows.beats_sso.sum()}/{len(val_rows)} "
          f"({val_rows.beats_sso.mean():.0%}) cohort-horizon pairs")
    for h in HORIZONS:
        sub = val_rows[val_rows.h == h]
        if len(sub):
            print(f"    +{h}y: {sub.beats_sso.sum()}/{len(sub)}")
    if len(dd_rows):
        print(f"  Mean max drawdown: portfolio={dd_rows.portfolio.mean():.1%}  "
              f"100% SSO={dd_rows.sso.mean():.1%}  "
              f"(shallower than SSO in {dd_rows.beats_sso.sum()}/{dd_rows.beats_sso.notna().sum()} cohorts)")
    return df


def main():
    print("Fetching underlying data and building legs...")
    spy_full = fetch_underlying_full("SPY")
    master_index = spy_full.index
    today_last_idx = len(master_index) - 1
    legs = build_leg_series(master_index)

    legs_tbill = dict(legs)
    legs_tbill["CASH"] = build_tbill_cash_leg(master_index)

    evaluate_variant(legs, master_index, today_last_idx,
                      {"SSO": 0.25, "TQQQ": 0.25, "CASH": 0.25, "TICKER": 0.25},
                      "Baseline: 25/25/25/25 SSO/TQQQ/Cash(flat 0%)/ticker")

    evaluate_variant(legs_tbill, master_index, today_last_idx,
                      {"SSO": 0.25, "TQQQ": 0.25, "CASH": 0.25, "TICKER": 0.25},
                      "25/25/25/25 SSO/TQQQ/Cash(real ^IRX T-bill)/ticker")

    evaluate_variant(legs, master_index, today_last_idx,
                      {"SSO": 0.5, "TICKER": 0.5},
                      "50/50 SSO/ticker (no TQQQ, no Cash)")

    evaluate_variant(legs_tbill, master_index, today_last_idx,
                      {"SSO": 0.5, "TICKER": 0.5},
                      "50/50 SSO/ticker (Cash leg unused in this variant, listed for consistency)")


if __name__ == "__main__":
    main()
