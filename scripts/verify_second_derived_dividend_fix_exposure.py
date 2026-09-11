"""Batch re-verification of the massive_second_derived double-dividend-adjustment
bug (fixed 2026-09-06, see docs/research_log.md's 2026-09-06 "Root cause of
massive_second_derived's price-scale mismatch" entry) against real stored
phase5_trades rows.

Context: phase5_trades' resolution='1s' rows store trade-level entry/exit prices
computed against whatever massive_second_derived build was active at generation
time. 33,057 of 33,062 such rows across the 14 real live tickers have created_at
before the fix (2026-09-06T18:44:14, OILU's own build-11->48 promotion instant).
Those rows were never refreshed (Phase5 is retired, no longer invoked per-campaign)
-- this script answers whether that exposure is actually MATERIAL per ticker, not
just "exists in principle".

Method (same as the OILU spot-check this script generalizes, 2026-09-08/09):
  1. A ticker with zero real massive_dividends_raw rows can't be affected by a
     dividend-adjustment bug at all -- mark unaffected without loading any second-
     level data.
  2. Otherwise, compare the ORIGINAL (2026-08-29, pre-fix, lowest build_id per
     ticker in massive_second_derived_builds) build's Close series against the
     currently ACTIVE (active_builds, confirmed post-fix per the research_log
     verdict) build's Close series. Byte-identical (allowing float round-trip
     noise) -> unaffected. Differs -> real, material exposure; report which
     pre-fix phase5_trades rows actually fall inside a differing timestamp.

Read-only throughout (db_cache.get_massive_second_derived, no writes) -- safe to
run any time, including alongside a live sweep campaign.
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import os
os.chdir(ROOT)

import numpy as np
import pandas as pd

import db_cache

DB_PATH = "cache/research/trading_universe.db"
FIX_CUTOFF = "2026-09-06T18:44:14"
TICKERS = ["SOXL", "AGQ", "ETHU", "OILU", "GDXU", "UGL", "WEBL", "DFEN",
           "HIBL", "JNUG", "KORU", "LABU", "NUGT", "DPST"]


def dividend_row_count(conn, ticker):
    return conn.execute(
        "SELECT COUNT(*) FROM massive_dividends_raw WHERE ticker=?", (ticker,)
    ).fetchone()[0]


def pre_and_active_build_ids(conn, ticker):
    pre = conn.execute(
        "SELECT MIN(id) FROM massive_second_derived_builds WHERE ticker=?", (ticker,)
    ).fetchone()[0]
    active = conn.execute(
        "SELECT build_id FROM active_builds WHERE ticker=? AND table_name='second'", (ticker,)
    ).fetchone()
    active = active[0] if active else None
    return pre, active


def pre_fix_1s_trades(conn, ticker):
    cur = conn.execute(
        "SELECT candidate_id, trade_idx, entry_time, exit_time, entry_price, exit_price "
        "FROM phase5_trades WHERE ticker=? AND resolution='1s' AND created_at < ?",
        (ticker, FIX_CUTOFF),
    )
    cols = [d[0] for d in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=cols)


def compare_builds(ticker, pre_build, active_build):
    """Returns (identical: bool, max_abs_rel_diff: float, diff_ts_index: pd.DatetimeIndex)."""
    pre_df = db_cache.get_massive_second_derived(ticker, build_id=pre_build)[["Close"]]
    active_df = db_cache.get_massive_second_derived(ticker, build_id=active_build)[["Close"]]
    joined = pre_df.join(active_df, how="inner", lsuffix="_pre", rsuffix="_active")
    if joined.empty:
        return True, 0.0, pd.DatetimeIndex([])
    rel_diff = (joined["Close_pre"] - joined["Close_active"]).abs() / joined["Close_active"]
    max_diff = float(rel_diff.max())
    if max_diff < 1e-9:
        return True, max_diff, pd.DatetimeIndex([])
    diffing = joined.index[rel_diff > 1e-6]
    return False, max_diff, diffing


def main():
    conn = sqlite3.connect(DB_PATH)
    print(f"{'ticker':6s} {'div_rows':>9s} {'exposed':>8s}  detail")
    print("-" * 90)
    exposed_report = []
    for t in TICKERS:
        divs = dividend_row_count(conn, t)
        if divs == 0:
            print(f"{t:6s} {divs:9d} {'no':>8s}  no dividend records -- can't be affected")
            continue

        pre_build, active_build = pre_and_active_build_ids(conn, t)
        if pre_build is None or active_build is None:
            print(f"{t:6s} {divs:9d} {'?':>8s}  missing build_id (pre={pre_build}, active={active_build})")
            continue
        if pre_build == active_build:
            print(f"{t:6s} {divs:9d} {'no':>8s}  only one build on record ({pre_build}) -- nothing to compare")
            continue

        identical, max_diff, diff_ts = compare_builds(t, pre_build, active_build)
        if identical:
            print(f"{t:6s} {divs:9d} {'no':>8s}  build {pre_build} vs {active_build} byte-identical "
                  f"(max rel diff {max_diff:.2e})")
            continue

        trades = pre_fix_1s_trades(conn, t)
        affected_candidates = set()
        if not trades.empty and len(diff_ts) > 0:
            diff_start, diff_end = diff_ts.min(), diff_ts.max()
            trades["entry_time"] = pd.to_datetime(trades["entry_time"])
            trades["exit_time"] = pd.to_datetime(trades["exit_time"])
            overlapping = trades[(trades["exit_time"] >= diff_start) & (trades["entry_time"] <= diff_end)]
            affected_candidates = set(overlapping["candidate_id"].unique())

        print(f"{t:6s} {divs:9d} {'YES':>8s}  build {pre_build} vs {active_build} DIFFERS "
              f"(max rel diff {max_diff:.4%}, {len(diff_ts)} differing seconds, "
              f"{len(trades)} pre-fix 1s trade rows total, "
              f"{len(affected_candidates)} candidate(s) overlap the differing window)")
        exposed_report.append((t, max_diff, len(affected_candidates), len(trades)))

    print()
    if exposed_report:
        print("MATERIAL EXPOSURE SUMMARY:")
        for t, max_diff, n_cand, n_trades in exposed_report:
            print(f"  {t}: max rel price diff {max_diff:.4%} -- {n_cand} candidate(s) / "
                  f"{n_trades} pre-fix 1s trade rows plausibly affected")
    else:
        print("No material exposure found -- all dividend-bearing tickers' active builds "
              "already match their original pre-fix builds (fix was a no-op price-wise for "
              "these, or dividend events fall outside the stored second-level history).")
    conn.close()


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
