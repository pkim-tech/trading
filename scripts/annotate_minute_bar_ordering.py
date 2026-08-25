"""Same technique as scripts/annotate_hourly_bar_ordering.py, one level finer:
uses real 1-second data (scripts/fetch_massive_second_data.py) to determine, per real
1-minute bar, whether the Low or High actually occurred first -- real measured ordering,
not the assumed/extrapolated 50/50 from the hourly-via-minute check (2026-08-24,
"promoter" session: the hourly finding doesn't transfer down a scale without its own
measurement).

Output: cache/research/second_data/{ticker}_minute_ordering.csv, one row per 1-minute
bar with any 1-second coverage: minute_start (ET), low_ts, low_price, high_ts,
high_price, low_before_high, n_seconds (coverage sanity check, ~60 for a fully-covered
minute).

Usage:
    .venv/bin/python scripts/annotate_minute_bar_ordering.py [--tickers T ...]
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SECOND_DATA_DIR = Path(__file__).resolve().parent.parent / "cache" / "research" / "second_data"
TICKERS = ["SOXL"]


def annotate_ticker(ticker):
    path = SECOND_DATA_DIR / f"{ticker}_1s.csv"
    if not path.exists():
        print(f"  {ticker}: no second data file, skipping")
        return None
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("America/New_York")
    df = df.set_index("timestamp").sort_index()
    df["minute_start"] = df.index.floor("min")

    rows = []
    for minute_start, grp in df.groupby("minute_start"):
        low_ts = grp["Low"].idxmin()
        high_ts = grp["High"].idxmax()
        rows.append(dict(
            minute_start=minute_start,
            low_ts=low_ts, low_price=grp.loc[low_ts, "Low"],
            high_ts=high_ts, high_price=grp.loc[high_ts, "High"],
            low_before_high=bool(low_ts < high_ts),
            n_seconds=len(grp),
        ))
    out = pd.DataFrame(rows).sort_values("minute_start")
    out_path = SECOND_DATA_DIR / f"{ticker}_minute_ordering.csv"
    out.to_csv(out_path, index=False)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=TICKERS)
    args = ap.parse_args()

    for ticker in args.tickers:
        print(f"=== {ticker} ===")
        out = annotate_ticker(ticker)
        if out is None:
            continue
        pct_low_first = out["low_before_high"].mean() * 100
        print(f"  {len(out):,} 1-minute bars annotated, {out['minute_start'].min()} -> {out['minute_start'].max()}")
        print(f"  low_before_high: {pct_low_first:.1f}% of bars")
        print(f"  n_seconds coverage: min={out['n_seconds'].min()} median={out['n_seconds'].median():.0f} max={out['n_seconds'].max()}")

        # Also split by time-of-day window, since the range-check earlier tonight
        # showed the morning open is far more volatile than midday/afternoon --
        # worth checking whether ordering probability is also window-dependent,
        # not just a single ticker-wide average.
        out["t"] = out["minute_start"].dt.time
        for wname, (start, end) in {
            "open_check_morning": (pd.Timestamp("09:31").time(), pd.Timestamp("09:40").time()),
            "open_check_midday": (pd.Timestamp("14:31").time(), pd.Timestamp("14:40").time()),
        }.items():
            mask = (out["t"] >= start) & (out["t"] <= end)
            sub = out.loc[mask]
            if len(sub):
                print(f"  {wname}: n={len(sub)}, low_before_high={sub['low_before_high'].mean()*100:.1f}%")


if __name__ == "__main__":
    main()
