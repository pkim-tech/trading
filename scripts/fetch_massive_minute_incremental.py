"""Incremental sibling of scripts/fetch_massive_minute_data.py (Task #11, 2026-08-31):
built to close the "minute/second data has no automated refresh" backlog item
(docs/backlog_cache.md, raised 2026-08-26) -- real trigger: UGL's massive_hourly_
derived was 10 days stale tonight, causing a false PHANTOM in evening_status.py.

Why this exists instead of just cron-ing the existing fetch script: confirmed
directly (not assumed) that fetch_massive_minute_data.py always re-fetches the
FULL requested range from the API -- there is no "since last cached bar" mode.
Cron-ing that as-is for 14 real tickers would mean re-pulling ~5yr of 1-minute
history every night (huge API-call volume, real time cost) just to catch a few
days of new bars. Worse: promote_market_data_pull.py (the only thing allowed to
write to canonical) does a straight copy2 REPLACE, not a merge -- it only
refuses a NARROWER start date. A naive "pull just the last few days" approach
would either get refused outright or, with --force, silently REPLACE the whole
canonical archive with a few days of data. Neither existing script is safe to
cron as-is for a delta refresh.

This script closes that gap WITHOUT modifying either settled script (per this
project's "isolate new code from settled paths" convention -- both fetch_
massive_minute_data.py and promote_market_data_pull.py stay exactly as they
are, still directly usable for a full/forced re-pull if ever needed):
  1. Reads the canonical CSV's real last timestamp per ticker (tail-seek, same
     technique as promote_market_data_pull.py's _first_last_dates -- these
     files run 70-80MB, a full read just to find the last line would be real,
     avoidable cost run nightly).
  2. Fetches only from (last_ts.date() - OVERLAP_DAYS) through today, via
     fetch_massive_minute_data.fetch_ticker() -- the exact same API-calling
     primitive the full-pull script uses, imported and reused, not
     reimplemented. OVERLAP_DAYS re-pulls a few days that are already cached,
     deliberately -- catches any late upstream corrections/backfills to
     recent bars, same rationale as fetch_massive_second_data.py's own
     streaming design note about not trusting a single as-of-yesterday pull
     as final.
  3. Merges the new rows into the FULL existing canonical DataFrame (concat +
     drop_duplicates on timestamp, keep='last' so the fresh pull wins for any
     overlap-window bar that changed upstream, then sorted) and writes the
     MERGED full-range result to pulls/{ticker}_1m_{true_start}_{end}.csv --
     same start date as canonical, so promote_market_data_pull.py's existing
     narrowing guard sees this as a normal (non-narrowing) promotion and
     behaves completely unchanged. This script never writes to canonical
     itself; promote_market_data_pull.py is still the only thing that does.
  4. If a ticker has no canonical file yet, falls back to a full pull via
     fetch_ticker() from CANONICAL_FALLBACK_START (matches fetch_massive_
     minute_data.py's own effective full-history usage, e.g. --start-date
     2021-08-27) instead of erroring -- a genuinely new real ticker should
     still get real coverage from this pipeline, not a silent skip.

Usage:
    .venv/bin/python scripts/fetch_massive_minute_incremental.py
        # default: every real capital-at-stake ticker (signals_db.get_watchlist
        # + signals_helpers.has_capital_at_stake -- the SAME selection
        # check_massive_hourly_derived_freshness uses, so this script and that
        # invariant check can never scope-drift against each other)
    .venv/bin/python scripts/fetch_massive_minute_incremental.py --tickers UGL AGQ

Does NOT promote -- staged pulls still require the existing scripts/
promote_market_data_pull.py step (or scripts/nightly_market_data_refresh.sh,
which chains this + promote + build_massive_hourly_derived.py +
promote_derived_build.py end to end)."""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.fetch_massive_minute_data import fetch_ticker, OUT_DIR, PULLS_DIR

OVERLAP_DAYS = 3
CANONICAL_FALLBACK_START = date(2021, 8, 27)  # matches fetch_massive_minute_data.py's
                                                # own real full-history usage precedent


def _canonical_path(ticker):
    return OUT_DIR / f"{ticker}_1m.csv"


