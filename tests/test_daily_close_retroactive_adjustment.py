"""Tests for the daily-close retroactive-adjustment detector (docs/design.md's
"2026-08-28 (late)" entry) -- signals_helpers.check_daily_close_retroactive_
adjustment, fetch_fresh_daily_closes, and signals_compute's wiring of both
into compute_buy_signal's indicator-computation path.

No real historical ex-dividend event is available to test against
organically (design doc's own caveat) -- these tests construct real,
synthetic/staged discontinuities and prove the day-over-day comparison
correctly detects an injected retroactive adjustment, and correctly does
NOT false-positive on ordinary price movement or a genuinely new trading
day's data."""
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_helpers
import signals_compute
from tests.conftest import fake_node, make_synthetic_csv, cleanup_csv

TICKER = 'TEST_DAILY_CLOSE_ADJ'
DAY1 = '2026-08-28'
DAY2 = '2026-08-29'  # "tomorrow" relative to DAY1, passed explicitly via today= --
                      # never monkeypatches the stdlib date class (which would be
                      # process-global for the test's duration, not just this module).


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    state_path = tmp_path / "daily_close_consistency_state.json"
    alert_path = tmp_path / "daily_close_discontinuity_alerts.json"
    monkeypatch.setattr(signals_helpers, '_DAILY_CLOSE_STATE_PATH', state_path)
    monkeypatch.setattr(signals_helpers, '_DAILY_CLOSE_ALERT_PATH', alert_path)
    # signals_compute._fresh_daily_cache/_daily_close_last_checked are
    # module-level (persist across the whole process, by design -- a
    # once-per-real-day cache) -- must be reset per test, or an earlier
    # test's cached (ticker, window) entry (same TICKER constant, same real
    # calendar day) silently short-circuits a later test's fetch entirely.
    signals_compute._fresh_daily_cache.clear()
    signals_compute._daily_close_last_checked.clear()
    yield state_path, alert_path
    signals_compute._fresh_daily_cache.clear()
    signals_compute._daily_close_last_checked.clear()


def _daily_df(closes_by_date):
    idx = pd.to_datetime(sorted(closes_by_date.keys()))
    closes = [closes_by_date[d.strftime('%Y-%m-%d')] for d in idx]
    return pd.DataFrame(
        {'Open': closes, 'High': closes, 'Low': closes, 'Close': closes, 'Volume': 1000},
        index=idx,
    )


def _base_closes(n=20, start='2026-08-01', value=100.0):
    dates = pd.bdate_range(start, periods=n)
    return {d.strftime('%Y-%m-%d'): value + i * 0.1 for i, d in enumerate(dates)}


# ---------------------------------------------------------------------------
# Core detector: signals_helpers.check_daily_close_retroactive_adjustment
# ---------------------------------------------------------------------------

def test_first_ever_call_never_fires(isolated_state):
    """No prior baseline exists yet -- nothing to compare against, must not
    false-fire on the very first fetch for a ticker."""
    closes = _base_closes()
    detection = signals_helpers.check_daily_close_retroactive_adjustment(TICKER, _daily_df(closes), today=DAY1)
    assert detection is None
    state = json.loads(signals_helpers._DAILY_CLOSE_STATE_PATH.read_text())
    assert TICKER in state
    assert state[TICKER]['closes'] == closes
    assert state[TICKER]['as_of'] == DAY1


def test_ordinary_new_trading_day_does_not_false_positive(isolated_state):
    """A genuinely new day's close appended, with every PRIOR (overlapping)
    date's value unchanged -- ordinary price movement, not a retroactive
    rescale -- must not fire."""
    closes_day1 = _base_closes()
    signals_helpers.check_daily_close_retroactive_adjustment(TICKER, _daily_df(closes_day1), today=DAY1)

    closes_day2 = dict(closes_day1)
    closes_day2['2026-08-31'] = 250.0  # new day, big real move -- not a rescale of history
    detection = signals_helpers.check_daily_close_retroactive_adjustment(TICKER, _daily_df(closes_day2), today=DAY2)
    assert detection is None, f"ordinary new-day price movement must not fire: {detection}"


