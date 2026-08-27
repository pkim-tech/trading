"""Pulls real 1-second historical bar data from the Massive.com API
(MASSIVE_API_KEY in .env) -- built 2026-08-24 (promoter session) to measure
real 1-minute-bar intrabar ordering (does Low or High come first) the same
way scripts/annotate_hourly_bar_ordering.py measured hourly-bar ordering
using minute data, one level finer. Confirmed directly (2026-08-24): 1-second
aggregates work back to 2021-08-25 (rolling ~5yr window, same span as the
existing 1-minute pull) -- NOT capped to a recent-months-only window the way
tick/trade-level data is (that endpoint returned 403, needs a plan upgrade).

Same pagination/rate-limit handling as fetch_massive_minute_data.py. Default
scope is 1 year, SOXL only -- deliberately narrower than the 1-minute pull's
5yr/6-ticker scope, since 1-second data is ~60x the row count/storage; widen
via --years/--tickers only once the smaller pull's proven useful.

Saves one CSV per ticker to cache/research/second_data/pulls/{ticker}_1s_{start}_{end}.csv --
NEVER writes to the canonical cache/research/second_data/{ticker}_1s.csv path directly (same
never-overwrite-canonical-directly convention as fetch_massive_minute_data.py, added
2026-08-26 after that script's no-args refresh silently narrowed SOXL/DPST/DFEN's canonical
minute archive). Use scripts/promote_market_data_pull.py --kind second to promote a staged
pull into the canonical location.

Usage:
    .venv/bin/python scripts/fetch_massive_second_data.py [--tickers T ...] [--years N]
    .venv/bin/python scripts/fetch_massive_second_data.py [--tickers T ...] --start-date 2021-08-27
"""
import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("MASSIVE_API_KEY")
OUT_DIR = Path(__file__).resolve().parent.parent / "cache" / "research" / "second_data"
PULLS_DIR = OUT_DIR / "pulls"

TICKERS = ["SOXL"]
SLEEP_BETWEEN_CALLS = 0.15
MAX_RETRIES = 5


def _rows_to_df(results):
    df = pd.DataFrame(results)
    df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert("America/New_York")
    df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close",
                             "v": "Volume", "vw": "VWAP", "n": "NumTrades"})
    return df[["timestamp", "Open", "High", "Low", "Close", "Volume", "VWAP", "NumTrades"]]


def fetch_ticker_streaming(ticker, start, end, out_path):
    """Streams each page straight to out_path (CSV, header on first write only)
    instead of accumulating all rows in memory first -- 5yr of 1-second data is
    ~30M+ rows, which OOM-killed the prior all-in-memory version (real incident,
    2026-08-24: python killed at 14.4GB RSS on a 15GB machine, mid-fetch, no
    traceback since the OOM killer doesn't give the process a chance to log
    one -- confirmed via dmesg/journalctl, not a script bug in the request
    logic itself)."""
    url = f"https://api.massive.com/v2/aggs/ticker/{ticker}/range/1/second/{start.isoformat()}/{end.isoformat()}"
    params = {"limit": 50000, "sort": "asc", "adjusted": "true", "apiKey": API_KEY}
    call_count = 0
    total_rows = 0
    wrote_header = False
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
        if results:
            df = _rows_to_df(results)
            df.to_csv(out_path, mode="a" if wrote_header else "w", header=not wrote_header, index=False)
            wrote_header = True
            total_rows += len(results)
        if call_count % 20 == 0 or not data.get("next_url"):
            print(f"  {ticker}: call {call_count}, +{len(results)} rows (total {total_rows:,})")
        next_url = data.get("next_url")
        if not next_url:
            break
        url = next_url
        params = {"apiKey": API_KEY}
        time.sleep(SLEEP_BETWEEN_CALLS)
    return total_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=TICKERS)
    ap.add_argument("--years", type=float, default=1)
    ap.add_argument("--start-date", type=str, default=None,
                     help="explicit YYYY-MM-DD start date, overrides --years")
    args = ap.parse_args()

    if not API_KEY:
        print("MASSIVE_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)

    PULLS_DIR.mkdir(parents=True, exist_ok=True)
    end = date.today()
    start = datetime.strptime(args.start_date, "%Y-%m-%d").date() if args.start_date else end - timedelta(days=int(365 * args.years))

    total = len(args.tickers)
    job_start = time.time()
    for i, ticker in enumerate(args.tickers, start=1):
        elapsed = time.time() - job_start
        eta = (elapsed / (i - 1) * (total - i + 1)) if i > 1 else float("nan")
        print(f"=== [{i}/{total}] {ticker} ({start} to {end})  job_elapsed={elapsed:.0f}s  eta_remaining={eta:.0f}s ===")
        out_path = PULLS_DIR / f"{ticker}_1s_{start}_{end}.csv"
        n_rows = fetch_ticker_streaming(ticker, start, end, out_path)
        if not n_rows:
            print(f"  {ticker}: no data, skipping")
            continue
        print(f"  {ticker}: saved {n_rows:,} rows to {out_path} ({out_path.stat().st_size / 1_000_000:.1f}MB)")
        print(f"  {ticker}: staged only -- run scripts/promote_market_data_pull.py --ticker {ticker} --kind second to make this canonical")
        if i < total:
            time.sleep(SLEEP_BETWEEN_CALLS)


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
