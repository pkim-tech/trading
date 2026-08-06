"""
Daily options-chain snapshot collector for the v5 watchlist tickers, started 2026-08-06 as
groundwork for the put-hedge cost/payoff modeling idea (docs/backlog_cache.md's
put-hedge entries) -- yfinance only exposes LIVE options data (no historical chains), so
there is no way to backtest a put-hedge design against real past premiums the way every
other piece of this system backtests against real historical price data. This script takes
one real snapshot per run (puts only -- calls aren't relevant to a hedge) so a genuine
history starts accumulating now instead of waiting until the put-hedge design is ready and
finding there's still nothing to backtest against, the same "record now, use later"
rationale as the still-unbuilt 1-minute-bar backlog idea.

Real constraint found before writing this (2026-08-06, ad hoc yfinance check): GDXU has
ZERO options expirations -- no options market exists for it at all, so it's structurally
excluded from any put-hedge design. Of the other 9 v5 tickers, only AGQ/DPST/KORU/NUGT/SOXL
have near-term WEEKLY expirations; HIBL/UDOW/USD/YANG's nearest expiration is 2+ weeks out,
which rules those 4 out for an overnight-only (buy-before-close/sell-at-open) hedge design
specifically, since you'd be forced to buy multi-week protection every night. Liquidity is
also thin nearly everywhere except KORU and SOXL (real open interest, tighter spreads) --
worth weighing before assuming any of this is actually executable at real size.

Usage: .venv/bin/python scripts/collect_options_snapshot.py [--tickers ...] [--expirations N]
       Meant to be run daily (cron/manual), not continuously -- each run is one snapshot.
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.drought_detection_test import load_nodes

DB_PATH = Path(__file__).resolve().parent.parent / "cache" / "research" / "trading_universe.db"
DEFAULT_TICKERS = ["AGQ", "DPST", "GDXU", "HIBL", "KORU", "NUGT", "SOXL", "UDOW", "USD", "YANG", "USO"]


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS options_snapshot (
            snapshot_ts TEXT NOT NULL,
            ticker TEXT NOT NULL,
            underlying_price REAL,
            expiration TEXT NOT NULL,
            strike REAL NOT NULL,
            bid REAL,
            ask REAL,
            last_price REAL,
            volume REAL,
            open_interest REAL,
            implied_volatility REAL,
            PRIMARY KEY (snapshot_ts, ticker, expiration, strike)
        )
    """)
    conn.commit()


def collect_ticker(ticker, n_expirations):
    """Returns (rows, underlying_price, expirations_available) for one ticker's puts
    across its nearest n_expirations real expirations. Empty rows (not an error) if the
    ticker has no options market at all -- GDXU is the known real case as of 2026-08-06."""
    tk = yf.Ticker(ticker)
    exps = tk.options
    if not exps:
        return [], None, 0
    px = tk.fast_info.get("lastPrice") or tk.fast_info.get("last_price")
    ts = datetime.now(timezone.utc).isoformat()
    rows = []
    for exp in exps[:n_expirations]:
        chain = tk.option_chain(exp)
        for _, p in chain.puts.iterrows():
            rows.append((
                ts, ticker, px, exp, float(p["strike"]),
                float(p["bid"]) if p["bid"] == p["bid"] else None,
                float(p["ask"]) if p["ask"] == p["ask"] else None,
                float(p["lastPrice"]) if p["lastPrice"] == p["lastPrice"] else None,
                float(p["volume"]) if p["volume"] == p["volume"] else None,
                float(p["openInterest"]) if p["openInterest"] == p["openInterest"] else None,
                float(p["impliedVolatility"]) if p["impliedVolatility"] == p["impliedVolatility"] else None,
            ))
    return rows, px, len(exps)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", default=None)
    parser.add_argument("--expirations", type=int, default=3,
                         help="how many nearest real expirations to snapshot per ticker")
    args = parser.parse_args()

    tickers = args.tickers or DEFAULT_TICKERS
    conn = sqlite3.connect(DB_PATH)
    ensure_table(conn)

    total_rows = 0
    for ticker in tickers:
        try:
            rows, px, n_exps = collect_ticker(ticker, args.expirations)
        except Exception as e:
            print(f"{ticker}: failed ({e})")
            continue
        if not rows:
            print(f"{ticker}: no options market (0 expirations)")
            continue
        conn.executemany("""
            INSERT OR REPLACE INTO options_snapshot
            (snapshot_ts, ticker, underlying_price, expiration, strike, bid, ask,
             last_price, volume, open_interest, implied_volatility)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, rows)
        conn.commit()
        total_rows += len(rows)
        print(f"{ticker}: {len(rows)} put rows across {min(n_exps, args.expirations)} expirations "
              f"(underlying=${px:.2f})")

    print(f"\nWrote {total_rows} rows to options_snapshot @ {DB_PATH}")


if __name__ == "__main__":
    main()