def test_dividend_sized_retroactive_rescale_is_detected(isolated_state):
    """Injected discontinuity: every overlapping historical date's close is
    uniformly rescaled by a small, non-split ratio (~0.8%, a plausible real
    dividend-adjustment size) -- must fire, classified dividend-sized."""
    closes_day1 = _base_closes()
    signals_helpers.check_daily_close_retroactive_adjustment(TICKER, _daily_df(closes_day1), today=DAY1)

    rescale = 0.992  # ~0.8% down-adjustment, dividend-sized, not near any _SPLIT_RATIOS entry
    closes_day2 = {d: v * rescale for d, v in closes_day1.items()}
    detection = signals_helpers.check_daily_close_retroactive_adjustment(TICKER, _daily_df(closes_day2), today=DAY2)

    assert detection is not None, "a uniform small retroactive rescale must be detected"
    assert detection['classification'] == 'dividend-or-other-sized'
    assert detection['nearest_split_factor'] is None
    assert detection['dates_affected'] == len(closes_day1)
    assert abs(detection['ratio'] - 1 / rescale) < 0.001


def test_split_sized_retroactive_rescale_is_classified_as_split(isolated_state):
    """Injected discontinuity at a clean 2:1 ratio (matches the existing
    split-guard's _SPLIT_RATIOS) -- must fire, classified split-sized, with
    the correct nearest_split_factor."""
    closes_day1 = _base_closes()
    signals_helpers.check_daily_close_retroactive_adjustment(TICKER, _daily_df(closes_day1), today=DAY1)

    closes_day2 = {d: v / 2.0 for d, v in closes_day1.items()}
    detection = signals_helpers.check_daily_close_retroactive_adjustment(TICKER, _daily_df(closes_day2), today=DAY2)

    assert detection is not None
    assert detection['classification'] == 'split-sized'
    assert detection['nearest_split_factor'] == 2.0
    assert abs(detection['ratio'] - 2.0) < 0.01


def test_partial_window_rebase_is_still_detected(isolated_state):
    """Real regression test for a HIGH finding from paired review: a
    retroactive adjustment only rescales dates BEFORE its effective (ex-)
    date -- if the comparison window straddles that date, only the OLDER
    MINORITY of the overlapping window is affected, the newer majority is
    unchanged. An earlier version of this detector required a majority of
    the whole window to fire and silently MISSED exactly this shape (a real
    rebase reproduced with 7 of 20 total dates affected, well under 50%).
    The fix requires the mismatched dates to form a contiguous prefix of the
    OLDEST dates instead, which correctly fires here regardless of what
    fraction of the total window that prefix covers."""
    closes_day1 = _base_closes(n=20)
    signals_helpers.check_daily_close_retroactive_adjustment(TICKER, _daily_df(closes_day1), today=DAY1)

    sorted_dates = sorted(closes_day1)
    closes_day2 = dict(closes_day1)
    for d in sorted_dates[:7]:  # only the oldest 7 of 20 (35%) rescaled -- minority of the window
        closes_day2[d] = closes_day1[d] * 0.99
    detection = signals_helpers.check_daily_close_retroactive_adjustment(TICKER, _daily_df(closes_day2), today=DAY2)

    assert detection is not None, "a real partial-window rebase (minority of dates) must still be detected"
    assert detection['dates_affected'] == 7


def test_scattered_noncontiguous_mismatches_do_not_fire(isolated_state):
    """Mismatches scattered non-contiguously through the window (not a clean
    oldest-first prefix followed by all-unchanged) don't match the real
    corp-action signature -- must not fire. Distinguishes genuine systematic
    rescale from noisier, less coherent data-quality issues."""
    closes_day1 = _base_closes(n=20)
    signals_helpers.check_daily_close_retroactive_adjustment(TICKER, _daily_df(closes_day1), today=DAY1)

    sorted_dates = sorted(closes_day1)
    closes_day2 = dict(closes_day1)
    # Rescale the oldest date (starts a "prefix"), then leave a gap, then
    # rescale a much later date too -- not a clean contiguous prefix.
    closes_day2[sorted_dates[0]] = closes_day1[sorted_dates[0]] * 0.99
    closes_day2[sorted_dates[10]] = closes_day1[sorted_dates[10]] * 0.99
    detection = signals_helpers.check_daily_close_retroactive_adjustment(TICKER, _daily_df(closes_day2), today=DAY2)
    assert detection is None, f"scattered non-contiguous mismatches must not fire: {detection}"


