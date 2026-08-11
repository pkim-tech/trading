"""Extends an existing cached _1h.csv file's history BACKWARD to whatever yfinance's
free hourly endpoint actually has available right now, without disturbing already-
cached rows. Built 2026-08-11 after finding 215 of 1475 cached tickers were silently
under-backfilled (~13 months of hourly history captured when yfinance actually had
~35 months available for free) -- see trading_incidents id=4 for the investigation.

Does NOT delete or re-bootstrap the file (that would risk losing the split-guard
rescale audit trail data_manager.py's incremental path already applied, and could
reintroduce a fresh scale-mismatch bug). Instead: fetches a fresh period="730d" (or
longer, if available) pull, then merges with the existing cache using the same
concat + dedupe-by-timestamp approach data_manager.fetch_live_data_smart's own
incremental path already uses -- EXISTING local rows win on any timestamp overlap,
new rows only ever extend the front of the history backward.

Usage: .venv/bin/python scripts/backfill_hourly_history.py TICKER [TICKER ...] [--dry-run]
"""
import argparse
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "research"


def backfill_one(ticker, dry_run=False):
    cache_path = CACHE_DIR / f"{ticker}_1h.csv"
    if not cache_path.exists():
        print(f"{ticker}: no existing cache file -- not this script's job (use the normal bootstrap path)")
        return

    df_local = pd.read_csv(cache_path, index_col=0, parse_dates=True)
    df_local.index = pd.to_datetime(df_local.index).tz_localize(None)
    df_local = df_local.sort_index()
    old_start = df_local.index.min()

    df_fresh = yf.download(ticker, period="730d", interval="1h", auto_adjust=True, progress=False)
    if df_fresh.empty:
        print(f"{ticker}: yfinance returned nothing, skipping")
        return
    if isinstance(df_fresh.columns, pd.MultiIndex):
        df_fresh.columns = df_fresh.columns.get_level_values(0)
    df_fresh.index = pd.to_datetime(df_fresh.index).tz_localize(None)
    df_fresh.index.name = df_local.index.name

    fresh_start = df_fresh.index.min()
    if fresh_start >= old_start:
        print(f"{ticker}: fresh pull starts {fresh_start.date()}, no earlier than cached {old_start.date()} -- nothing to backfill")
        return

    # existing local rows win on any overlapping timestamp -- never let a fresh
    # re-pull silently override rows a prior split-guard rescale may have already
    # corrected (same reasoning data_manager.py's own incremental path documents).
    df_combined = pd.concat([df_fresh, df_local], axis=0)
    df_combined = df_combined[~df_combined.index.duplicated(keep='last')]
    df_combined = df_combined.sort_index()

    new_start = df_combined.index.min()
    added_rows = len(df_combined) - len(df_local)
    print(f"{ticker}: {old_start.date()} -> {new_start.date()} ({added_rows} new rows, "
          f"{len(df_local)} -> {len(df_combined)} total)")

    if not dry_run:
        df_combined.to_csv(cache_path)
        print(f"{ticker}: wrote {cache_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for t in args.tickers:
        backfill_one(t, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
