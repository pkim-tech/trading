"""
Research script: does realized volatility show a real seasonal pattern -- specifically a
Christmas/EOY quiet period (the "Santa Claus rally" folk pattern) and/or a summer slowdown --
across SOXL/TQQQ/AGQ? Raised 2026-08-16: if real, this could justify manually pausing/reducing
the algo during a specific calendar window (a real operational decision, not automated) instead
of running full-size year-round.

Uses the same cached hourly data every other tool in this project reads (cache/research/
{ticker}_1h.csv). Default measure is daily-close log returns (x sqrt(252)) -- SIMPLE BUT
FLAGGED MISLEADING for this codebase's own purposes (docs/research_log.md, 2026-08-05: daily
close averages away intrabar moves, which matter a lot for a strategy whose signal is computed
on hourly bars intraday). --ivol switches to this project's own established intraday-vol
convention instead (drought_overlay_sweep.get_ivol_series's exact method: hourly log returns,
9:30 bars excluded, annualized x sqrt(252*6) since there are ~6 non-9:30 bars/trading day) --
same measure the real live SOXL drought vol-gate is built on, not a fresh reinvention. With
~3.06 years of cached history, each calendar month only has 2-3 independent yearly samples --
thin, directionally informative only, not a statistically powered test.

Usage: .venv/bin/python scripts/seasonal_volatility_check.py --tickers SOXL TQQQ AGQ
       .venv/bin/python scripts/seasonal_volatility_check.py --tickers SOXL TQQQ AGQ --ivol
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

CACHE_DIR = Path("cache/research")
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def load_daily(ticker):
    path = CACHE_DIR / f"{ticker}_1h.csv"
    df = pd.read_csv(path, index_col=0, parse_dates=True).sort_index()
    close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    daily = df[close_col].resample("D").last().dropna()
    return daily


def load_intraday_returns(ticker):
    """Hourly log returns, 9:30 bars excluded -- same series
    drought_overlay_sweep.get_ivol_series builds (not re-derived, mirrored exactly so this
    stays consistent with the real live vol-gate), just returned ungrouped so callers can
    bucket it by calendar window/month themselves instead of only a rolling lookback."""
    path = CACHE_DIR / f"{ticker}_1h.csv"
    df = pd.read_csv(path, index_col=0, parse_dates=True).sort_index()
    close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    if close_col != "Close":
        df["Close"] = df[close_col]
    hret = np.log(df["Close"] / df["Close"].shift(1))
    return hret[df.index.hour != 9].dropna()


def annualized_vol(returns, ivol=False):
    if len(returns) < 2:
        return None
    factor = np.sqrt(252 * 6) if ivol else np.sqrt(252)
    return float(returns.std() * factor * 100)


def monthly_vol_table(returns, ivol=False):
    by_month = {}
    for m in range(1, 13):
        sub = returns[returns.index.month == m]
        by_month[m] = annualized_vol(sub, ivol=ivol)
    return by_month


def _in_window_mask(index, month_day_start, month_day_end, wrap_year):
    m0, d0 = month_day_start
    m1, d1 = month_day_end
    key = np.array([(ts.month, ts.day) for ts in index])
    lo = np.array([m0, d0])
    hi = np.array([m1, d1])
    ge_lo = (key[:, 0] > lo[0]) | ((key[:, 0] == lo[0]) & (key[:, 1] >= lo[1]))
    le_hi = (key[:, 0] < hi[0]) | ((key[:, 0] == hi[0]) & (key[:, 1] <= hi[1]))
    return (ge_lo | le_hi) if wrap_year else (ge_lo & le_hi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=["SOXL", "TQQQ", "AGQ"])
    ap.add_argument("--christmas-start", default="12-20")
    ap.add_argument("--christmas-end", default="01-02")
    ap.add_argument("--summer-start", default="06-01")
    ap.add_argument("--summer-end", default="08-31")
    ap.add_argument("--ivol", action="store_true",
                     help="use this project's real intraday-vol measure (hourly log returns, "
                          "9:30 excluded) instead of daily-close returns")
    args = ap.parse_args()

    cs_m, cs_d = (int(x) for x in args.christmas_start.split("-"))
    ce_m, ce_d = (int(x) for x in args.christmas_end.split("-"))
    ss_m, ss_d = (int(x) for x in args.summer_start.split("-"))
    se_m, se_d = (int(x) for x in args.summer_end.split("-"))

    measure = "intraday (hourly, 9:30 excluded)" if args.ivol else "daily-close"
    print(f"vol measure: {measure}\n")
    print(f"{'ticker':8s}" + "".join(f"{m:>7s}" for m in MONTH_NAMES) + f"{'  overall':>10s}")
    all_returns = {}
    for ticker in args.tickers:
        returns = load_intraday_returns(ticker) if args.ivol else np.log(load_daily(ticker) / load_daily(ticker).shift(1)).dropna()
        all_returns[ticker] = returns
        by_month = monthly_vol_table(returns, ivol=args.ivol)
        overall = annualized_vol(returns, ivol=args.ivol)
        row = "".join(f"{by_month[m]:>6.0f}%" if by_month[m] is not None else "     -" for m in range(1, 13))
        print(f"{ticker:8s}" + row + f"{overall:>9.0f}%")

    print(f"\n(annualized realized vol %, by calendar month, pooled across all ~3 cached years "
          f"-- ~2-3 samples per month bucket, thin)\n")

    print(f"--- Christmas window ({args.christmas_start} to {args.christmas_end}) vs rest of year ---")
    for ticker, returns in all_returns.items():
        xmas_mask = _in_window_mask(returns.index, (cs_m, cs_d), (ce_m, ce_d), wrap_year=True)
        xmas_vol, xmas_n = annualized_vol(returns[xmas_mask], ivol=args.ivol), int(xmas_mask.sum())
        rest_vol = annualized_vol(returns[~xmas_mask], ivol=args.ivol)
        delta = xmas_vol - rest_vol if xmas_vol is not None and rest_vol is not None else None
        print(f"  {ticker:6s} christmas={xmas_vol:.0f}% (n={xmas_n})  rest_of_year={rest_vol:.0f}%  "
              f"delta={delta:+.0f}pp")

    print(f"\n--- Summer window ({args.summer_start} to {args.summer_end}) vs rest of year ---")
    for ticker, returns in all_returns.items():
        summer_mask = _in_window_mask(returns.index, (ss_m, ss_d), (se_m, se_d), wrap_year=False)
        summer_vol, summer_n = annualized_vol(returns[summer_mask], ivol=args.ivol), int(summer_mask.sum())
        rest_vol = annualized_vol(returns[~summer_mask], ivol=args.ivol)
        delta = summer_vol - rest_vol if summer_vol is not None and rest_vol is not None else None
        print(f"  {ticker:6s} summer={summer_vol:.0f}% (n={summer_n})  rest_of_year={rest_vol:.0f}%  "
              f"delta={delta:+.0f}pp")


if __name__ == "__main__":
    main()
