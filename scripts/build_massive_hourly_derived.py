"""Builds the derived, dividend-adjusted hourly series from real Massive.com minute
data -- this is what the GT/hourly kernels should read for a ticker's full history,
per the 2026-08-22 pipeline decision: Massive (minute, resampled) is the sole source
across the full ~5yr range; Yahoo hourly stays completely separate, used only as an
independent audit signal, never consumed directly. No stitching -- one uniform
resample method across the whole range.

Pipeline (per ticker):
  1. Load cached minute data (cache/research/minute_data/{ticker}_1m.csv) -- split-
     adjusted-only per Massive's own docs (their aggregates `adjusted=true` flag
     covers splits, NOT dividends -- confirmed live against massive.com/knowledge-base
     2026-08-22).
  2. Fetch (or reuse cached) real dividend history via Massive's /stocks/v1/dividends
     endpoint, cached in db_cache.massive_dividends_raw (raw, untouched, never
     mutated). Apply historical_adjustment_factor per Massive's documented rule: for
     a price on date D, use the first dividend whose ex_dividend_date is after D
     (cumulative -- only the nearest future one is ever applied).
  3. Filter to regular session (09:30-16:00 ET), resample to hourly bars anchored at
     :30 (matches the real live-daemon bar convention), full available range.
  4. Two independent spike-correction detectors (see correct_spikes()'s own
     docstring for full detail -- this summary was found stale/contradicting the
     actual code by a second paired-review round, 2026-08-22, after the first
     round's fix changed the design but not this docstring):
       - detect_close_roundtrips(): a Close-based fabricated-round-trip detector
         (the DFEN, 2026-08-11 incident shape), using LOG returns so both upward
         and downward spikes are caught symmetrically at any magnitude -- always
         corrected in place (neutralized to Open).
       - Wick-based (High/Low far beyond the bar's own O/C and recent typical
         range, with Open/Close unaffected -- a DIFFERENT shape the round-trip
         detector can't catch). Yahoo is used ONLY as a binary detector here (does
         Yahoo's own bar show the same anomaly or not) -- NEVER as a value donor.
         When Yahoo confirms an anomaly is real, the correction is still sourced
         from Massive's OWN self-consistency clip, never Yahoo's raw price
         (substituting Yahoo's raw value was tried and found to inject a real
         cross-vendor adjustment-basis mismatch, up to ~5.8% off, since Yahoo's
         cached hourly file isn't uniformly re-adjusted to today's basis the way
         this derived series is). In the no-Yahoo backfill region (no ground
         truth to check against at all), an anomaly is DETECTED AND LOGGED ONLY,
         never auto-modified -- auto-clipping was tried and found to destroy a
         real market-wide event (2022-09-21 FOMC volatility).
     Every correction (and every detected-but-not-corrected flag) is logged to
     db_cache.massive_hourly_corrections (ticker/build_id/ts/field/raw/new/reason)
     for a full audit trail, same spirit as data_mutation_log.
  5. Writes the final series to db_cache.massive_hourly_derived under a fresh,
     permanent build_id (db_cache.record_massive_hourly_build) -- every vintage of
     a ticker's derived series is kept side by side, never overwritten (see that
     table's docstring in db_cache.py for why).

Rate limiting: Massive's free tier is 5 calls/min. The dividends endpoint is one
(paginated) call per ticker, cached after the first fetch -- SLEEP_BETWEEN_TICKERS
paces a multi-ticker run conservatively regardless, since a --tickers-all run hits
many tickers back to back.

Usage:
    .venv/bin/python scripts/build_massive_hourly_derived.py --tickers SOXL
    .venv/bin/python scripts/build_massive_hourly_derived.py --tickers SOXL KORU
    .venv/bin/python scripts/build_massive_hourly_derived.py --all   # every cached minute-data ticker
"""
import argparse
import glob
import os
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import db_cache

API_KEY = os.environ.get("MASSIVE_API_KEY")
MINUTE_DIR = ROOT / "cache" / "research" / "minute_data"
HOURLY_DIR = ROOT / "cache" / "research"

SLEEP_BETWEEN_TICKERS = 13.0  # conservative: assumes free tier (5 calls/min = 12s/call minimum)
                               # since it's unconfirmed whether the account has actually been
                               # downgraded from the paid "unlimited" plan yet (2026-08-22 decision,
                               # not verified executed) -- safe either way, just slower if still paid.
