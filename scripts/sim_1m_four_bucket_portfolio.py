"""$1M / 4-bucket portfolio simulation, rolling cohort starts 1995-2024, quarterly
rebalanced. Portfolio A: three EQUAL real-money accounts (Roth/soxl_ira/brokerage),
each independently 25% SSO / 25% TQQQ / 25% Cash / 25% its own algo ticker
(KORU / SOXL / AGQ respectively), rebalanced to that 25/25/25/25 target every ~63
trading days. No cross-account transfers -- each account only rebalances against its
own 4 holdings, matching how real Roth/IRA/taxable-brokerage accounts actually work
(can't freely shift gains between account types). Baselines: 100% SSO, 100% SPY,
100% TQQQ, same $1M starting capital, no accounts/rebalancing (single asset).

IMPORTANT SCOPE LIMITATION, stated explicitly, not hidden: the KORU/SOXL/AGQ legs run
CORE STRATEGY ONLY (real v5 node params: window/z-threshold/SL/trail/arm/max-hold).
Full "v5-stacked" (core + drought-overlay + margin add-on) was requested but is NOT
built here -- confirmed by reading scripts/v5_stacked_crash_stress.py's own docstring,
which documents this as a genuine, unbuilt gap even for the existing (shorter) crash-
stress tooling: add-on needs a real arm-day index run_strategy_daily doesn't track, and
drought-overlay needs a signal-gap detector against the synthetic series -- "a
meaningfully separate build, not a generalization of the existing pattern." Building
that properly for a 30-year daily-bar horizon is real follow-on work, not done here.

Also a coarser daily-bar approximation of the live hourly kernel throughout (same
caveats as sim_bear_market_stress.py) -- no expense-ratio/financing drag on the 2x/3x
ETFs, no dividends reinvested (auto_adjust=True on the underlying handles this for the
1x underlying only, not modeled separately on the synthetic leveraged product), Cash
bucket is flat 0% (a stated, easily-changed simplification, not a T-bill assumption).

Real, hard data constraint: leveraged ETFs proxied here didn't exist for the full
1995-2024 span. Portfolio A needs ALL of SPY/QQQ/SOXX/EWY/SLV -- SLV (AGQ's proxy)
only goes back to 2006-04-28, so Portfolio A is only computable for cohorts starting
2006 onward. The 100% TQQQ baseline needs QQQ (inception 1999-03-10), so it's only
computable from 1999/2000 onward. Cohorts before a portfolio's data floor are reported
as "no data" rather than silently fabricated. Similarly, a cohort near the present
(2023, 2024) may not yet have 5 real years of forward data -- reported as "not yet
reached" rather than extrapolated.

Usage: .venv/bin/python scripts/sim_1m_four_bucket_portfolio.py [--start-year 1995] [--end-year 2024]
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.sim_bear_market_stress import get_node_params, run_strategy_daily, synthesize_leveraged
from scripts.sim_skim_redeploy import build_equity_curve

STARTING_CAPITAL = 1_000_000.0
REBALANCE_EVERY_DAYS = 63  # ~1 quarter of trading days
HORIZONS = [2, 3, 4, 5]

# ticker -> (real underlying proxy for the daily-bar reconstruction, leverage)
UNDERLYING_PROXY = {
    "SSO": ("SPY", 2),
    "TQQQ": ("QQQ", 3),
    "SOXL": ("SOXX", 3),
    "KORU": ("EWY", 3),
    "AGQ": ("SLV", 2),
}

ACCOUNTS = {
    "Roth (KORU)": "KORU",
    "soxl_ira (SOXL)": "SOXL",
    "brokerage (AGQ)": "AGQ",
}


def fetch_underlying_full(ticker, start="1990-01-01"):
    df = yf.download(ticker, start=start, progress=False, auto_adjust=True)
    if df.empty:
        raise RuntimeError(f"no data for {ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df[["Open", "High", "Low", "Close"]].dropna()


def build_leg_series(master_index):
    """One-time build of every leg's day-indexed cumulative-return-ratio series
    (starting at whatever its own first available day is, NaN before that),
    reindexed onto the shared master calendar via forward-fill ONLY (no back-fill --
    leading NaN must stay NaN, that's how a cohort correctly detects 'this ticker
    doesn't exist yet')."""
    underlying_cache = {}
    for _proxy, _lev in UNDERLYING_PROXY.values():
        if _proxy not in underlying_cache:
            underlying_cache[_proxy] = fetch_underlying_full(_proxy)

    legs = {}

    # SSO / TQQQ: pure buy-and-hold leveraged reconstruction, no strategy trading.
    for name in ("SSO", "TQQQ"):
        proxy, lev = UNDERLYING_PROXY[name]
        synth = synthesize_leveraged(underlying_cache[proxy], lev)
        s = synth["Close"]
        legs[name] = s.reindex(master_index, method="ffill")

    # SPY itself (unleveraged), for the 100%-SPY baseline.
    legs["SPY"] = underlying_cache["SPY"]["Close"].reindex(master_index, method="ffill")
    # QQQ itself, only used to bound TQQQ's real data floor (not a baseline on its own).

    # KORU / SOXL / AGQ: real v5 core-strategy trades run across the FULL available
    # history for that proxy, then converted to a day-indexed equity curve (flat/cash
    # between trades, compounding only on realized closed-trade returns -- matches how
    # capital actually sits idle between signal windows live).
    for name in ("SOXL", "KORU", "AGQ"):
        proxy, lev = UNDERLYING_PROXY[name]
        synth = synthesize_leveraged(underlying_cache[proxy], lev)
        params = get_node_params(name)
        if params is None:
            raise RuntimeError(f"no real v5 node params found for {name} (watchlist_id=65)")
        trades = run_strategy_daily(synth, params)
        eq = build_equity_curve(trades, len(synth))
        s = pd.Series(eq, index=synth.index)
        legs[name] = s.reindex(master_index, method="ffill")

    legs["CASH"] = pd.Series(1.0, index=master_index)  # flat 0% -- stated simplification
    return legs


def quarterly_rebalance_multi(ratio_curves: dict, weights: dict, principal: float,
                               rebalance_every_days=REBALANCE_EVERY_DAYS):
    """ratio_curves: name -> aligned array of cumulative-return ratios (NaN-free over
    the slice being simulated), all starting at their own day-0 value (need not be
    1.0 -- normalized internally). weights: name -> target weight, reset every
    `rebalance_every_days` trading days. Returns a dollar-value array starting at
    `principal`."""
    names = list(weights.keys())
    n = len(next(iter(ratio_curves.values())))
    norm = {k: ratio_curves[k] / ratio_curves[k][0] for k in names}
    w = dict(weights)
    total = principal
    curve = np.empty(n)
    curve[0] = principal

    for i in range(1, n):
        vals = {}
        new_total = 0.0
        for k in names:
            r = norm[k][i] / norm[k][i - 1] - 1.0
            vals[k] = total * w[k] * (1.0 + r)
            new_total += vals[k]
        total = new_total
        for k in names:
            w[k] = vals[k] / total if total > 0 else weights[k]
        if i % rebalance_every_days == 0:
            w = dict(weights)
        curve[i] = total

    return curve


def slice_from(legs, start_date, master_index):
    """Returns (start_idx, available) -- available=False if any required leg is NaN
    at start_date (ticker doesn't exist yet for this cohort)."""
    start_idx = master_index.searchsorted(start_date)
    if start_idx >= len(master_index):
        return None, False
    return start_idx, True


def portfolio_a_accounts(legs, master_index, start_idx):
    """Returns {account_label: curve} for the 3 equal accounts, each independently
    25/25/25/25 SSO/TQQQ/CASH/own-ticker, each own-ticker rebalanced only against ITS
    OWN account's SSO/TQQQ/CASH -- never against the other two accounts' tickers.
    Returns None if any required ticker predates this cohort's start."""
    weights = {"SSO": 0.25, "TQQQ": 0.25, "CASH": 0.25}
    accounts = {}

    for label, ticker in ACCOUNTS.items():
        acc_weights = dict(weights)
        acc_weights[ticker] = 0.25
        ratio_curves = {
            "SSO": legs["SSO"].to_numpy()[start_idx:],
            "TQQQ": legs["TQQQ"].to_numpy()[start_idx:],
            "CASH": legs["CASH"].to_numpy()[start_idx:],
            ticker: legs[ticker].to_numpy()[start_idx:],
        }
        if np.isnan(ratio_curves[ticker][0]) or np.isnan(ratio_curves["SSO"][0]) \
                or np.isnan(ratio_curves["TQQQ"][0]):
            return None  # this cohort's start predates one of the required tickers
        accounts[label] = quarterly_rebalance_multi(ratio_curves, acc_weights, STARTING_CAPITAL / 3.0)

    return accounts


def portfolio_a_curve(legs, master_index, start_idx):
    accounts = portfolio_a_accounts(legs, master_index, start_idx)
    if accounts is None:
        return None
    return sum(accounts.values())


def single_asset_curve(legs, name, start_idx):
    s = legs[name].to_numpy()[start_idx:]
    if len(s) == 0 or np.isnan(s[0]):
        return None
    return STARTING_CAPITAL * (s / s[0])


def value_at_horizon(curve, master_index, start_idx, years, today_last_idx):
    if curve is None:
        return None, "no data (ticker doesn't exist yet at cohort start)"
    target_date = master_index[start_idx] + pd.DateOffset(years=years)
    target_idx_in_master = master_index.searchsorted(target_date)
    if target_idx_in_master > today_last_idx:
        return None, "not yet reached (insufficient real data so far)"
    local_idx = target_idx_in_master - start_idx
    if local_idx >= len(curve):
        return None, "not yet reached (insufficient real data so far)"
    return float(curve[local_idx]), None


def value_at_calendar_year_end(curve, master_index, start_idx, calendar_year, today_last_idx):
    """Value at the last trading day on/before Dec 31 of `calendar_year`. Explicitly
    checks the target DATE against the last real date available, not just the index --
    searchsorted(side='right') silently clips a future date to the last available
    index instead of exceeding it (caught live: was repeating 2024's last real value
    for every future year-end instead of reporting 'not yet reached')."""
    if curve is None:
        return None
    target_date = pd.Timestamp(calendar_year, 12, 31)
    if target_date > master_index[today_last_idx]:
        return None
    idx = master_index.searchsorted(target_date, side="right") - 1
    if idx < start_idx:
        return None
    local_idx = idx - start_idx
    if local_idx >= len(curve):
        return None
    return float(curve[local_idx])


def build_by_account_year_end_table(legs, master_index, start_year, end_year, today_last_idx,
                                     max_years_out=5):
    """Long-format: for every cohort start year, every calendar year-end through
    start+max_years_out (or through the last real data available, whichever is
    sooner), every entity (3 accounts + Portfolio A total + 3 single-asset baselines)
    -- so 'how did the IRA/SOXL account do vs the Roth/KORU account after N years' is
    directly answerable, not just the pooled total."""
    rows = []
    last_real_year = master_index[today_last_idx].year
    for year in range(start_year, end_year + 1):
        start_date = pd.Timestamp(year, 1, 1)
        start_idx = master_index.searchsorted(start_date)
        if start_idx >= len(master_index):
            continue

        accounts = portfolio_a_accounts(legs, master_index, start_idx)
        baselines = {
            "100% SSO": single_asset_curve(legs, "SSO", start_idx),
            "100% SPY": single_asset_curve(legs, "SPY", start_idx),
            "100% TQQQ": single_asset_curve(legs, "TQQQ", start_idx),
        }

        entities = dict(baselines)
        if accounts is not None:
            entities.update(accounts)
            entities["Portfolio A TOTAL (3 accounts)"] = sum(accounts.values())
        else:
            for label in ACCOUNTS:
                entities[label] = None
            entities["Portfolio A TOTAL (3 accounts)"] = None

        year_end_cap = min(year + max_years_out, last_real_year)
        for calendar_year in range(year, year_end_cap + 1):
            years_elapsed = calendar_year - year
            for entity, curve in entities.items():
                val = value_at_calendar_year_end(curve, master_index, start_idx, calendar_year, today_last_idx)
                rows.append({
                    "cohort_start_year": year,
                    "as_of_year_end": calendar_year,
                    "years_elapsed": years_elapsed,
                    "entity": entity,
                    "value": val,
                    "return_pct": (val / (STARTING_CAPITAL / 3.0) - 1.0)
                        if val is not None and entity in ACCOUNTS
                        else (val / STARTING_CAPITAL - 1.0) if val is not None else None,
                })
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-year", type=int, default=1995)
    ap.add_argument("--end-year", type=int, default=2024)
    ap.add_argument("--full-table-years", type=int, nargs="*", default=[2000, 2007],
                     help="cohort start years to print full year-end-by-year-end tables for "
                          "(default: right before the dot-com crash and right before the 2008 GFC)")
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

    rows = []
    for year in range(args.start_year, args.end_year + 1):
        start_date = pd.Timestamp(year, 1, 1)
        start_idx = master_index.searchsorted(start_date)
        if start_idx >= len(master_index):
            continue

        portfolios = {
            "Portfolio A (3-account, 25/25/25/25, core-only)": portfolio_a_curve(legs, master_index, start_idx),
            "100% SSO": single_asset_curve(legs, "SSO", start_idx),
            "100% SPY": single_asset_curve(legs, "SPY", start_idx),
            "100% TQQQ": single_asset_curve(legs, "TQQQ", start_idx),
        }

        for pname, curve in portfolios.items():
            row = {"cohort_start_year": year, "portfolio": pname}
            for h in HORIZONS:
                val, note = value_at_horizon(curve, master_index, start_idx, h, today_last_idx)
                row[f"value_+{h}y"] = val
                row[f"return_+{h}y"] = (val / STARTING_CAPITAL - 1.0) if val is not None else None
                if note:
                    row[f"note_+{h}y"] = note
            rows.append(row)

    df = pd.DataFrame(rows)
    Path("output").mkdir(exist_ok=True)
    df.to_csv("output/sim_1m_four_bucket_portfolio.csv", index=False)
    print(f"\nWrote output/sim_1m_four_bucket_portfolio.csv ({len(df)} rows)")

    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 200)
    value_cols = [f"value_+{h}y" for h in HORIZONS]
    print("\n=== Dollar value at each horizon (starting $1,000,000) ===")
    print(df[["cohort_start_year", "portfolio"] + value_cols].to_string(
        index=False, float_format=lambda x: f"${x:,.0f}" if pd.notna(x) else "—"))

    print("\nBuilding per-account year-end breakdown (full history through today)...")
    acct_df = build_by_account_year_end_table(legs, master_index, args.start_year, args.end_year,
                                               today_last_idx, max_years_out=100)
    acct_df.to_csv("output/sim_1m_by_account_year_end.csv", index=False)
    print(f"Wrote output/sim_1m_by_account_year_end.csv ({len(acct_df)} rows)")

    all_entities = list(ACCOUNTS.keys()) + ["Portfolio A TOTAL (3 accounts)", "100% SSO", "100% SPY", "100% TQQQ"]
    for y in args.full_table_years:
        sub = acct_df[acct_df["cohort_start_year"] == y]
        if sub.empty:
            continue
        pivot = sub.pivot(index="entity", columns="as_of_year_end", values="value")
        pivot = pivot.reindex(all_entities)
        print(f"\n=== Cohort start {y} — full value table by calendar year-end (through today) ===")
        print(pivot.to_string(float_format=lambda x: f"${x:,.0f}" if pd.notna(x) else "—"))


if __name__ == "__main__":
    main()
