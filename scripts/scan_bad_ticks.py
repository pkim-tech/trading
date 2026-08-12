"""Scans cached hourly CSVs for single-bar spike-and-full-recovery patterns
-- the shape of a bad data tick, not a real price move (a genuine leveraged-
ETF crash doesn't fully round-trip back to the pre-crash level on the very
next bar). Built 2026-08-11 after DFEN's v5.1 "winner" (80,812% alpha) turned
out to be entirely one fabricated trade off a single bad tick
(2024-06-03 09:30: Close/Low=$0.27 vs a $28.75 Open/neighbors) -- run this
BEFORE trusting a sweep's results, not after chasing an outlier headline
number back to its cause one ticker at a time.

Flags a bar as suspect when it drops (or spikes) by more than --threshold
from the prior bar's Close AND the very next bar reverses most of that move
in the opposite direction -- the DFEN pattern exactly. A real crash/rip
doesn't round-trip like that.

--fix applies the same conservative correction used on DFEN: for each hit bar,
set Low=Close=Open (Open/High are trustworthy -- they matched neighboring
bars in every case checked; Low/Close are what the bad tick corrupted). This
doesn't fabricate a specific "true" price, it just neutralizes the fake
signal (the bar shows no move from Open instead of an impossible spike).
Each fix is logged to db_cache.data_mutation_log with the pre-fix row
preserved, same as the manual DFEN fix.

Usage:
    .venv/bin/python scripts/scan_bad_ticks.py [--tickers T ...] [--threshold 0.4]
        [--recovery-frac 0.7] [--fix]
    # no --tickers: scans every cache/research/*_1h.csv
"""
import argparse
import glob
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db_cache

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "research"


def scan_ticker(ticker, threshold, recovery_frac, log=True):
    """Returns (hits, date_start, date_end, row_count). Logs the scan (window
    + hits, even when empty) to db_cache.bad_tick_scan_log unless log=False --
    see that table's docstring for why the scanned window matters, not just
    the hit list."""
    path = CACHE_DIR / f"{ticker}_1h.csv"
    if not path.exists():
        return [], None, None, 0
    df = pd.read_csv(path)
    if "Datetime" not in df.columns:
        # e.g. MUB_1h.csv is actually daily data with a "Date" column despite
        # the filename -- not part of the real hourly-bar universe this scan
        # is for. Skip rather than crash the whole run.
        print(f"  {ticker}: skipped (no 'Datetime' column, unexpected schema)")
        return [], None, None, len(df)
    if len(df) < 3:
        if log:
            db_cache.log_bad_tick_scan(ticker, None, None, len(df), threshold, recovery_frac, [])
        return [], None, None, len(df)
    close = df["Close"]
    ret = close.pct_change(fill_method=None)
    next_ret = ret.shift(-1)

    hits = []
    for i in range(1, len(df) - 1):
        r, nr = ret.iloc[i], next_ret.iloc[i]
        if pd.isna(r) or pd.isna(nr):
            continue
        # Opposite-sign moves, both large, next bar reverses at least
        # recovery_frac of the drop/spike -- a real round-trip-to-glitch shape.
        if abs(r) < threshold or (r > 0) == (nr > 0):
            continue
        if abs(nr) < abs(r) * recovery_frac:
            continue
        hits.append({
            "ticker": ticker,
            "bar_time": df["Datetime"].iloc[i],
            "prior_close": float(close.iloc[i - 1]),
            "bar_close": float(close.iloc[i]),
            "bar_low": float(df["Low"].iloc[i]),
            "bar_high": float(df["High"].iloc[i]),
            "bar_open": float(df["Open"].iloc[i]),
            "next_close": float(close.iloc[i + 1]),
            "ret_pct": float(r * 100),
            "next_ret_pct": float(nr * 100),
        })

    date_start, date_end = df["Datetime"].iloc[0], df["Datetime"].iloc[-1]
    if log:
        db_cache.log_bad_tick_scan(ticker, date_start, date_end, len(df),
                                    threshold, recovery_frac, hits)
    return hits, date_start, date_end, len(df)


