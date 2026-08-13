"""detect_one_bar_split_artifacts / fix_one_bar_split_artifacts (2026-08-13,
redesigned same day after paired Opus review): catches a single-bar
split-day artifact -- yfinance serving a partially-adjusted print for
exactly the split-effective bar within a single fetched dataframe, distinct
from data_manager.py's whole-history incremental-overlap rescale (which only
fires on a stale-cache-vs-fresh-fetch mismatch). Found live 2026-08-13: 11
of 12 scripts/scan_bad_ticks.py "bad tick" hits were actually this, confirmed
against real corporate-action dates.

The first version of this fix (price-ratio-and-recovery heuristic only, no
real-split cross-check, proportional per-field rescaling) was reviewed by a
paired independent-cold + contextual Opus review before being committed and
found to have several real bugs: it could rescale a genuinely volatile-but-
valid bar into an internally inconsistent one (Low > High), it silently
"fixed" a plain bad-tick spike that coincidentally matched a clean ratio, a
NaN next-bar Close bypassed the recovery guard, and it was only wired into
the once-per-ticker bootstrap path (leaving every already-cached ticker's
future splits uncaught). This redesigned version requires a real confirmed
split (via a `splits` argument, e.g. yfinance's Ticker.splits) before
correcting anything, and flattens the whole bar to a verified anchor price
instead of proportionally rescaling individual fields. See
docs/research_log.md's 2026-08-13 entries for the full history.
"""
import math
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from signals_helpers import detect_one_bar_split_artifacts, fix_one_bar_split_artifacts


def _df(rows):
    idx = pd.date_range("2026-01-01 09:30", periods=len(rows), freq="h")
    df = pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"])
    return df


def _real_split(date_str, ratio):
    return pd.Series([ratio], index=pd.to_datetime([date_str]))


def test_no_hits_without_a_real_split_on_file():
    # The exact RXL-shaped price pattern, but with NO real split record --
    # must not fire. This is the core fix for the false-positive risk found
    # by review: a plain bad-tick / real large move must never be "corrected"
    # on price-heuristic alone.
    df = _df([
        [51.20, 51.30, 51.10, 51.27],
        [51.33, 51.33, 25.66, 25.66],
        [51.50, 51.70, 51.40, 51.66],
    ])
    assert detect_one_bar_split_artifacts(df, pd.Series(dtype=float)) == []
    assert detect_one_bar_split_artifacts(df, None) == []


def test_detects_close_only_artifact_shape_with_real_split_confirmed():
    # Mirrors RXL's real 2024-11-06 hit: Open/High stay on the pre-split
    # scale, Close/Low collapse by a clean 2x, next bar recovers, and a real
    # split is on file for this date.
    df = _df([
        [51.20, 51.30, 51.10, 51.27],
        [51.33, 51.33, 25.66, 25.66],
        [51.50, 51.70, 51.40, 51.66],
    ])
    splits = _real_split(df.index[1].date(), 2.0)
    hits = detect_one_bar_split_artifacts(df, splits)
    assert len(hits) == 1
    ts, factor = hits[0]
    assert ts == df.index[1]
    assert factor == 2.0


def test_detects_whole_bar_artifact_shape_with_real_split_confirmed():
    # Mirrors UPW's real 2025-11-19 hit: Open/High/Low/Close ALL collapse
    # together by a clean 4x, next bar recovers, real split on file.
    df = _df([
        [23.40, 23.50, 23.30, 23.40],
        [5.82, 5.82, 5.82, 5.82],
        [23.60, 23.90, 23.50, 23.86],
    ])
    splits = _real_split(df.index[1].date(), 4.0)
    hits = detect_one_bar_split_artifacts(df, splits)
    assert len(hits) == 1


def test_real_split_more_than_tolerance_days_away_does_not_count():
    df = _df([
        [51.20, 51.30, 51.10, 51.27],
        [51.33, 51.33, 25.66, 25.66],
        [51.50, 51.70, 51.40, 51.66],
    ])
    far_date = (df.index[1] + pd.Timedelta(days=10)).date()
    splits = _real_split(far_date, 2.0)
    assert detect_one_bar_split_artifacts(df, splits, split_date_tolerance_days=1) == []


def test_real_split_ratio_mismatch_does_not_count():
    # A real split IS on file near this date, but at a different ratio (5x,
    # not the 2x this bar's price pattern implies) -- must not match.
    df = _df([
        [51.20, 51.30, 51.10, 51.27],
        [51.33, 51.33, 25.66, 25.66],
        [51.50, 51.70, 51.40, 51.66],
    ])
    splits = _real_split(df.index[1].date(), 5.0)
    assert detect_one_bar_split_artifacts(df, splits) == []


