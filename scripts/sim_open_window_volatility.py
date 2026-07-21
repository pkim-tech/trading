"""Real intraday price-drift check for the live daemon's four signal-reaction
windows -- built to calibrate the sizing pad for automated bar-close/open-check
BUY order placement (see docs/backlog_cache.md's Part 3 follow-on discussion,
2026-07-21). Not a backtest simulation -- pulls real yfinance 1-minute bars
(only ~7-8 trading days of history available) and measures how far price
wanders, per ticker per day, from the specific print each window is reacting
to.

Four windows, matching active_signals.py's real polling structure
(_OPEN_CHECK_WINDOWS, _SIGNAL_WINDOWS):
  morning_open  -- entry_timing='open_check' nodes' 9:31-9:40 poll, reacting
                   to the 9:30:00 open print (the live proxy for the backtest's
                   literal hourly-bar Open). Drift measured from that print
                   over 9:30-9:40.
  midday_open   -- same mechanism, second open_check window (14:31-14:40),
                   reacting to the 14:30:00 print. Drift over 14:30-14:40.
  morning_close -- entry_timing='close' nodes' 10:25-10:40 signal window,
                   reacting to the 9:30-10:30 bar's Close (~10:30:00 print).
                   Drift over 10:25-10:40.
  afternoon_close -- second signal window (15:25-15:40), reacting to the
                   14:30-15:30 bar's Close (~15:30:00 print). Drift over
                   15:25-15:40.

For each window: range_pct (day's high-low span over the window, relative to
the reference print) and max_dev_pct (largest single-direction move away from
the reference print) -- max_dev_pct is the more direct pad input, since a
market/trailing-buy order only cares about adverse drift in one direction.

Usage:
    .venv/bin/python scripts/sim_open_window_volatility.py                 # all watchlist tickers
    .venv/bin/python scripts/sim_open_window_volatility.py --tickers HIBL KORU
"""
import argparse
from pathlib import Path

import pandas as pd
import yfinance as yf

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "output"

WINDOWS = {
    "morning_open":    ("09:30", "09:40"),
    "midday_open":     ("14:30", "14:40"),
    "morning_close":   ("10:25", "10:40"),
    "afternoon_close": ("15:25", "15:40"),
}
# Reference print for each window: the FIRST minute of the window for the two
# open_check windows (the real open print itself); the bar this window is
# REACTING to for the two close windows -- the prior fully-closed hourly bar,
# i.e. the print at the window's own start time still works since 10:25-10:40
# reacts to the 9:30-10:30 bar and 10:30:00 is inside 10:25-10:40's own span
# only once the bar has closed. We use the price at the window's start as the
# reference for morning/midday_open (true open print) and separately pull the
# 10:30:00 / 15:30:00 print for the close windows since that's the print the
# signal actually reacts to, not 10:25:00.
CLOSE_REFERENCE_TIME = {
    "morning_close":   "10:30",
    "afternoon_close": "15:30",
}


def _fetch_1m(ticker):
    df = yf.download(ticker, period="7d", interval="1m", progress=False, auto_adjust=False)
    if df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = df.index.tz_convert("America/New_York")
    return df


def _window_stats(df, window_name, start, end):
    df = df.copy()
    df["date"] = df.index.date
    ref_time = CLOSE_REFERENCE_TIME.get(window_name, start)
    rows = []
    for date, day_df in df.groupby("date"):
        win_df = day_df.between_time(start, end)
        if win_df.empty:
            continue
        ref_df = day_df.between_time(ref_time, ref_time)
        if ref_df.empty:
            # reference print not available this day (early close, missing bar) -- skip
            continue
        ref_px = float(ref_df.iloc[0]["Open"])
        hi = float(win_df["High"].max())
        lo = float(win_df["Low"].min())
        range_pct = (hi - lo) / ref_px * 100
        max_dev_pct = max(hi - ref_px, ref_px - lo) / ref_px * 100
        rows.append((date, ref_px, hi, lo, range_pct, max_dev_pct))
    if not rows:
        return None
    return pd.DataFrame(rows, columns=["date", "ref_px", "hi", "lo", "range_pct", "max_dev_pct"])


