"""Builds the derived, dividend-adjusted 1-SECOND series from real, already-cached
raw 1s data on disk (cache/research/second_data/{ticker}_1s.csv) -- the second-
level sibling of scripts/build_massive_hourly_derived.py's hourly/minute legs,
built 2026-08-29 for the 16 tickers that already have cached raw 1s data (checked
directly: BTCZ, CWEB, DFEN, EDC, ETHU, FAS, GDXU, GUSH, JNUG, NUGT, OILU, ROM,
TECL, TNA, UGL, WEBL -- SOXL has its own separate 1s pipeline, Phase5's
load_seconds(), untouched by this script; the other ~29 tickers with no cached raw
1s data are out of scope for this build, a separate future decision).

Differences from build_massive_hourly_derived.py's build_ticker():
  1. Reads raw data from cache/research/second_data/{ticker}_1s.csv -- already on
     disk, NO Massive API fetch needed (columns already named
     Open/High/Low/Close/Volume/VWAP/NumTrades; timestamp column carries an
     explicit UTC offset per row, e.g. "2026-08-24 19:31:07-04:00" -- confirmed by
     reading real rows across BTCZ/TNA/GUSH/TECL directly before writing this).
  2. Reuses fetch_dividends()/apply_dividend_adjustment() from
     build_massive_hourly_derived.py DIRECTLY (imported, not reimplemented) --
     dividend-history-level functions, apply identically regardless of bar
     granularity.
  3. NO spike-correction step -- that's specific to the hourly build's cross-check
     against Yahoo hourly data; there's no Yahoo-second reference to correct
     against, and it's out of this task's scope. The `corrected` column is still
     written (always 0) purely for column-shape consistency with the sibling
     massive_hourly_derived/massive_minute_derived tables.
  4. Filters to regular session (09:30-16:00 ET) before persisting, matching
     massive_minute_derived's own convention -- the raw 1s CSVs include
     extended-hours ticks (confirmed: TECL's raw file has rows past 19:30 ET) that
     no real consumer of a derived series in this project currently reads.
  5. Writes under massive_second_derived's OWN independent build_id sequence
     (db_cache.record_massive_second_build / massive_second_derived_builds) --
     NOT shared with massive_hourly_derived_builds/massive_minute_derived_builds.
     The hourly/minute legs share one build_id because build_massive_hourly_
     derived.py produces both from the same Massive-minute-API pull in one pass;
     the raw 1s data here is a wholly separate source (pre-fetched CSVs, not
     derived from that same pull), so there's no equivalent "same build" concept
     to preserve.

fetch_dividends() still runs for real here (not skippable) -- it raises on failure
per its own "fail loud, don't silently write unadjusted data" convention, since
dividend adjustment is the entire point of this build.

Usage:
    .venv/bin/python scripts/build_massive_second_derived.py --tickers BTCZ
    .venv/bin/python scripts/build_massive_second_derived.py --tickers BTCZ TNA
    .venv/bin/python scripts/build_massive_second_derived.py --all   # every cached 1s-data ticker
"""
import argparse
import glob
import os
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import db_cache
from build_massive_hourly_derived import fetch_dividends, apply_dividend_adjustment

SECOND_DIR = ROOT / "cache" / "research" / "second_data"
BUILD_LABEL = "as of 2026-08-29"  # vintage label for this build -- bump the date on any future full rerun


def build_ticker(ticker):
    raw_path = SECOND_DIR / f"{ticker}_1s.csv"
    if not raw_path.exists():
        print(f"{ticker}: no cached 1s data, skipping")
        return False

    raw_pulled_at = pd.Timestamp(os.path.getmtime(raw_path), unit="s").strftime("%Y-%m-%d %H:%M:%S")

    dfs = pd.read_csv(raw_path)
    # timestamp column already carries an explicit per-row UTC offset (e.g.
    # "...-04:00" / "...-05:00" across DST) -- utc=True correctly interprets each
    # row's own offset before converting to a uniform US/Eastern, tz-naive index,
    # same convention as build_massive_hourly_derived.py's minute-CSV read.
    dfs["timestamp"] = pd.to_datetime(dfs["timestamp"], utc=True).dt.tz_convert("US/Eastern").dt.tz_localize(None)
    dfs = dfs.set_index("timestamp").sort_index()
    raw_data_start = dfs.index.min().strftime("%Y-%m-%d")
    raw_data_end = dfs.index.max().strftime("%Y-%m-%d")

    divs = fetch_dividends(ticker)  # raises on failure -- fail loud, don't silently write unadjusted data
    print(f"{ticker}: {len(divs)} dividend records")
    dividend_asof = max((d["ex_dividend_date"] for d in divs), default=None)

    dfs_adj = apply_dividend_adjustment(dfs, divs)

    # Regular-session-only (09:30-16:00 ET) -- see module docstring point 4.
    t = dfs_adj.index.time
    session = dfs_adj.loc[(t >= pd.Timestamp("09:30").time()) & (t < pd.Timestamp("16:00").time())]

    with sqlite3.connect(db_cache.DB_PATH) as conn:
        build_id = db_cache.record_massive_second_build(
            ticker, BUILD_LABEL, raw_pulled_at, raw_data_start, raw_data_end,
            dividend_asof, len(session), correction_count=0, conn=conn)
        db_cache.write_massive_second_derived(ticker, build_id, session, conn=conn)

    print(f"{ticker}: wrote {len(session)} second bars "
          f"({session.index.min()} .. {session.index.max()}), build_id={build_id}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=None)
    ap.add_argument("--all", action="store_true", help="process every ticker with cached raw 1s data on disk")
    args = ap.parse_args()

    if not os.environ.get("MASSIVE_API_KEY"):
        print("MASSIVE_API_KEY not found in .env", file=sys.stderr)
        sys.exit(1)

    if args.all:
        tickers = sorted(Path(f).stem.replace("_1s", "") for f in glob.glob(str(SECOND_DIR / "*_1s.csv")))
    elif args.tickers:
        tickers = args.tickers
    else:
        print("Specify --tickers T [T ...] or --all", file=sys.stderr)
        sys.exit(1)

    print(f"Building derived second for {len(tickers)} ticker(s): {tickers}")
    ok, skipped, errored = 0, 0, []
    for i, t in enumerate(tickers):
        t0 = time.time()
        try:
            if build_ticker(t):
                ok += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"{t}: ERROR {e!r}", file=sys.stderr)
            errored.append(t)
        print(f"[{i+1}/{len(tickers)}] {t} done in {time.time()-t0:.1f}s")

    print(f"\nDone: {ok} built, {skipped} skipped (no cached 1s data), "
          f"{len(errored)} errored out of {len(tickers)}")
    if errored:
        print(f"Errored tickers (re-run individually once fixed): {errored}")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