def test_no_false_positive_on_a_plain_bad_tick_spike_even_with_recovery():
    # A bad-tick spike that happens to land near a clean ratio and reverts
    # next bar (the exact shape a real bug's price-heuristic-only version
    # would have wrongly "fixed") -- with no real split on file, must not fire.
    df = _df([
        [10.0, 10.1, 9.9, 10.0],
        [15.0, 15.2, 14.8, 15.1],
        [10.0, 10.1, 9.9, 10.05],
    ])
    assert detect_one_bar_split_artifacts(df, pd.Series(dtype=float)) == []


def test_nan_next_close_does_not_falsely_pass_recovery_check():
    df = _df([
        [51.20, 51.30, 51.10, 51.27],
        [51.33, 51.33, 25.66, 25.66],
        [float("nan"), float("nan"), float("nan"), float("nan")],
    ])
    splits = _real_split(df.index[1].date(), 2.0)
    assert detect_one_bar_split_artifacts(df, splits) == []


def test_zero_or_negative_prices_are_skipped_not_crashed_on():
    df = _df([
        [51.20, 51.30, 51.10, 51.27],
        [0.0, 0.0, 0.0, 0.0],
        [51.50, 51.70, 51.40, 51.66],
    ])
    splits = _real_split(df.index[1].date(), 2.0)
    # must not raise (division by zero) and must not flag a hit
    assert detect_one_bar_split_artifacts(df, splits) == []


def test_fix_close_only_artifact_flattens_to_open_and_stays_consistent():
    df = _df([
        [51.20, 51.30, 51.10, 51.27],
        [51.33, 51.33, 25.66, 25.66],
        [51.50, 51.70, 51.40, 51.66],
    ])
    splits = _real_split(df.index[1].date(), 2.0)
    fixed, fixes = fix_one_bar_split_artifacts(df, splits)
    assert len(fixes) == 1
    ts = df.index[1]
    for col in ("Open", "High", "Low", "Close"):
        assert fixed.loc[ts, col] == 51.33
    # original df not mutated in place
    assert df.loc[ts, "Close"] == 25.66


def test_fix_whole_bar_artifact_flattens_to_prior_close():
    df = _df([
        [23.40, 23.50, 23.30, 23.40],
        [5.82, 5.82, 5.82, 5.82],
        [23.60, 23.90, 23.50, 23.86],
    ])
    splits = _real_split(df.index[1].date(), 4.0)
    fixed, fixes = fix_one_bar_split_artifacts(df, splits)
    assert len(fixes) == 1
    ts = df.index[1]
    for col in ("Open", "High", "Low", "Close"):
        assert fixed.loc[ts, col] == 23.40


def test_fix_never_produces_an_internally_inconsistent_bar():
    # Regression for the review-confirmed bug: a genuinely volatile-but-
    # valid bar (Low well below Close, real intrabar action) must never come
    # out with Low > High or Close outside [Low, High] after correction --
    # the flatten-to-anchor strategy guarantees this by construction.
    df = _df([
        [100.0, 101.0, 99.0, 100.0],
        [100.0, 101.0, 60.0, 50.0],  # Close collapsed 2x, but Low=60 is real, not an artifact value
        [100.0, 101.0, 99.0, 100.0],
    ])
    splits = _real_split(df.index[1].date(), 2.0)
    fixed, fixes = fix_one_bar_split_artifacts(df, splits)
    assert len(fixes) == 1
    ts = df.index[1]
    o, h, l, c = (fixed.loc[ts, x] for x in ("Open", "High", "Low", "Close"))
    assert l <= min(o, c) <= max(o, c) <= h


def test_no_false_positive_on_a_real_sustained_crash():
    df = _df([
        [51.20, 51.30, 51.10, 51.27],
        [51.33, 51.33, 25.66, 25.66],
        [25.50, 25.70, 25.40, 25.55],  # stays down -- real move, not a split artifact
    ])
    splits = _real_split(df.index[1].date(), 2.0)
    assert detect_one_bar_split_artifacts(df, splits) == []


def test_no_hits_on_clean_data():
    df = _df([
        [51.20, 51.30, 51.10, 51.27],
        [51.30, 51.40, 51.20, 51.35],
        [51.35, 51.50, 51.25, 51.40],
    ])
    splits = _real_split(df.index[1].date(), 2.0)
    assert detect_one_bar_split_artifacts(df, splits) == []
