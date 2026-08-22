"""Corporate-action discontinuity check: real splits vs raw 5yr minute data.

Built for docs/plans/ground_truth_kernel_rebuild.md's Step 1 prerequisite #1.

The plan's original draft assumed the manually-curated signals_db.corporate_actions
table already had entries (e.g. "SOXS's confirmed 2026-07-14 split") to cross-
reference against. Checked directly (2026-08-21): that table is EMPTY (0 rows) in
cache/live/trading_live.db -- it's a curated table for a different purpose
(live-side split detection alerts, see signals_compute.py/signals_helpers.py) and
was never populated with historical splits. The real authoritative split-history
source already used in this project is yfinance's Ticker.splits, as
scripts/check_stock_splits.py already established for the HOURLY csv cache (found
40/82 candidate tickers with a real split landing inside their cached window).

This script reuses that same yfinance-splits detection, but checks the raw MINUTE
CSVs directly for a price discontinuity at each flagged split date -- the actual
question this plan's prerequisite asks: does Massive's `adjusted=true` minute feed
already reflect the split (smooth price across the date), or does it leave a
step (unadjusted, needs a rescale like the hourly CSVs' own split-guard does)?

Method: for each (ticker, split_date, ratio) where the split falls inside the
ticker's minute-data window, compare the median Close of the last N regular-
session minutes before the split date to the median Close of the first N minutes
on/after it. A real un-adjusted split leaves a jump close to the split ratio; an
already-adjusted feed shows near-zero jump (normal day-to-day drift only).
Flags anything outside a tolerance band around 1.0x (adjusted) OR the expected
ratio (correctly un-adjusted, if this project's own overlay code expects raw
prices -- worth a human look either way, this script only flags, never rescales).

Usage:
    .venv/bin/python scripts/check_minute_split_discontinuity.py
    .venv/bin/python scripts/check_minute_split_discontinuity.py --tickers SOXS KORU ETHU
Read-only. Writes output/minute_split_discontinuity_report.csv.
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MINUTE_DIR = os.path.join(ROOT, "cache", "research", "minute_data")
OUT = os.path.join(ROOT, "output", "minute_split_discontinuity_report.csv")

N_MINUTES = 30           # minutes sampled either side of the split date
JUMP_TOLERANCE = 0.05     # 5% -- above this and not near a known ratio => flag


def all_tickers():
    return sorted(f[:-len("_1m.csv")] for f in os.listdir(MINUTE_DIR) if f.endswith("_1m.csv"))


def load_minutes(ticker):
    df = pd.read_csv(os.path.join(MINUTE_DIR, f"{ticker}_1m.csv"))
    ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("US/Eastern").dt.tz_localize(None)
    df = df.set_index(ts).sort_index()
    t = df.index.time
    keep = (t >= pd.Timestamp("09:30").time()) & (t < pd.Timestamp("16:00").time())
    return df.loc[keep, ["Close"]]


def check_ticker(ticker):
    m = load_minutes(ticker)
    if m.empty:
        return []
    start, end = m.index.min(), m.index.max()

    splits = yf.Ticker(ticker).splits
    if splits.empty:
        return []
    splits.index = splits.index.tz_localize(None)
    in_range = splits[(splits.index >= start) & (splits.index <= end)]
    if in_range.empty:
        return []

    out = []
    for split_date, ratio in in_range.items():
        before = m.loc[:split_date - pd.Timedelta(seconds=1)].tail(N_MINUTES)
        after = m.loc[split_date:].head(N_MINUTES)
        if before.empty or after.empty:
            out.append(dict(ticker=ticker, split_date=split_date.strftime("%Y-%m-%d"),
                             ratio=ratio, jump=None, verdict="insufficient_data_around_date"))
            continue
        med_before = before.Close.median()
        med_after = after.Close.median()
        jump = med_after / med_before

        adjusted_ok = abs(jump - 1.0) <= JUMP_TOLERANCE
        # yfinance's split ratio is the multiplier applied to SHARE COUNT (e.g. 0.1 for a
        # 1-for-10 reverse split), so an unadjusted PRICE feed jumps by 1/ratio across the
        # date (price rises ~10x on a 1-for-10 reverse split), not by ratio itself. Found
        # by the paired-review independent-cold pass (2026-08-21) — the original `jump vs
        # ratio` comparison mislabeled every genuinely-unadjusted split as UNEXPLAINED_JUMP
        # instead of UNADJUSTED_STEP_MATCHES_RATIO (still flagged either way, just the
        # wrong reason).
        expected_unadjusted_jump = 1.0 / ratio if ratio else 0.0
        unadjusted_matches_ratio = (
            abs(jump - expected_unadjusted_jump) / expected_unadjusted_jump <= JUMP_TOLERANCE
            if expected_unadjusted_jump else False
        )
        if adjusted_ok:
            verdict = "adjusted_clean"
        elif unadjusted_matches_ratio:
            verdict = "UNADJUSTED_STEP_MATCHES_RATIO"
        else:
            verdict = "UNEXPLAINED_JUMP"

        out.append(dict(ticker=ticker, split_date=split_date.strftime("%Y-%m-%d"),
                         ratio=ratio, jump=round(float(jump), 4), verdict=verdict))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*")
    args = ap.parse_args()

    tickers = args.tickers or all_tickers()
    rows = []
    for i, t in enumerate(tickers, 1):
        try:
            r = check_ticker(t)
        except Exception as e:
            r = [dict(ticker=t, split_date=None, ratio=None, jump=None,
                       verdict=f"error: {e}")]
        rows.extend(r)
        if r:
            print(f"[{i}/{len(tickers)}] {t}: {r}")

    df = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    df.to_csv(OUT, index=False)

    if df.empty:
        print("\nNo splits landed inside any ticker's minute-data window.")
        return
    bad = df[~df.verdict.isin(["adjusted_clean"])]
    print(f"\n{len(df)} (ticker, split) pairs checked across {df.ticker.nunique()} tickers "
          f"with an in-window split.")
    print(f"Non-clean verdicts: {len(bad)}")
    if len(bad):
        print(bad.to_string(index=False))
    print(f"Full report: {OUT}")


if __name__ == "__main__":
    main()
