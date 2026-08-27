"""Checks whether real 1-minute bar range (High-Low)/price is small relative
to each v6 promotion candidate's actual stop-loss width, during the real
signal-reaction windows (open_check: 9:31-9:40/14:31-14:40 ET; bar-close
fallback: 10:25-10:40/15:25-15:40 ET) -- see conversation 2026-08-24
("promoter" session) re: whether 1-minute bars leave enough residual
intrabar ambiguity to matter, without needing sub-minute (10s/1s/tick) data.

This is a hard bound, not a model: (High-Low)/price for a real 1-minute bar
is the maximum possible drift that bar's own ambiguity could hide, no
Low-before-High/High-before-Low guess required. If this is small relative to
a node's fixed_sl, residual 1-minute ambiguity can't plausibly explain a
material fraction of that node's edge; if it's comparable, that's a real
signal finer data would matter.

Uses cache/research/minute_data/{ticker}_1m.csv (Massive.com, already
fetched, 5yr for most tickers) -- no new data needed.

Usage: .venv/bin/python scripts/check_1min_range_vs_stop.py
"""
import sys
from datetime import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MINUTE_DIR = Path(__file__).resolve().parent.parent / "cache" / "research" / "minute_data"

# (ticker, fixed_sl_pct) -- from scripts/promote_v6_2026_08_23_batch1.py's
# real BATCH list, the actual v6 promotion candidates and their real stop widths.
CANDIDATES = [
    ("AGQ", 2.0),
    ("GDXU", 1.0),
    ("HIBL", 3.0),
    ("JNUG", 3.0),
    ("KORU", 3.0),
    ("LABU", 5.0),
    ("NUGT", 2.0),
    ("UGL", 2.0),
]

WINDOWS = {
    "open_check_morning": (time(9, 31), time(9, 40)),
    "open_check_midday": (time(14, 31), time(14, 40)),
    "bar_close_morning": (time(10, 25), time(10, 40)),
    "bar_close_afternoon": (time(15, 25), time(15, 40)),
}


def load_minute_ranges(ticker):
    path = MINUTE_DIR / f"{ticker}_1m.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("America/New_York")
    df["t"] = df["timestamp"].dt.time
    df["range_pct"] = (df["High"] - df["Low"]) / df["Close"] * 100
    return df


def main():
    print(f"{'ticker':<6} {'window':<20} {'n':>7} {'mean%':>7} {'median%':>8} {'p90%':>7} {'p95%':>7} {'p99%':>7} {'max%':>7}  fixed_sl%")
    for ticker, fixed_sl in CANDIDATES:
        df = load_minute_ranges(ticker)
        if df is None:
            print(f"{ticker:<6} -- no minute data on file --")
            continue
        for wname, (start, end) in WINDOWS.items():
            mask = (df["t"] >= start) & (df["t"] <= end)
            sub = df.loc[mask, "range_pct"]
            if sub.empty:
                continue
            print(f"{ticker:<6} {wname:<20} {len(sub):>7} {sub.mean():>7.3f} {sub.median():>8.3f} "
                  f"{sub.quantile(0.90):>7.3f} {sub.quantile(0.95):>7.3f} {sub.quantile(0.99):>7.3f} "
                  f"{sub.max():>7.3f}  {fixed_sl:>8.2f}")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