def fix_ticker(ticker, hits):
    """Applies the conservative Low=Close=Open correction for each hit bar
    and logs it to data_mutation_log, matching the manual DFEN fix exactly."""
    path = CACHE_DIR / f"{ticker}_1h.csv"
    df = pd.read_csv(path)
    for h in hits:
        mask = df["Datetime"] == h["bar_time"]
        if mask.sum() != 1:
            print(f"  {ticker} {h['bar_time']}: skipped fix (expected 1 matching row, found {mask.sum()})")
            continue
        before = df[mask].copy()
        open_p = h["bar_open"]
        db_cache.log_data_mutation(
            ticker=ticker, factor=1.0, overlap_bar_time=h["bar_time"],
            price_before=h["bar_close"], price_after=open_p,
            notes=(
                f"Bad tick auto-fix (scan_bad_ticks.py --fix) -- {h['bar_time']} bar had "
                f"Close={h['bar_close']:.4f} (ret={h['ret_pct']:+.1f}%, next bar recovered "
                f"{h['next_ret_pct']:+.1f}%), while Open={open_p:.4f}/High={h['bar_high']:.4f} "
                f"matched neighboring bars. Set Low=Close=Open for this one bar (no fabricated "
                f"intrabar price, just removes the fake signal)."
            ),
            pre_mutation_df=before,
        )
        df.loc[mask, "Close"] = open_p
        df.loc[mask, "Low"] = open_p
        print(f"  {ticker} {h['bar_time']}: fixed (Close/Low {h['bar_close']:.4f} -> {open_p:.4f})")
    df.to_csv(path, index=False)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", nargs="+", default=None)
    p.add_argument("--threshold", type=float, default=0.4,
                    help="Minimum abs single-bar return to consider (default 0.4 = 40%%)")
    p.add_argument("--recovery-frac", type=float, default=0.7,
                    help="Next bar must reverse at least this fraction of the move (default 0.7)")
    p.add_argument("--no-log", action="store_true",
                    help="Skip recording this run to db_cache.bad_tick_scan_log (e.g. for a quick manual check)")
    p.add_argument("--fix", action="store_true",
                    help="Apply the conservative Low=Close=Open correction to every hit found, logged to data_mutation_log")
    args = p.parse_args()

    if args.tickers:
        tickers = args.tickers
    else:
        tickers = sorted(
            Path(f).stem.replace("_1h", "")
            for f in glob.glob(str(CACHE_DIR / "*_1h.csv"))
        )

    all_hits = []
    windows = []
    for t in tickers:
        hits, date_start, date_end, row_count = scan_ticker(
            t, args.threshold, args.recovery_frac, log=not args.no_log)
        all_hits.extend(hits)
        if date_start is not None:
            windows.append((t, date_start, date_end, row_count))

    if windows:
        earliest_start = min(w[1] for w in windows)
        latest_end = max(w[2] for w in windows)
        print(f"Scanned {len(windows)} tickers, window {earliest_start} .. {latest_end} "
              f"(per-ticker windows vary -- logged individually to bad_tick_scan_log)"
              f"{' [not logged, --no-log]' if args.no_log else ''}.\n")

    if not all_hits:
        print(f"No suspect bad-tick round-trips found "
              f"(threshold={args.threshold:.0%}, recovery_frac={args.recovery_frac:.0%}).")
        return

    print(f"{len(all_hits)} suspect bad-tick round-trip(s) across {len(tickers)} tickers:\n")
    for h in sorted(all_hits, key=lambda x: -abs(x["ret_pct"])):
        print(
            f"  {h['ticker']:6} {h['bar_time']}  "
            f"prior_close={h['prior_close']:.4f} -> bar_close={h['bar_close']:.4f} "
            f"(low={h['bar_low']:.4f} high={h['bar_high']:.4f} open={h['bar_open']:.4f})  "
            f"ret={h['ret_pct']:+.1f}%  next_bar_close={h['next_close']:.4f} "
            f"next_ret={h['next_ret_pct']:+.1f}%"
        )

    if args.fix:
        print(f"\nApplying fixes to {len({h['ticker'] for h in all_hits})} ticker(s):")
        by_ticker = {}
        for h in all_hits:
            by_ticker.setdefault(h["ticker"], []).append(h)
        for t, hits in by_ticker.items():
            fix_ticker(t, hits)
        print("\nAny ticker fixed above needs a resweep for the version(s) it was already "
              "swept under -- existing backtest_cache rows were computed off the bad data "
              "and are now stale (same situation as the manual DFEN fix).")
        return

    # Exit 2 (not 1 -- reserve 1 for a genuine crash) when hits were found and
    # NOT fixed this run, so a caller (run_liquidity_tranches.sh) can tell
    # "found something, still needs --fix" apart from "ran cleanly" without
    # having to scrape stdout.
    sys.exit(2)


if __name__ == "__main__":
    main()