def test_single_glitched_date_alone_does_not_fire(isolated_state):
    """One isolated date differs (a data-quality blip in a single fetch), the
    rest of the overlapping window is unchanged -- must NOT fire; this shape
    is a glitch, not a systematic retroactive rescale, and firing on it would
    be a real false-positive risk the majority-consistency requirement exists
    to prevent."""
    closes_day1 = _base_closes(n=20)
    signals_helpers.check_daily_close_retroactive_adjustment(TICKER, _daily_df(closes_day1), today=DAY1)

    closes_day2 = dict(closes_day1)
    one_date = next(iter(closes_day1))
    closes_day2[one_date] = closes_day1[one_date] * 1.5  # one date way off, rest untouched
    detection = signals_helpers.check_daily_close_retroactive_adjustment(TICKER, _daily_df(closes_day2), today=DAY2)
    assert detection is None, f"a single glitched date alone must not fire: {detection}"


def test_state_self_corrects_to_freshest_values(isolated_state):
    """After a detected rescale, the stored state must reflect TODAY's fresh
    (corrected) values, not yesterday's stale ones -- self-correcting by
    construction, no separate fix-it step needed."""
    closes_day1 = _base_closes()
    signals_helpers.check_daily_close_retroactive_adjustment(TICKER, _daily_df(closes_day1), today=DAY1)

    closes_day2 = {d: v / 2.0 for d, v in closes_day1.items()}
    signals_helpers.check_daily_close_retroactive_adjustment(TICKER, _daily_df(closes_day2), today=DAY2)

    state = json.loads(signals_helpers._DAILY_CLOSE_STATE_PATH.read_text())
    assert state[TICKER]['closes'] == closes_day2
    assert state[TICKER]['as_of'] == DAY2


def test_same_day_repeat_call_does_not_re_diff(isolated_state):
    """A second call the SAME day (e.g. a later poll) must not re-run the
    comparison against itself -- state 'as_of' already matches today."""
    closes_day1 = _base_closes()
    signals_helpers.check_daily_close_retroactive_adjustment(TICKER, _daily_df(closes_day1), today=DAY1)
    different = {d: v * 5 for d, v in closes_day1.items()}
    detection = signals_helpers.check_daily_close_retroactive_adjustment(TICKER, _daily_df(different), today=DAY1)
    assert detection is None
    state = json.loads(signals_helpers._DAILY_CLOSE_STATE_PATH.read_text())
    assert state[TICKER]['closes'] == closes_day1, "same-day repeat call must not overwrite the day's baseline"


# ---------------------------------------------------------------------------
# Wiring: signals_compute._daily_close_source / compute_buy_signal
# ---------------------------------------------------------------------------

def test_daily_close_source_falls_back_on_fetch_failure(isolated_state, monkeypatch):
    """fetch_fresh_daily_closes returning None (network hiccup) must fall
    back to the existing resampled-cache dataframe, never block/return None
    itself -- this module's fail-toward-existing-behavior convention."""
    monkeypatch.setattr(signals_compute, 'fetch_fresh_daily_closes', lambda ticker, window: None)
    fallback_df = pd.DataFrame({'Close': [100.0, 101.0]}, index=pd.bdate_range('2026-08-01', periods=2))
    node = fake_node(TICKER, 'ZScoreBreakout', window=2)
    result = signals_compute._daily_close_source(node, 2, fallback_df)
    assert result is fallback_df


def test_daily_close_source_uses_fresh_fetch_when_available(isolated_state, monkeypatch):
    """A successful fresh fetch with enough rows must be used in place of the
    fallback dataframe."""
    fresh_df = pd.DataFrame({'Close': [200.0, 201.0, 202.0]},
                             index=pd.bdate_range('2026-08-25', periods=3))
    monkeypatch.setattr(signals_compute, 'fetch_fresh_daily_closes', lambda ticker, window: fresh_df)
    fallback_df = pd.DataFrame({'Close': [1.0]}, index=pd.bdate_range('2020-01-01', periods=1))
    node = fake_node(TICKER, 'ZScoreBreakout', window=2)
    result = signals_compute._daily_close_source(node, 2, fallback_df)
    assert result is fresh_df


