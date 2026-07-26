"""Scan the full research-universe ticker list for real overnight gaps.

Usage:
  python scripts/gap_scan.py [--min-gap-pct 3.0] [--min-dollar-vol 1000000] [--top 20]

Read-only, no order placement, no watchlist dependency -- built to feed candidates
into the gap-resize test-seeding step (seed a synthetic pending_buys row on a
ticker that actually gapped, so signals_notify.check_gap_resize's cancel+replace
path gets exercised for real instead of waiting on the current live watchlist to
gap on its own, per the still-open 2026-07-24 backlog item). Universe is
cache/research/trading_universe.db's `tickers` table (624 liquid rows with
has_data=1), filtered by avg_vol_10d*last_price before hitting yfinance at all
to keep the batch small. Two batched yf.download calls (daily history for prior
close, 1-minute prepost-included intraday for the current/premarket price) --
not one call per ticker, which would be too slow at this scale.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_DB = ROOT / "cache" / "research" / "trading_universe.db"
_CHUNK = 100


def _load_universe(min_dollar_vol):
    c = sqlite3.connect(UNIVERSE_DB)
    rows = c.execute(
        "SELECT symbol, avg_vol_10d, last_price FROM tickers "
        "WHERE has_data=1 AND avg_vol_10d IS NOT NULL AND last_price IS NOT NULL"
    ).fetchall()
    return [sym for sym, vol, px in rows if vol * px >= min_dollar_vol]


def _chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _prev_close(daily_df, ticker, today):
    try:
        closes = daily_df[ticker]['Close'].dropna()
    except KeyError:
        return None
    closes = closes[closes.index.normalize() < today]
    return float(closes.iloc[-1]) if len(closes) else None


def _current_price(intraday_df, ticker):
    try:
        closes = intraday_df[ticker]['Close'].dropna()
    except KeyError:
        return None
    return float(closes.iloc[-1]) if len(closes) else None


def scan_gap_candidates(min_gap_pct=3.0, min_dollar_vol=1_000_000):
    tickers = _load_universe(min_dollar_vol)
    today = pd.Timestamp.now().normalize()
    results = []

    for batch in _chunked(tickers, _CHUNK):
        try:
            daily = yf.download(batch, period='5d', interval='1d', group_by='ticker',
                                 threads=True, progress=False, auto_adjust=True)
            intraday = yf.download(batch, period='1d', interval='1m', prepost=True,
                                    group_by='ticker', threads=True, progress=False, auto_adjust=True)
        except Exception as e:
            print(f"  [warn] batch download failed ({e}), skipping {len(batch)} tickers", file=sys.stderr)
            continue

        for t in batch:
            prev_close = _prev_close(daily, t, today)
            cur = _current_price(intraday, t)
            if prev_close is None or cur is None or prev_close == 0:
                continue
            gap_pct = (cur - prev_close) / prev_close * 100
            if abs(gap_pct) >= min_gap_pct:
                results.append((t, prev_close, cur, gap_pct))

    results.sort(key=lambda r: -abs(r[3]))
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-gap-pct", type=float, default=3.0)
    ap.add_argument("--min-dollar-vol", type=float, default=1_000_000)
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    results = scan_gap_candidates(args.min_gap_pct, args.min_dollar_vol)
    print(f"{len(results)} candidate(s) with |gap| >= {args.min_gap_pct}% "
          f"(universe filtered to avg $vol >= {args.min_dollar_vol:,.0f})\n")
    print(f"{'Ticker':<8} {'PrevClose':>10} {'Current':>10} {'Gap%':>8}")
    for t, prev, cur, gap in results[:args.top]:
        print(f"{t:<8} {prev:>10.2f} {cur:>10.2f} {gap:>7.2f}%")


if __name__ == '__main__':
    main()