def _read_last_timestamp(path: Path):
    """Tail-seek, same technique as promote_market_data_pull.py's _first_last_dates --
    avoids a full read of a 70-80MB file just to find the last line. Returns a
    real datetime.date (not just a date string) since the caller needs to do
    arithmetic on it (subtract OVERLAP_DAYS)."""
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        block = min(size, 4096)
        f.seek(size - block)
        tail = f.read().decode(errors="replace")
    last_line = [l for l in tail.splitlines() if l.strip()][-1]
    ts_str = last_line.split(",", 1)[0]
    return pd.Timestamp(ts_str).date()


def _rows_to_df(rows):
    """Same transform fetch_massive_minute_data.py's own main() applies -- kept as
    a short, separate copy here rather than importing/refactoring that script's
    inline main() logic, since factoring a settled, already-in-production script
    just to share ~6 lines is a worse tradeoff than a small, obviously-correct
    duplication (isolate-new-code-from-settled-paths convention)."""
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert("America/New_York")
    df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close",
                             "v": "Volume", "vw": "VWAP", "n": "NumTrades"})
    return df[["timestamp", "Open", "High", "Low", "Close", "Volume", "VWAP", "NumTrades"]]


def refresh_ticker(ticker, overlap_days=OVERLAP_DAYS):
    """Returns the path to the staged (not yet promoted) merged pull, or None if
    there was nothing new to fetch. Never touches canonical -- caller/cron job
    still runs promote_market_data_pull.py as a separate, explicit step."""
    canonical_path = _canonical_path(ticker)
    end = date.today()

    if not canonical_path.exists():
        print(f"  {ticker}: no canonical file yet -- full pull from {CANONICAL_FALLBACK_START}")
        rows = fetch_ticker(ticker, CANONICAL_FALLBACK_START, end)
        if not rows:
            print(f"  {ticker}: no data returned, skipping")
            return None
        merged = _rows_to_df(rows).drop_duplicates(subset="timestamp").sort_values("timestamp")
    else:
        last_ts = _read_last_timestamp(canonical_path)
        fetch_start = last_ts - timedelta(days=overlap_days)
        print(f"  {ticker}: canonical through {last_ts}, fetching {fetch_start} -> {end} "
              f"({overlap_days}-day overlap)")
        rows = fetch_ticker(ticker, fetch_start, end)
        if not rows:
            print(f"  {ticker}: no new bars returned (already current)")
            return None
        new_df = _rows_to_df(rows)
        canonical_df = pd.read_csv(canonical_path, parse_dates=["timestamp"])
        merged = (pd.concat([canonical_df, new_df], ignore_index=True)
                  .drop_duplicates(subset="timestamp", keep="last")
                  .sort_values("timestamp"))
        if len(merged) == len(canonical_df):
            print(f"  {ticker}: fetch returned {len(new_df)} rows, all already covered "
                  f"(no new bars) -- not staging")
            return None

    true_start = merged["timestamp"].min().date()
    PULLS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PULLS_DIR / f"{ticker}_1m_{true_start}_{end}.csv"
    merged.to_csv(out_path, index=False)
    print(f"  {ticker}: staged {len(merged):,} rows ({true_start} -> {end}) to {out_path}")
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=None,
                     help="default: every real capital-at-stake ticker "
                          "(signals_db.get_watchlist + has_capital_at_stake)")
    ap.add_argument("--overlap-days", type=int, default=OVERLAP_DAYS)
    args = ap.parse_args()

    if args.tickers:
        tickers = args.tickers
    else:
        import signals_db as db
        import signals_helpers as helpers
        tickers = sorted({n["ticker"] for n in db.get_watchlist()
                           if helpers.has_capital_at_stake(n)})

    print(f"Incremental minute-data refresh: {len(tickers)} ticker(s): {tickers}")
    staged = []
    for ticker in tickers:
        print(f"=== {ticker} ===")
        path = refresh_ticker(ticker, overlap_days=args.overlap_days)
        if path:
            staged.append(ticker)

    print(f"\n{len(staged)}/{len(tickers)} ticker(s) staged with new data: {staged}")
    if staged:
        print("Staged only -- run scripts/promote_market_data_pull.py --ticker T --kind minute "
              "for each, or scripts/nightly_market_data_refresh.sh for the full chained pipeline.")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