DRIFT_PROFILE_OFFSETS = [1, 2, 3, 5, 10]  # minutes after the 9:30:00 open print


def _open_drift_profile(df):
    """Minute-by-minute drift accumulation from the 9:30:00 print -- tells us
    whether morning_open's volatility is mostly instantaneous (opening-print
    noise, unaffected by poll speed) or builds up gradually over the window
    (in which case catching the signal earlier in the window meaningfully
    reduces exposure)."""
    df = df.copy()
    df["date"] = df.index.date
    rows = []
    for date, day_df in df.groupby("date"):
        open_row = day_df.between_time("09:30", "09:30")
        if open_row.empty:
            continue
        open_px = float(open_row.iloc[0]["Open"])
        row = {"date": date}
        for m in DRIFT_PROFILE_OFFSETS:
            end_t = f"09:{30+m:02d}"
            sub = day_df.between_time("09:30", end_t)
            if sub.empty:
                row[f"dev_{m}m"] = None
                continue
            hi = float(sub["High"].max())
            lo = float(sub["Low"].min())
            row[f"dev_{m}m"] = max(hi - open_px, open_px - lo) / open_px * 100
        rows.append(row)
    if not rows:
        return None
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", default=None)
    args = parser.parse_args()

    if args.tickers:
        tickers = args.tickers
    else:
        tickers = sorted({n["ticker"] for n in db.get_watchlist()})

    summary_rows = []
    for ticker in tickers:
        print(f"\n=== {ticker} ===")
        df = _fetch_1m(ticker)
        if df is None:
            print("  no data")
            continue
        try:
            adv = yf.Ticker(ticker).fast_info.get("tenDayAverageVolume")
            last_price = yf.Ticker(ticker).fast_info.last_price
        except Exception:
            adv, last_price = None, None
        adv_dollars = (adv or 0) * (last_price or 0)

        for window_name, (start, end) in WINDOWS.items():
            stats = _window_stats(df, window_name, start, end)
            if stats is None:
                print(f"  {window_name}: no data")
                continue
            mean_range = stats["range_pct"].mean()
            max_range = stats["range_pct"].max()
            mean_dev = stats["max_dev_pct"].mean()
            max_dev = stats["max_dev_pct"].max()
            print(f"  {window_name:<16} n={len(stats):<2} mean_range={mean_range:5.2f}%  "
                  f"max_range={max_range:5.2f}%  mean_dev={mean_dev:5.2f}%  max_dev={max_dev:5.2f}%")
            summary_rows.append({
                "ticker": ticker, "window": window_name, "n_days": len(stats),
                "mean_range_pct": mean_range, "max_range_pct": max_range,
                "mean_max_dev_pct": mean_dev, "max_max_dev_pct": max_dev,
                "avg_vol_10d": adv, "last_price": last_price, "adv_dollars": adv_dollars,
            })

    if not summary_rows:
        print("No data collected.")
        return

    out = pd.DataFrame(summary_rows)
    OUTPUT_DIR.mkdir(exist_ok=True)
    csv_path = OUTPUT_DIR / "open_window_volatility_summary.csv"
    out.to_csv(csv_path, index=False)
    print(f"\nWrote {csv_path}")

    print("\n=== Morning-open drift-accumulation profile (mean dev_pct at N min after 9:30:00 print) ===")
    profile_rows = []
    for ticker in tickers:
        df = _fetch_1m(ticker)
        if df is None:
            continue
        prof = _open_drift_profile(df)
        if prof is None:
            continue
        means = {f"dev_{m}m": prof[f"dev_{m}m"].mean() for m in DRIFT_PROFILE_OFFSETS}
        print(f"  {ticker:6s} " + "  ".join(f"{m}m={means[f'dev_{m}m']:.2f}%" for m in DRIFT_PROFILE_OFFSETS))
        profile_rows.append({"ticker": ticker, **means})

    if profile_rows:
        prof_out = pd.DataFrame(profile_rows)
        prof_csv_path = OUTPUT_DIR / "open_drift_profile.csv"
        prof_out.to_csv(prof_csv_path, index=False)
        print(f"\nWrote {prof_csv_path}")


if __name__ == "__main__":
    main()
