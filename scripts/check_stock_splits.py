"""Flag cached tickers whose price history spans a real stock split.

Uses yfinance's authoritative corporate-actions data (Ticker.splits) instead of
inferring splits from price jumps — deterministic, no threshold tuning, and it's
how the UVIX unadjusted-reverse-split bug (2026-07-01) was actually confirmed.

data_manager.py's incremental fetch only re-adjusts overlapping rows on update
(see fetch_live_data_smart), so any split landing after a ticker's initial 730-day
bootstrap can leave older cached rows stuck at the pre-split scale. Any ticker
flagged here should have its cache/{ticker}_1h.csv deleted and rebuilt.
"""
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import yfinance as yf

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


def check_ticker(csv_path: Path) -> list[dict]:
    ticker = csv_path.stem.replace("_1h", "")
    df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
    cache_start, cache_end = df.index.min(), df.index.max()

    splits = yf.Ticker(ticker).splits
    if splits.empty:
        return []

    splits.index = splits.index.tz_localize(None)
    in_range = splits[(splits.index >= cache_start) & (splits.index <= cache_end)]

    return [
        {"ticker": ticker, "split_date": date.strftime("%Y-%m-%d"), "ratio": ratio}
        for date, ratio in in_range.items()
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=20)
    args = parser.parse_args()

    csv_files = sorted(CACHE_DIR.glob("*_1h.csv"))
    all_flags = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(check_ticker, p): p for p in csv_files}
        for i, future in enumerate(as_completed(futures), 1):
            csv_path = futures[future]
            if i % 200 == 0:
                print(f"  ...checked {i}/{len(csv_files)}", file=sys.stderr)
            try:
                all_flags.extend(future.result())
            except Exception as e:
                print(f"⚠️  {csv_path.name}: failed to check ({e})", file=sys.stderr)

    if not all_flags:
        print(f"No splits found within cached history across {len(csv_files)} tickers.")
        return

    print(f"\nFound {len(all_flags)} split(s) within cached history across {len(csv_files)} tickers:\n")
    for flag in sorted(all_flags, key=lambda f: f["split_date"]):
        print(f"  {flag['ticker']:>6}  {flag['split_date']}  ratio={flag['ratio']}")


if __name__ == "__main__":
    main()
