"""Applies real dividend adjustment (fetch_dividends/apply_dividend_adjustment,
reused from build_massive_hourly_derived.py) to already-fetched 1-second CSVs
in cache/research/second_data/ -- separate pass from the fetch itself so a
fetch failure/retry doesn't repeat the (slower) adjustment step.

Usage: .venv/bin/python scripts/apply_dividend_adjustment_batch.py --tickers T ...
"""
import argparse
import shutil
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_massive_hourly_derived import fetch_dividends, apply_dividend_adjustment

SECOND_DATA_DIR = Path(__file__).resolve().parent.parent / "cache" / "research" / "second_data"
BACKUP_DIR = SECOND_DATA_DIR / "backups"


def adjust_ticker(ticker):
    path = SECOND_DATA_DIR / f"{ticker}_1s.csv"
    if not path.exists():
        print(f"  {ticker}: no file, skipping")
        return
    divs = fetch_dividends(ticker)
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("America/New_York")
    df_idx = df.set_index(df["timestamp"].dt.tz_localize(None))[["Open", "High", "Low", "Close"]]
    adj = apply_dividend_adjustment(df_idx, divs)
    df["Open"] = adj["Open"].values
    df["High"] = adj["High"].values
    df["Low"] = adj["Low"].values
    df["Close"] = adj["Close"].values

    # Back up the pre-adjustment file before overwriting -- real precedent tonight
    # (2026-08-26, see scripts/promote_market_data_pull.py): never overwrite a
    # canonical market-data file without a way to recover the prior version if the
    # transform turns out wrong.
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    import time as _time
    backup_path = BACKUP_DIR / f"{ticker}_1s_pre_dividend_adjust_{_time.strftime('%Y%m%d_%H%M%S')}.csv"
    shutil.copy2(path, backup_path)

    df.to_csv(path, index=False)
    print(f"  {ticker}: {len(divs)} dividends applied, {len(df):,} rows saved (backup: {backup_path.name})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", required=True)
    args = ap.parse_args()
    total = len(args.tickers)
    t0 = time.time()
    for i, ticker in enumerate(args.tickers, start=1):
        elapsed = time.time() - t0
        eta = (elapsed / (i - 1) * (total - i + 1)) if i > 1 else float("nan")
        print(f"=== [{i}/{total}] {ticker}  elapsed={elapsed:.0f}s eta={eta:.0f}s ===")
        adjust_ticker(ticker)


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