MAX_RETRIES = 5
MAX_PAGES = 50
WICK_OUTLIER_MULT = 3.0   # a wick beyond this many multiples of the recent typical range is suspect
ROLLING_WINDOW = 20       # bars, for the "typical range" baseline
REL_DIFF_THRESHOLD = 0.005  # 0.5% -- how much Massive/Yahoo must disagree to trigger a cross-check
CLOSE_ROUNDTRIP_THRESHOLD = 0.4    # matches scan_bad_ticks.py's default -- the DFEN-shape detector
CLOSE_ROUNDTRIP_RECOVERY_FRAC = 0.7
BUILD_LABEL = "as of 2026-08-22"  # vintage label for this rebuild -- bump the date on any future full rerun


def fetch_dividends(ticker):
    """Returns cached dividends if present, else fetches from Massive's
    /stocks/v1/dividends (paginated) and caches them."""
    cached = db_cache.get_massive_dividends(ticker)
    if cached:
        return cached

    all_divs = []
    url = "https://api.massive.com/stocks/v1/dividends"
    params = {"ticker": ticker, "limit": 1000, "apiKey": API_KEY}
    page_count = 0
    while url:
        page_count += 1
        if page_count > MAX_PAGES:
            raise RuntimeError(f"{ticker}: dividends pagination exceeded {MAX_PAGES} pages, aborting")
        resp = None
        for attempt in range(MAX_RETRIES):
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                backoff = 5.0 * (attempt + 1)
                print(f"  {ticker}: dividends 429 rate-limited, retry {attempt+1}/{MAX_RETRIES} in {backoff:.0f}s",
                      file=sys.stderr)
                time.sleep(backoff)
                continue
            break
        if resp.status_code != 200:
            # One-time cleanup, not an ongoing service -- fail loud and skip this
            # ticker rather than silently writing a dividend-UNADJUSTED series that
            # looks identical to a genuinely non-dividend-paying ETF (found by
            # paired review 2026-08-22: this was the exact failure mode before).
            raise RuntimeError(f"{ticker}: dividends fetch HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        all_divs.extend(data.get("results", []))
        next_url = data.get("next_url")
        if not next_url:
            break
        url = next_url
        params = {"apiKey": API_KEY}
        time.sleep(0.3)

    db_cache.cache_massive_dividends(ticker, all_divs)
    return db_cache.get_massive_dividends(ticker)


def apply_dividend_adjustment(dfm, divs):
    """dfm: minute DataFrame indexed by tz-naive timestamp (Open/High/Low/Close).
    divs: list of {ex_dividend_date, historical_adjustment_factor}. Returns a new
    DataFrame with adjusted prices -- never mutates the raw cached CSV."""
    if not divs:
        return dfm.copy()
    d = pd.DataFrame(divs)
    d["ex_dividend_date"] = pd.to_datetime(d["ex_dividend_date"])
    d = d.sort_values("ex_dividend_date").reset_index(drop=True)

    idx_df = pd.DataFrame({"ts": dfm.index})
    merged = pd.merge_asof(idx_df, d.rename(columns={"ex_dividend_date": "ts"}),
                            on="ts", direction="forward")
    factor = merged["historical_adjustment_factor"].fillna(1.0).values

    out = dfm.copy()
    for col in ["Open", "High", "Low", "Close"]:
        out[col] = out[col].values * factor
    return out


def resample_to_hourly(dfm_adj):
    t = dfm_adj.index.time
    session = dfm_adj.loc[(t >= pd.Timestamp("09:30").time()) & (t < pd.Timestamp("16:00").time())]
    bucket = pd.DatetimeIndex(pd.Series(session.index).apply(
        lambda ts: ts.floor("h") + pd.Timedelta(minutes=30) if ts.minute >= 30
        else ts.floor("h") - pd.Timedelta(minutes=30)
    ))
    tmp = session.copy()
    tmp["bucket"] = bucket
    agg = {"Open": ("Open", "first"), "High": ("High", "max"),
           "Low": ("Low", "min"), "Close": ("Close", "last")}
    if "Volume" in tmp.columns:
        agg["Volume"] = ("Volume", "sum")
    hourly = tmp.groupby("bucket").agg(**agg)
    return hourly


def load_real_yahoo_hourly(ticker):
    path = HOURLY_DIR / f"{ticker}_1h.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.sort_index()


def detect_close_roundtrips(hourly):
    """The DFEN-shape detector -- a single-bar Close spike that round-trips back on
    the very next bar. The wick-based detector below can never catch this shape (a
    Close spike drags High/Low up with it, so upper_wick/lower_wick relative to
    max(O,C)/min(O,C) stays ~0) -- confirmed by paired review 2026-08-22 as a real
    gap the original version of this script had.

    Uses LOG returns, not scan_bad_ticks.py's raw pct_change -- found by a second
    paired-review round (2026-08-22) that arithmetic pct_change is asymmetric: a
    full round-trip from an UP-spike of magnitude r requires a reversal of only
    -r/(1+r), so scan_bad_ticks.py's own abs(nr) >= abs(r)*recovery_frac test is
    only satisfiable for r <= ~0.43 -- a 10x fabricated up-tick (far more damaging
    than a 45% one) sails through completely undetected, in BOTH this port and the
    original scan_bad_ticks.py it copied. Log returns are symmetric under a perfect
    round-trip (ln(P2/P1) == -ln(P1/P0) exactly when P2==P0, regardless of
    direction or magnitude), so this closes the gap for both directions at once.
    Returns a list of (ts, direction) for each hit; direction is 'high' if the
    spike was upward or 'low' if downward."""
    close = hourly["Close"]
    log_ret = np.log(close / close.shift(1))
    next_log_ret = log_ret.shift(-1)
    threshold_log = np.log1p(CLOSE_ROUNDTRIP_THRESHOLD)
    hits = []
    idx = hourly.index
    for i in range(1, len(hourly) - 1):
        r, nr = log_ret.iloc[i], next_log_ret.iloc[i]
        if pd.isna(r) or pd.isna(nr):
            continue
        if abs(r) < threshold_log or (r > 0) == (nr > 0):
            continue
        if abs(nr) < abs(r) * CLOSE_ROUNDTRIP_RECOVERY_FRAC:
            continue
        hits.append((idx[i], "high" if r > 0 else "low"))
    return hits


def correct_spikes(ticker, hourly, yahoo_hourly):
    """Returns (corrected_hourly, corrections_applied). Two independent detectors,
    since they catch different bug shapes (paired review 2026-08-22 found the
    original version only had the first one, missing the shape that actually caused
    the real DFEN incident):

    1. Close-based round-trip (detect_close_roundtrips, scan_bad_ticks.py's proven
       logic) -- a fabricated Close spike that reverses next bar. Always corrected in
       place (Low=Close=Open for a downward spike, High=Close=Open for an upward one,
       same conservative "neutralize the fake signal, don't fabricate a specific true
       price" convention as scan_bad_ticks.py's --fix).

    2. Wick outlier (High/Low far beyond the bar's own O/C and recent typical range,
       Close/Open unaffected) -- the shape actually found this session, which (1)
       cannot catch. Resolution differs by data availability, per the 2026-08-22
       design decision:
       - Yahoo-overlap window: Yahoo used ONLY as a detector (does Yahoo show the same
         anomaly or not), never as a value donor -- substituting Yahoo's raw price
         was tried and found to inject a real ~1-6% cross-vendor adjustment-basis
         mismatch (Yahoo's cached hourly file is built incrementally and its older
         rows are NOT re-adjusted to today's basis the way this derived series is;
         paired-review contextual pass 2026-08-22 found this produced an actually-
         impossible bar, Low > High, in SOXL). So when Yahoo disagrees, the CORRECTION
         still comes from Massive's own self-consistency clip -- Yahoo only answers
         "is this confirmed real or confirmed suspect," never "what's the true price."
       - Backfill region (no Yahoo bar to check against at all): DETECT AND LOG ONLY,
         never auto-modify. The original version auto-clipped here and was found to
         have clipped a genuine market-wide event (2022-09-21 FOMC whipsaw, hit
         simultaneously across 8 unrelated tickers) -- there's no way to distinguish
         a real event from a bad tick with zero ground truth, so the safe default is
         visibility without risk of destroying real volatility."""
    hourly = hourly.copy()
    hourly["corrected"] = 0
    corrections = []

    for ts, direction in detect_close_roundtrips(hourly):
        o, h, l, c = hourly.loc[ts, ["Open", "High", "Low", "Close"]]
        if direction == "low":
            hourly.at[ts, "Close"] = o
            hourly.at[ts, "Low"] = o
            corrections.append((ts, "Close", c, o, "close-roundtrip (DFEN-shape): downward spike reversed next bar, neutralized to Open"))
            corrections.append((ts, "Low", l, o, "close-roundtrip (DFEN-shape): downward spike reversed next bar, neutralized to Open"))
        else:
            hourly.at[ts, "Close"] = o
            hourly.at[ts, "High"] = o
            corrections.append((ts, "Close", c, o, "close-roundtrip (DFEN-shape): upward spike reversed next bar, neutralized to Open"))
            corrections.append((ts, "High", h, o, "close-roundtrip (DFEN-shape): upward spike reversed next bar, neutralized to Open"))
        hourly.at[ts, "corrected"] = 1

    upper_wick = hourly["High"] - hourly[["Open", "Close"]].max(axis=1)
    lower_wick = hourly[["Open", "Close"]].min(axis=1) - hourly["Low"]
    true_range = hourly["High"] - hourly["Low"]
    typical_range = true_range.rolling(ROLLING_WINDOW, min_periods=5).median().shift(1)

    for ts in hourly.index:
        tr = typical_range.loc[ts]
        if pd.isna(tr) or tr <= 0:
            continue
        has_yahoo = yahoo_hourly is not None and ts in yahoo_hourly.index
        y = yahoo_hourly.loc[ts] if has_yahoo else None

        uw = upper_wick.loc[ts]
        if uw > WICK_OUTLIER_MULT * tr:
            raw_high = hourly.at[ts, "High"]
            confirmed_anomaly = True
            if has_yahoo:
                y_uw = y["High"] - max(y["Open"], y["Close"])
                confirmed_anomaly = y_uw <= WICK_OUTLIER_MULT * tr
                if not confirmed_anomaly:
                    corrections.append((ts, "High", raw_high, raw_high,
                                         "cross-vendor refuted (Yahoo shows the same wick): treated as a real event, not corrected"))
            if confirmed_anomaly:
                clipped = hourly.loc[ts, ["Open", "Close"]].max() + WICK_OUTLIER_MULT * tr
                if clipped < raw_high:
                    if has_yahoo:
                        hourly.at[ts, "High"] = clipped
                        hourly.at[ts, "corrected"] = 1
                        corrections.append((ts, "High", raw_high, clipped,
                                             "cross-vendor confirmed (Yahoo doesn't show the same wick): self-consistency clip applied"))
                    else:
                        corrections.append((ts, "High", raw_high, raw_high,
                                             "no-Yahoo backfill region: DETECTED, NOT auto-corrected (no ground truth to confirm real-vs-bad-tick)"))

        lw = lower_wick.loc[ts]
        if lw > WICK_OUTLIER_MULT * tr:
            raw_low = hourly.at[ts, "Low"]
            confirmed_anomaly = True
            if has_yahoo:
                y_lw = min(y["Open"], y["Close"]) - y["Low"]
                confirmed_anomaly = y_lw <= WICK_OUTLIER_MULT * tr
                if not confirmed_anomaly:
                    corrections.append((ts, "Low", raw_low, raw_low,
                                         "cross-vendor refuted (Yahoo shows the same wick): treated as a real event, not corrected"))
            if confirmed_anomaly:
                clipped = hourly.loc[ts, ["Open", "Close"]].min() - WICK_OUTLIER_MULT * tr
                if clipped > raw_low:
                    if has_yahoo:
                        hourly.at[ts, "Low"] = clipped
                        hourly.at[ts, "corrected"] = 1
                        corrections.append((ts, "Low", raw_low, clipped,
                                             "cross-vendor confirmed (Yahoo doesn't show the same wick): self-consistency clip applied"))
                    else:
                        corrections.append((ts, "Low", raw_low, raw_low,
                                             "no-Yahoo backfill region: DETECTED, NOT auto-corrected (no ground truth to confirm real-vs-bad-tick)"))

    return hourly, corrections


def build_ticker(ticker):
    minute_path = MINUTE_DIR / f"{ticker}_1m.csv"
    if not minute_path.exists():
        print(f"{ticker}: no cached minute data, skipping")
        return False

    raw_pulled_at = pd.Timestamp(os.path.getmtime(minute_path), unit="s").strftime("%Y-%m-%d %H:%M:%S")

    dfm = pd.read_csv(minute_path)
    dfm["timestamp"] = pd.to_datetime(dfm["timestamp"], utc=True).dt.tz_convert("US/Eastern").dt.tz_localize(None)
    dfm = dfm.set_index("timestamp").sort_index()
    raw_data_start = dfm.index.min().strftime("%Y-%m-%d")
    raw_data_end = dfm.index.max().strftime("%Y-%m-%d")

    divs = fetch_dividends(ticker)  # raises on failure -- one-time cleanup, fail loud, don't silently write unadjusted data
    print(f"{ticker}: {len(divs)} dividend records")
    dividend_asof = max((d["ex_dividend_date"] for d in divs), default=None)

    dfm_adj = apply_dividend_adjustment(dfm, divs)
    hourly = resample_to_hourly(dfm_adj)

    yahoo_hourly = load_real_yahoo_hourly(ticker)
    hourly_corrected, corrections = correct_spikes(ticker, hourly, yahoo_hourly)

    # Persist the adjusted MINUTE series too (2026-08-22 fix) -- dfm_adj was
    # computed above but previously only ever consumed via its hourly resample.
    # Regular-session-only, matching every real minute consumer's own filter
    # (sim_minute_groundtruth_independent.load_minutes,
    # run_optimization_sweep._load_minute_df).
    t = dfm_adj.index.time
    minute_session = dfm_adj.loc[(t >= pd.Timestamp("09:30").time()) & (t < pd.Timestamp("16:00").time())]

    # Build row + hourly rows + corrections + minute rows all share ONE transaction
    # (paired review, 2026-08-22: three independent reviewers confirmed the prior
    # one-connection-per-write design could leave the hourly leg on a newer build_id
    # than the minute leg if the process died mid-build -- get_massive_hourly_ohlcv
    # and get_massive_minute_ohlcv each independently pick "latest build_id with rows
    # in MY table", so a partial build silently pairs two different dividend/raw-data
    # vintages with zero error). Wrapping every write for this build in one
    # sqlite3 transaction means a failure at any point rolls back the whole build --
    # the next rerun starts clean rather than leaving a mismatched pair on file.
    with sqlite3.connect(db_cache.DB_PATH) as conn:
        build_id = db_cache.record_massive_hourly_build(
            ticker, BUILD_LABEL, raw_pulled_at, raw_data_start, raw_data_end,
            dividend_asof, len(hourly_corrected), len(corrections), conn=conn)

        for ts, field, raw_v, new_v, reason in corrections:
            db_cache.log_massive_hourly_correction(ticker, build_id, ts, field, raw_v, new_v, reason, conn=conn)

        db_cache.write_massive_hourly_derived(ticker, build_id, hourly_corrected, conn=conn)
        db_cache.write_massive_minute_derived(ticker, build_id, minute_session, conn=conn)

    # raw_value == new_value is the real structural signal for "flagged, not
    # actually changed" -- more robust than matching specific reason strings,
    # which now include two distinct not-corrected cases (no-Yahoo backfill
    # region, and cross-vendor-refuted/confirmed-real).
    applied = sum(1 for c in corrections if c[2] != c[3])
    flagged_only = len(corrections) - applied
    print(f"{ticker}: wrote {len(hourly_corrected)} hourly bars "
          f"({hourly_corrected.index.min()} .. {hourly_corrected.index.max()}), "
          f"{applied} correction(s) applied, {flagged_only} flagged-only (no ground truth); "
          f"wrote {len(minute_session)} adjusted minute bars")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=None)
    ap.add_argument("--all", action="store_true", help="process every ticker with cached minute data")
    args = ap.parse_args()

    if not API_KEY:
        print("MASSIVE_API_KEY not found in .env", file=sys.stderr)
        sys.exit(1)

    if args.all:
        tickers = sorted(Path(f).stem.replace("_1m", "") for f in glob.glob(str(MINUTE_DIR / "*_1m.csv")))
    elif args.tickers:
        tickers = args.tickers
    else:
        print("Specify --tickers T [T ...] or --all", file=sys.stderr)
        sys.exit(1)

    print(f"Building derived hourly for {len(tickers)} ticker(s): {tickers}")
    ok, skipped, errored = 0, 0, []
    for i, t in enumerate(tickers):
        try:
            if build_ticker(t):
                ok += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"{t}: ERROR {e!r}", file=sys.stderr)
            errored.append(t)
        if i < len(tickers) - 1:
            time.sleep(SLEEP_BETWEEN_TICKERS)

    print(f"\nDone: {ok} built, {skipped} skipped (no cached minute data), "
          f"{len(errored)} errored out of {len(tickers)}")
    if errored:
        print(f"Errored tickers (re-run individually once fixed): {errored}")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
