"""Read-only canary: is Schwab's get_quote (or Massive's live minute-bar API)
usable pre-market, compared against yfinance?

Built 2026-09-10 per planner-session backlog item: active_signals.py's entry/
signal-check price path is pure yfinance with no fallback, while the exit/
reconcile path (schwab_client.get_current_price) already does
Schwab-primary/yfinance-fallback. Before scoping a fix we need real evidence
of whether Schwab's quote (or Massive's live API) is even valid pre-market --
get_current_price's except block is silent (schwab_client.py:1582-1584) so no
historical log evidence exists either way.

Single-pass mode (changed 2026-09-10, same evening): one invocation = one poll
round across all three sources (Schwab raw quote, yfinance fast_info, Massive
latest-minute-bar) for every live/paper watch_list ticker, then exit. Meant to
be driven externally via crontab (every 30 min, midnight-8:30am ET), not an
internal sleep loop.

Massive query mirrors the request shape of
.claude/worktrees/agent-a47e2b4b97765997b/scripts/fetch_massive_minute_data.py's
fetch_ticker (MASSIVE_API_KEY via .env), narrowed to a single today's-date
range query, taking the latest bar in the response.

Never places orders, never posts Slack, never modifies any DB table --
observability only.

Usage: .venv/bin/python scripts/check_schwab_premarket_quote_validity.py
"""
import csv
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import schwab_client
import signals_config as cfg

load_dotenv()

MASSIVE_API_KEY = os.environ.get("MASSIVE_API_KEY")
ET = ZoneInfo("America/New_York")
OUT_DIR = Path(__file__).resolve().parent.parent / "logs"

FIELDNAMES = [
    "poll_time", "ticker",
    "schwab_quote_lastPrice", "schwab_quote_tradeTime",
    "schwab_quote_bidPrice", "schwab_quote_askPrice",
    "schwab_extended_lastPrice", "schwab_extended_tradeTime",
    "yfinance_last_price",
    "massive_latest_bar_timestamp", "massive_latest_bar_close",
    "massive_result_count",
    "schwab_error", "yfinance_error", "massive_error",
]


def get_watchlist_tickers() -> list:
    conn = sqlite3.connect(cfg.DB_PATH)
    try:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM watch_list WHERE state IN ('live','paper') AND archived_at IS NULL"
        ).fetchall()
    finally:
        conn.close()
    return sorted(r[0] for r in rows)


def fetch_massive_latest_bar(ticker: str) -> dict:
    """Latest minute bar for `ticker` today, mirroring fetch_massive_minute_data.py's
    fetch_ticker request shape but narrowed to today's date, single call, no pagination."""
    result = {"timestamp": None, "close": None, "result_count": None, "error": None}
    if not MASSIVE_API_KEY:
        result["error"] = "MASSIVE_API_KEY not set in .env"
        return result
    today = date.today().isoformat()
    url = f"https://api.massive.com/v2/aggs/ticker/{ticker}/range/1/minute/{today}/{today}"
    params = {"limit": 50000, "sort": "desc", "adjusted": "true", "apiKey": MASSIVE_API_KEY}
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            result["error"] = f"HTTP {resp.status_code}: {resp.text[:300]}"
            return result
        data = resp.json()
        results = data.get("results", [])
        result["result_count"] = len(results)
        if results:
            latest = results[0]
            result["timestamp"] = datetime.fromtimestamp(
                latest["t"] / 1000, tz=ZoneInfo("UTC")
            ).astimezone(ET).isoformat()
            result["close"] = latest.get("c")
    except Exception as e:
        result["error"] = repr(e)
    return result


def poll_ticker(ticker: str) -> dict:
    row = {k: None for k in FIELDNAMES}
    row["poll_time"] = datetime.now(ET).isoformat()
    row["ticker"] = ticker

    try:
        r = schwab_client._get_client().get_quote(ticker)
        r.raise_for_status()
        data = r.json()[ticker]
        quote = data.get("quote") or {}
        extended = data.get("extended") or {}
        row["schwab_quote_lastPrice"] = quote.get("lastPrice")
        row["schwab_quote_tradeTime"] = quote.get("tradeTime")
        row["schwab_quote_bidPrice"] = quote.get("bidPrice")
        row["schwab_quote_askPrice"] = quote.get("askPrice")
        row["schwab_extended_lastPrice"] = extended.get("lastPrice")
        row["schwab_extended_tradeTime"] = extended.get("tradeTime")
    except Exception as e:
        row["schwab_error"] = repr(e)

    try:
        import yfinance as yf
        row["yfinance_last_price"] = yf.Ticker(ticker).fast_info.last_price
    except Exception as e:
        row["yfinance_error"] = repr(e)

    massive = fetch_massive_latest_bar(ticker)
    row["massive_latest_bar_timestamp"] = massive["timestamp"]
    row["massive_latest_bar_close"] = massive["close"]
    row["massive_result_count"] = massive["result_count"]
    row["massive_error"] = massive["error"]

    return row


def main():
    tickers = get_watchlist_tickers()
    if not tickers:
        print("No live/paper tickers found in watch_list -- nothing to poll.")
        return

    today = datetime.now(ET).strftime("%Y%m%d")
    out_path = OUT_DIR / f"schwab_premarket_canary_{today}.csv"
    write_header = not out_path.exists()

    print(f"Single-pass poll of {len(tickers)} tickers ({tickers}) -> {out_path}")

    with open(out_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        for ticker in tickers:
            row = poll_ticker(ticker)
            writer.writerow(row)

    print(f"Done: {len(tickers)} rows appended.")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    sys.exit(main())
