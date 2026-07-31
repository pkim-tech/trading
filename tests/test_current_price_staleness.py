"""Tests for signals_compute._current_price's staleness guard (widened
2026-07-31 from a same-calendar-day check to a straight bar-age check --
weekends/overnight have no real new pricing data to test this against, so
this fakes the cache contents and the clock instead of relying on real
market data)."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_compute

TICKER = 'TEST_STALENESS'


def _fake_df(last_ts):
    idx = pd.date_range(end=last_ts, periods=5, freq='h')
    return pd.DataFrame({'Close': [100.0, 101.0, 102.0, 103.0, 104.0]}, index=idx)


class _FrozenDatetime(datetime):
    _now = None

    @classmethod
    def now(cls, tz=None):
        return cls._now


def _freeze(monkeypatch, now):
    frozen = type('_Frozen', (_FrozenDatetime,), {'_now': now})
    monkeypatch.setattr(signals_compute, 'datetime', frozen)


def test_recent_bar_within_max_age_is_fresh(monkeypatch):
    last_ts = datetime(2026, 7, 27, 14, 30)  # Monday
    monkeypatch.setattr(signals_compute, '_load_cache', lambda t: (_fake_df(last_ts), None))
    _freeze(monkeypatch, last_ts + timedelta(minutes=45))
    price, ts = signals_compute._current_price(TICKER)
    assert price == 104.0
    assert ts == last_ts


def test_bar_just_past_max_age_is_stale(monkeypatch):
    last_ts = datetime(2026, 7, 27, 14, 30)
    monkeypatch.setattr(signals_compute, '_load_cache', lambda t: (_fake_df(last_ts), None))
    _freeze(monkeypatch, last_ts + signals_compute._STALE_PRICE_MAX_AGE + timedelta(minutes=1))
    price, ts = signals_compute._current_price(TICKER)
    assert (price, ts) == (None, None)


def test_same_day_bar_hours_old_is_stale(monkeypatch):
    """The exact GDXU incident shape (2026-07-28): the day's last bar (15:30)
    checked hours later the SAME calendar day -- a date-only check would have
    let this through as 'fresh'; the age check correctly rejects it."""
    last_ts = datetime(2026, 7, 27, 15, 30)
    monkeypatch.setattr(signals_compute, '_load_cache', lambda t: (_fake_df(last_ts), None))
    _freeze(monkeypatch, datetime(2026, 7, 27, 23, 0))  # same date, hours later
    price, ts = signals_compute._current_price(TICKER)
    assert (price, ts) == (None, None)


def test_prior_day_cache_is_stale(monkeypatch):
    last_ts = datetime(2026, 7, 24, 13, 30)  # Friday close
    monkeypatch.setattr(signals_compute, '_load_cache', lambda t: (_fake_df(last_ts), None))
    _freeze(monkeypatch, datetime(2026, 7, 27, 10, 0))  # Monday morning
    price, ts = signals_compute._current_price(TICKER)
    assert (price, ts) == (None, None)


def test_prior_day_cache_on_weekend_is_stale(monkeypatch):
    """Old date-only guard's `now.weekday() < 5` clause meant a weekend poll
    never triggered the guard at all -- a Saturday check against Friday's
    close silently passed as 'fresh'. Age check catches it regardless of
    weekday."""
    last_ts = datetime(2026, 7, 24, 13, 30)  # Friday close
    monkeypatch.setattr(signals_compute, '_load_cache', lambda t: (_fake_df(last_ts), None))
    _freeze(monkeypatch, datetime(2026, 7, 25, 10, 0))  # Saturday
    price, ts = signals_compute._current_price(TICKER)
    assert (price, ts) == (None, None)


def test_no_cache_returns_none(monkeypatch):
    monkeypatch.setattr(signals_compute, '_load_cache', lambda t: (None, None))
    price, ts = signals_compute._current_price(TICKER)
    assert (price, ts) == (None, None)
