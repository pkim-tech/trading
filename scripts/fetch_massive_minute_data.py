"""Pulls real 1-minute historical bar data from the Massive.com API (MASSIVE_API_KEY
in .env) for the 6 real live TrailingBoth tickers, 2 years back. Built 2026-08-21 (very
late) to characterize the four hourly-resolution findings from tonight's session against
real minute data instead of hourly-bar approximation. See docs/backlog_cache.md.

Handles pagination (next_url) and the free tier's 5-calls/minute rate limit. Includes
extended hours (pre-market/after-hours) -- confirmed real via scripts/test_massive_api.py,
04:00-19:59 ET per day, not just the regular 9:30-16:00 session.

Saves one CSV per ticker to cache/research/minute_data/{ticker}_1m.csv.

Usage:
    .venv/bin/python scripts/fetch_massive_minute_data.py [--tickers T ...] [--years N]
"""
import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("MASSIVE_API_KEY")
OUT_DIR = Path(__file__).resolve().parent.parent / "cache" / "research" / "minute_data"

TICKERS = ["SOXL", "DPST", "KORU", "JNUG", "HIBL", "LABU"]

# Account upgraded 2026-08-21 (night) from the free tier's 5-calls/min to an
# "unlimited" plan -- SLEEP_BETWEEN_CALLS dropped to a small courtesy pause
# (not zero, in case "unlimited" still has an unstated practical ceiling) plus
# real 429 retry/backoff, since a burst of calls with no throttling at all is
# more likely to actually hit a transient limit than the old 5/min pacing was.
SLEEP_BETWEEN_CALLS = 0.15
MAX_RETRIES = 5


def fetch_ticker(ticker, start, end):
    all_rows = []
    url = f"https://api.massive.com/v2/aggs/ticker/{ticker}/range/1/minute/{start.isoformat()}/{end.isoformat()}"
    params = {"limit": 50000, "sort": "asc", "adjusted": "true", "apiKey": API_KEY}
    call_count = 0
    while url:
        call_count += 1
        for attempt in range(MAX_RETRIES):
            resp = requests.get(url, params=params, timeout=60)
            if resp.status_code == 429:
                backoff = 2.0 * (attempt + 1)
                print(f"  {ticker}: 429 rate-limited, retry {attempt+1}/{MAX_RETRIES} in {backoff:.0f}s",
                      file=sys.stderr)
                time.sleep(backoff)
                continue
            break
        if resp.status_code != 200:
            print(f"  {ticker}: HTTP {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
            break
        data = resp.json()
        results = data.get("results", [])
        all_rows.extend(results)
        print(f"  {ticker}: call {call_count}, +{len(results)} rows (total {len(all_rows)})")
        next_url = data.get("next_url")
        if not next_url:
            break
        url = next_url
        params = {"apiKey": API_KEY}  # next_url already has query params baked in except the key
        time.sleep(SLEEP_BETWEEN_CALLS)
    return all_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=TICKERS)
    ap.add_argument("--years", type=int, default=2)
    args = ap.parse_args()

    if not API_KEY:
        print("MASSIVE_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    end = date.today()
    start = end - timedelta(days=365 * args.years)

    for i, ticker in enumerate(args.tickers):
        print(f"=== {ticker} ({start} to {end}) ===")
        rows = fetch_ticker(ticker, start, end)
        if not rows:
            print(f"  {ticker}: no data, skipping")
            continue
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert("America/New_York")
        df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close",
                                 "v": "Volume", "vw": "VWAP", "n": "NumTrades"})
        df = df[["timestamp", "Open", "High", "Low", "Close", "Volume", "VWAP", "NumTrades"]]
        out_path = OUT_DIR / f"{ticker}_1m.csv"
        df.to_csv(out_path, index=False)
        print(f"  {ticker}: saved {len(df):,} rows to {out_path} ({out_path.stat().st_size / 1_000_000:.1f}MB)")
        if i < len(args.tickers) - 1:
            time.sleep(SLEEP_BETWEEN_CALLS)


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
