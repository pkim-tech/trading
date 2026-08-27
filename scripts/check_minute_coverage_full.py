"""Full-scope minute-data coverage/population check: all 82 tickers, full 5yr window.

Built for docs/plans/ground_truth_kernel_rebuild.md's Step 1 prerequisite #2 --
tonight's (2026-08-21) --coverage-report on sim_minute_groundtruth_independent.py
only verified 2yr/6 tickers. This extends the same falsifiable range-coverage test
(does the minute feed's per-hour [min Low, max High] contain the independent
hourly bar's [Low, High]?) to every ticker with cached minute data, over each
ticker's own full available window (not a fixed date range -- coverage varies by
when each ticker's minute fetch started).

Reuses coverage_report()/load_hourly()/load_minutes() from
sim_minute_groundtruth_independent.py directly rather than reimplementing the
range-coverage logic -- avoids a second parallel implementation of the same check
(the exact failure mode that motivated this whole plan).

Usage:
    .venv/bin/python scripts/check_minute_coverage_full.py
    .venv/bin/python scripts/check_minute_coverage_full.py --tickers SOXL HIBL
Read-only. Writes output/minute_coverage_full_report.csv.
"""
import argparse
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from sim_minute_groundtruth_independent import (  # noqa: E402
    HOURLY_DIR, MINUTE_DIR, coverage_report,
)

OUT = os.path.join(ROOT, "output", "minute_coverage_full_report.csv")


def all_tickers():
    return sorted(
        f[:-len("_1m.csv")] for f in os.listdir(MINUTE_DIR)
        if f.endswith("_1m.csv")
    )


def ticker_window(ticker):
    """Real available window: intersection of hourly-CSV and minute-CSV coverage."""
    dfh = pd.read_csv(os.path.join(HOURLY_DIR, f"{ticker}_1h.csv"),
                       index_col=0, parse_dates=True)
    dfh.index = pd.to_datetime(dfh.index).tz_localize(None)
    dfm = pd.read_csv(os.path.join(MINUTE_DIR, f"{ticker}_1m.csv"))
    mts = pd.to_datetime(dfm["timestamp"], utc=True).dt.tz_convert("US/Eastern").dt.tz_localize(None)
    start = max(dfh.index.min(), mts.min()).strftime("%Y-%m-%d")
    end = min(dfh.index.max(), mts.max()).strftime("%Y-%m-%d")
    return start, end


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*")
    args = ap.parse_args()

    tickers = args.tickers or all_tickers()
    rows = []
    for i, t in enumerate(tickers, 1):
        try:
            start, end = ticker_window(t)
            r = coverage_report(t, start, end)
            r["start"] = start
            r["end"] = end
        except Exception as e:
            r = dict(ticker=t, bars=None, minute_fill=None, no_minutes=None,
                      low_uncovered=None, high_uncovered=None, start=None, end=None,
                      error=str(e))
        rows.append(r)
        print(f"[{i}/{len(tickers)}] {t}: {r}")

    df = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    df.to_csv(OUT, index=False)

    bad = df[(df.get("low_uncovered", 0).fillna(0) > 0) | (df.get("high_uncovered", 0).fillna(0) > 0)]
    errored = df[df.get("error").notna()] if "error" in df.columns else df.iloc[0:0]
    print(f"\n{len(df)} tickers checked. Uncovered bars found in: "
          f"{list(bad.ticker) if len(bad) else 'NONE'}")
    if len(errored):
        print(f"Errored (see error column): {list(errored.ticker)}")
    print(f"Full report: {OUT}")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