def test_compute_buy_signal_live_call_triggers_alert_on_detected_rescale(isolated_state, monkeypatch):
    """End-to-end: a live compute_buy_signal call (no df_hourly_override)
    whose fresh fetch shows a retroactive rescale vs. a seeded prior-day
    state fires the Slack alert + coverage_event, exactly once (dedup).
    Seeds the state directly with a PRIOR (real yesterday) 'as_of' rather
    than faking "today", so the real date.today() used internally needs no
    patching at all."""
    make_synthetic_csv(TICKER, last_close=95.0)
    try:
        closes_day1 = _base_closes(n=25, value=95.0)
        signals_helpers._save_daily_close_state({TICKER: {'as_of': '2020-01-01', 'closes': closes_day1}})
        signals_compute._fresh_daily_cache.clear()
        signals_compute._daily_close_last_checked.clear()

        posted = []
        monkeypatch.setattr(signals_compute, '_post_message',
                             lambda *a, **kw: (posted.append(a[0] if a else kw.get('text')), (None, None))[1])
        # signals_compute does `from signals_helpers import massive_dividend_
        # cross_check` (a direct-name import), so patching signals_helpers'
        # copy alone does not affect signals_compute's own bound reference --
        # the exact trap the conftest.py autouse fixture's own docstring
        # warns about. Patching the wrong module here made this test hit a
        # REAL live api.massive.com call every run (found by paired review).
        monkeypatch.setattr(signals_compute, 'massive_dividend_cross_check',
                             lambda ticker: "Massive cross-check: skipped (test)")

        node = fake_node(TICKER, 'ZScoreBreakout', window=20)

        closes_day2 = {d: v * 0.97 for d, v in closes_day1.items()}  # ~3% uniform rescale
        day2_df = _daily_df(closes_day2)
        monkeypatch.setattr(signals_compute, 'fetch_fresh_daily_closes', lambda ticker, window: day2_df)
        signals_compute.compute_buy_signal(node)

        assert any(TICKER in m and 'discontinuity' in m for m in posted), (
            f"expected a discontinuity alert to fire, got: {posted}")

        events = json.loads(signals_helpers._DAILY_CLOSE_ALERT_PATH.read_text())
        assert events.get(TICKER) == date.today().isoformat()

        # A second compute_buy_signal call the same "day" must not double-alert.
        posted.clear()
        signals_compute.compute_buy_signal(node)
        assert not posted, "must not re-alert twice the same day (dedup)"
    finally:
        cleanup_csv(TICKER)
        signals_compute._fresh_daily_cache.clear()
        signals_compute._daily_close_last_checked.clear()


def test_daily_sync_node_is_excluded_from_fresh_fetch(isolated_state, monkeypatch):
    """A daily_sync node must stay on the OLD resampled-cache path entirely --
    its whole purpose is isolating price-source TIMING as the only variable
    against a backtest replay; swapping its indicator data source would
    reintroduce exactly the confound that isolation exists to eliminate."""
    make_synthetic_csv(TICKER, last_close=95.0)
    try:
        called = []
        monkeypatch.setattr(signals_compute, 'fetch_fresh_daily_closes',
                             lambda ticker, window: called.append(1) or None)
        node = fake_node(TICKER, 'ZScoreBreakout', window=20)
        node['paper_role'] = 'daily_sync'
        signals_compute.compute_buy_signal(node)
        assert not called, "fetch_fresh_daily_closes must never be called for a daily_sync node"
    finally:
        cleanup_csv(TICKER)


def test_fetch_fresh_daily_closes_returns_none_on_fetch_failure(monkeypatch):
    """Fail-open contract, proven without a real network call: yf.Ticker(...)
    raising must return None, not propagate."""
    class _Boom:
        def __init__(self, ticker):
            raise ConnectionError("simulated network failure")
    import yfinance as yf
    monkeypatch.setattr(yf, 'Ticker', _Boom)
    result = signals_helpers.fetch_fresh_daily_closes('ANY', 20)
    assert result is None
