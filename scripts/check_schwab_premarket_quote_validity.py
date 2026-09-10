"""Read-only canary: does Schwab's get_quote return usable pre-market data?

Built 2026-09-10 per planner-session backlog item: active_signals.py's entry/
signal-check price path is pure yfinance with no fallback, while the exit/
reconcile path (schwab_client.get_current_price) already does
Schwab-primary/yfinance-fallback. Before scoping a fix we need real evidence
of whether Schwab's quote is even valid pre-market -- get_current_price's
except block is silent (schwab_client.py:1582-1584) so there's no historical
log evidence either way.

Polls every ticker currently state IN ('live','paper') in watch_list, once
per POLL_INTERVAL_SECONDS, logging Schwab's raw quote/extended fields plus a
yfinance comparison price to a CSV. Never places orders, never posts Slack,
never modifies any DB table -- observability only.

Usage: nohup .venv/bin/python scripts/check_schwab_premarket_quote_validity.py &
Meant to be launched manually pre-market; not a cron job, not daemon-linked.
"""
import csv
import sqlite3
import sys
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import schwab_client
import signals_config as cfg

POLL_INTERVAL_SECONDS = 60
MARKET_OPEN_ET = dtime(9, 33)  # 9:30 + a few minutes buffer
ET = ZoneInfo("America/New_York")


def get_watchlist_tickers() -> list:
    conn = sqlite3.connect(cfg.DB_PATH)
    try:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM watch_list WHERE state IN ('live','paper') AND archived_at IS NULL"
        ).fetchall()
    finally:
        conn.close()
    return sorted(r[0] for r in rows)


def poll_ticker(ticker: str) -> dict:
    row = {
        "poll_time": datetime.now(ET).isoformat(),
        "ticker": ticker,
        "schwab_quote_lastPrice": None,
        "schwab_quote_tradeTime": None,
        "schwab_quote_bidPrice": None,
        "schwab_quote_askPrice": None,
        "schwab_extended_lastPrice": None,
        "schwab_extended_tradeTime": None,
        "yfinance_last_price": None,
        "schwab_error": None,
        "yfinance_error": None,
    }

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

    return row


def main():
    tickers = get_watchlist_tickers()
    if not tickers:
        print("No live/paper tickers found in watch_list -- nothing to poll.")
        return

    today = datetime.now(ET).strftime("%Y%m%d")
    out_path = f"logs/schwab_premarket_canary_{today}.csv"

    print(f"Polling {len(tickers)} tickers ({tickers}) every {POLL_INTERVAL_SECONDS}s -> {out_path}")
    print(f"Will stop once ET time passes {MARKET_OPEN_ET}.")

    write_header = True
    poll_count = 0
    with open(out_path, "a", newline="") as f:
        writer = None
        while True:
            now_et = datetime.now(ET)
            for ticker in tickers:
                row = poll_ticker(ticker)
                if writer is None:
                    writer = csv.DictWriter(f, fieldnames=list(row.keys()))
                    if write_header:
                        writer.writeheader()
                        write_header = False
                writer.writerow(row)
            f.flush()
            poll_count += 1
            print(f"[{now_et.isoformat()}] poll #{poll_count} done for {len(tickers)} tickers")

            if now_et.time() >= MARKET_OPEN_ET:
                print(f"Reached {MARKET_OPEN_ET} ET -- stopping.")
                break

            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
