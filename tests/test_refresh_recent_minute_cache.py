"""Unit coverage for scripts/refresh_recent_minute_cache.py (Task #11, 2026-09-01):
the same-day/recent-days minute cache built after the nightly-canonical-refresh design
was abandoned (real reproducibility risk, see that script's own docstring). Real
Massive.com API calls (fetch_ticker) are always monkeypatched -- never hit the real
API in a test. Every test uses an isolated tmp_path db, never the real
cache/research/massive_minute_recent.db."""
from datetime import date, timedelta

import pandas as pd
import pytest

from scripts import refresh_recent_minute_cache as cache


def _rows(timestamps_prices):
    return [
        {"t": int(pd.Timestamp(ts, tz="America/New_York").tz_convert("UTC").timestamp() * 1000),
         "o": price, "h": price, "l": price, "c": price, "v": 100.0, "vw": price, "n": 1}
        for ts, price in timestamps_prices
    ]


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test_massive_minute_recent.db"


def test_ensure_table_idempotent(db_path):
    cache.ensure_table(db_path)
    cache.ensure_table(db_path)  # must not error on a second call


def test_refresh_ticker_caches_real_rows(db_path, monkeypatch):
    cache.ensure_table(db_path)

    def fake_fetch_ticker(ticker, start, end):
        return _rows([("2026-08-30 09:30:00", 50.0), ("2026-08-30 09:31:00", 50.1)])

    monkeypatch.setattr(cache, "fetch_ticker", fake_fetch_ticker)
    n = cache.refresh_ticker("TESTTICKER", db_path=db_path)
    assert n == 2

    df = cache.load_recent_cache("TESTTICKER", db_path=db_path)
    assert len(df) == 2
    assert list(df.columns) == ["Open", "High", "Low", "Close"]
    assert df.index.tz is None  # matches get_massive_minute_ohlcv's tz-naive convention


def test_refresh_ticker_replaces_prior_rows(db_path, monkeypatch):
    """Full-replace semantics (delete + reinsert), not accumulate -- a stale row from
    3 refreshes ago must not linger forever in a 'disposable rolling window' cache."""
    cache.ensure_table(db_path)

    monkeypatch.setattr(cache, "fetch_ticker",
                         lambda t, s, e: _rows([("2026-08-25 09:30:00", 10.0)]))
    cache.refresh_ticker("REPLACETEST", db_path=db_path)

    monkeypatch.setattr(cache, "fetch_ticker",
                         lambda t, s, e: _rows([("2026-08-30 09:30:00", 20.0)]))
    cache.refresh_ticker("REPLACETEST", db_path=db_path)

    df = cache.load_recent_cache("REPLACETEST", db_path=db_path)
    assert len(df) == 1  # the 2026-08-25 row is gone, not accumulated
    assert df["Close"].iloc[0] == 20.0


def test_refresh_ticker_uses_recent_days_window(db_path, monkeypatch):
    captured = {}

    def fake_fetch_ticker(ticker, start, end):
        captured["start"] = start
        captured["end"] = end
        return []

    monkeypatch.setattr(cache, "fetch_ticker", fake_fetch_ticker)
    cache.ensure_table(db_path)
    cache.refresh_ticker("WINDOWTEST", recent_days=10, db_path=db_path)

    assert captured["end"] == date.today()
    assert captured["start"] == date.today() - timedelta(days=10)


def test_refresh_ticker_no_data_leaves_prior_cache_untouched(db_path, monkeypatch):
    cache.ensure_table(db_path)
    monkeypatch.setattr(cache, "fetch_ticker",
                         lambda t, s, e: _rows([("2026-08-30 09:30:00", 10.0)]))
    cache.refresh_ticker("QUIETTEST", db_path=db_path)

    monkeypatch.setattr(cache, "fetch_ticker", lambda t, s, e: [])
    n = cache.refresh_ticker("QUIETTEST", db_path=db_path)
    assert n == 0

    df = cache.load_recent_cache("QUIETTEST", db_path=db_path)
    assert len(df) == 1  # prior cache preserved, not wiped by an empty API response


def test_load_recent_cache_never_cached_ticker_returns_empty_frame(db_path):
    cache.ensure_table(db_path)
    df = cache.load_recent_cache("NEVERCACHED", db_path=db_path)
    assert df.empty
    assert list(df.columns) == ["Open", "High", "Low", "Close"]


def test_load_recent_cache_missing_db_file_returns_empty_frame(tmp_path):
    missing = tmp_path / "does_not_exist.db"
    df = cache.load_recent_cache("ANYTICKER", db_path=missing)
    assert df.empty
    assert list(df.columns) == ["Open", "High", "Low", "Close"]


def test_load_recent_cache_scoped_to_ticker(db_path, monkeypatch):
    cache.ensure_table(db_path)
    monkeypatch.setattr(cache, "fetch_ticker",
                         lambda t, s, e: _rows([("2026-08-30 09:30:00", 10.0)]))
    cache.refresh_ticker("TICKERA", db_path=db_path)
    cache.refresh_ticker("TICKERB", db_path=db_path)

    assert len(cache.load_recent_cache("TICKERA", db_path=db_path)) == 1
    assert len(cache.load_recent_cache("TICKERB", db_path=db_path)) == 1
    assert len(cache.load_recent_cache("TICKERC", db_path=db_path)) == 0
