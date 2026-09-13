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
  2. Does NOT call apply_dividend_adjustment on the price data (2026-09-06 fix --
     first version of this script did, a real bug found by paired review of a
     downstream 1s-fill-resolution kernel wiring attempt: confirmed directly,
     comparing raw pre-any-adjustment second/minute prices at matching
     timestamps against the real cached historical_adjustment_factor for that
     date, e.g. SOXL 2022-03-01 raw ratio 0.97046 vs real factor 0.970479, DFEN
     2024-09-03 raw ratio 0.8089 vs real factor 0.804713 -- scripts/
     fetch_massive_second_data.py's raw pull already requests Massive's seconds-
     aggregates endpoint with adjusted=true, and THAT endpoint's adjusted=true
     bakes in dividend adjustment too (unlike the minute/hourly aggregates
     endpoint, confirmed split-only/not-dividend per build_massive_hourly_
     derived.py's own docstring -- that confirmation was never re-verified for
     the seconds endpoint specifically, and turned out not to hold there).
     Re-applying apply_dividend_adjustment on top of already-adjusted raw data
     was double-applying the same factor -- silently corrupted every dividend-
     paying ticker's second-level series (near-zero-dividend tickers like GDXU/
     AGQ happened to look fine, since double-applying a ~1.0 factor is still
     ~1.0). fetch_dividends() still runs (not skippable, fails loud) -- and its
     result IS still fed into apply_dividend_adjustment, but restricted to only
     ex-dividend dates AFTER the raw pull (see build_ticker's own "Residual
     top-up" comment) -- Massive's own source-side adjustment only reflects
     dividends known to them as of that pull, so any later real dividend still
     needs our own incremental correction, or this table would silently drift
     stale relative to massive_hourly_derived/massive_minute_derived (which get
     freshly re-adjusted from massive_dividends_raw on every rebuild) the next
     time a new dividend lands -- the exact bug just fixed, recurring through a
     different door. dividend_asof below is genuinely meaningful again once this
     residual top-up is applied (this table's data really is fully adjusted
     through that date, not just Massive's own pull-time snapshot).
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

    # Chunked, per-chunk-session-pre-filtered read (2026-09-06, memory-safety fix
    # -- a single-shot pd.read_csv on SOXL's real 2.6GB raw file repeatedly OOM-
    # killed this build on this project's 15GB box; same fix shape as scripts/
    # poc_1s_cliffbox_check.py's own load_seconds_lean()). Filters to regular
    # session PER CHUNK before concatenation, so peak memory is bounded by one
    # chunk's raw size plus the already-filtered running total, not the full raw
    # file. raw_data_start/end still reflect the FULL raw range (including
    # extended-hours rows, matching this script's original semantics), tracked
    # via a running min/max across every chunk, not just the session-filtered
    # rows.
    parts = []
    raw_min, raw_max = None, None
    for chunk in pd.read_csv(raw_path, chunksize=250_000):
        # timestamp column already carries an explicit per-row UTC offset (e.g.
        # "...-04:00" / "...-05:00" across DST) -- utc=True correctly interprets
        # each row's own offset before converting to a uniform US/Eastern,
        # tz-naive index, same convention as build_massive_hourly_derived.py's
        # minute-CSV read.
        ts = pd.to_datetime(chunk["timestamp"], utc=True).dt.tz_convert("US/Eastern").dt.tz_localize(None)
        chunk = chunk.set_index(ts).sort_index()
        chunk_min, chunk_max = chunk.index.min(), chunk.index.max()
        raw_min = chunk_min if raw_min is None else min(raw_min, chunk_min)
        raw_max = chunk_max if raw_max is None else max(raw_max, chunk_max)
        t = chunk.index.time
        parts.append(chunk.loc[(t >= pd.Timestamp("09:30").time()) & (t < pd.Timestamp("16:00").time())])
    session = pd.concat(parts).sort_index()
    raw_data_start = raw_min.strftime("%Y-%m-%d")
    raw_data_end = raw_max.strftime("%Y-%m-%d")

    # fetch_dividends() still runs (not skippable, raises on failure) purely for
    # dividend_asof provenance below -- 2026-09-06 fix: NOT applied to the price
    # data (see module docstring point 2's replacement) -- Massive's own seconds-
    # aggregates pull (scripts/fetch_massive_second_data.py's adjusted=true param)
    # already bakes in dividend adjustment at the source, unlike the minute/hourly
    # aggregates endpoint (confirmed split-only, not dividend, per build_massive_
    # hourly_derived.py's own docstring) -- applying apply_dividend_adjustment on
    # top of already-adjusted second data was double-applying the same factor.
    divs = fetch_dividends(ticker)
    print(f"{ticker}: {len(divs)} dividend records")

    # Residual top-up (2026-09-06, single-Opus-review HIGH finding, added same
    # session as the double-adjustment fix above): Massive's own seconds-endpoint
    # adjustment only reflects real ex-dividend events known to THEM as of this
    # raw pull -- any ex-dividend date AFTER raw_pulled_at needs its own
    # incremental correction here, or this table's basis would silently drift
    # stale relative to massive_hourly_derived/massive_minute_derived (which get
    # freshly re-adjusted from massive_dividends_raw on every rebuild) the next
    # time either leg is rebuilt after a new real dividend -- the exact bug shape
    # just fixed above, recurring through a different door. Restricting the
    # dividend list fed to apply_dividend_adjustment to ONLY post-pull ex-div
    # dates means every pre-pull price's nearest-future-dividend lookup finds
    # nothing (factor defaults to 1.0, no-op) -- only prices affected by a
    # genuinely-new post-pull dividend get a real correction.
    raw_pulled_at_ts = pd.Timestamp(raw_pulled_at)
    residual_divs = [d for d in divs if pd.Timestamp(d["ex_dividend_date"]) > raw_pulled_at_ts]
    if residual_divs:
        print(f"{ticker}: {len(residual_divs)} dividend(s) after raw pull ({raw_pulled_at}) "
              f"-- applying residual top-up adjustment")
        session = apply_dividend_adjustment(session, residual_divs)
    dividend_asof = max((d["ex_dividend_date"] for d in divs), default=None)

    with sqlite3.connect(db_cache.TICKDATA_DB_PATH) as conn:
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
