"""Pre-processes real 1-minute data (scripts/fetch_massive_minute_data.py) into a
lightweight per-hourly-bar annotation: WHEN within each hour the Low and High actually
occurred. Built 2026-08-21 (very late) -- replaces the inline minute-lookup approach in
scripts/sim_minute_ground_truth_full.py with a one-time precompute, reusable by any
future script without re-parsing minute data each run. See docs/backlog_cache.md.

From low_ts/high_ts alone, any downstream kernel logic can derive whatever ordering fact
it needs (did Low happen before High; was a given stop_price breached after a given entry
minute; etc.) via simple timestamp comparison -- no need to re-walk raw minute bars.

Output: cache/research/minute_data/{ticker}_hourly_ordering.csv, one row per hourly bar
with ANY minute coverage, columns: hour_start (ET, matches the hourly kernel's own bar
labeling), low_ts, low_price, high_ts, high_price, low_before_high, n_minutes (coverage
sanity check -- should be ~60 for a fully-covered regular-session hour, less for
partial/extended-hours-only coverage).

Usage:
    .venv/bin/python scripts/annotate_hourly_bar_ordering.py [--tickers T ...]
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MINUTE_DATA_DIR = Path(__file__).resolve().parent.parent / "cache" / "research" / "minute_data"
TICKERS = ["SOXL", "DPST", "KORU", "JNUG", "HIBL", "LABU"]


def annotate_ticker(ticker):
    path = MINUTE_DATA_DIR / f"{ticker}_1m.csv"
    if not path.exists():
        print(f"  {ticker}: no minute data file, skipping")
        return None
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("America/New_York")
    df = df.set_index("timestamp").sort_index()
    df["hour_start"] = df.index.floor("h")

    rows = []
    for hour_start, grp in df.groupby("hour_start"):
        low_ts = grp["Low"].idxmin()
        high_ts = grp["High"].idxmax()
        rows.append(dict(
            hour_start=hour_start,
            low_ts=low_ts, low_price=grp.loc[low_ts, "Low"],
            high_ts=high_ts, high_price=grp.loc[high_ts, "High"],
            low_before_high=bool(low_ts < high_ts),
            n_minutes=len(grp),
        ))
    out = pd.DataFrame(rows).sort_values("hour_start")
    out_path = MINUTE_DATA_DIR / f"{ticker}_hourly_ordering.csv"
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
        print(f"  {len(out):,} hourly bars annotated, {out['hour_start'].min()} -> {out['hour_start'].max()}")
        print(f"  low_before_high: {pct_low_first:.1f}% of bars")
        print(f"  n_minutes coverage: min={out['n_minutes'].min()} median={out['n_minutes'].median():.0f} max={out['n_minutes'].max()}")


if __name__ == "__main__":
    main()
