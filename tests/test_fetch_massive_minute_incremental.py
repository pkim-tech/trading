"""Unit coverage for scripts/fetch_massive_minute_incremental.py (Task #11,
2026-08-31): the merge/dedup logic is the one real correctness risk this
script introduces (promote_market_data_pull.py's own narrowing guard is
already covered by that script's existing usage -- this test suite is about
proving refresh_ticker() never hands it a narrower-than-canonical staged
pull). Real Massive.com API calls (fetch_ticker) are always monkeypatched --
never hit the real API in a test."""
from datetime import date

import pandas as pd
import pytest

from scripts import fetch_massive_minute_incremental as inc


def _rows(timestamps_prices):
    """timestamps_prices: list of (iso_str, close_price) -> raw API row dicts,
    matching the real Massive.com aggs response shape _rows_to_df expects."""
    return [
        {"t": int(pd.Timestamp(ts, tz="America/New_York").tz_convert("UTC").timestamp() * 1000),
         "o": price, "h": price, "l": price, "c": price, "v": 100.0, "vw": price, "n": 1}
        for ts, price in timestamps_prices
    ]


@pytest.fixture(autouse=True)
def _isolate_dirs(tmp_path, monkeypatch):
    """Every test in this file gets OUT_DIR/PULLS_DIR patched to tmp_path --
    nothing here ever touches the real cache/research/minute_data/."""
    monkeypatch.setattr(inc, "OUT_DIR", tmp_path)
    monkeypatch.setattr(inc, "PULLS_DIR", tmp_path / "pulls")


def _write_canonical(ticker, rows):
    path = inc._canonical_path(ticker)
    inc._rows_to_df(rows).to_csv(path, index=False)
    return path


def test_no_canonical_file_falls_back_to_full_pull(monkeypatch):
    captured = {}

    def fake_fetch_ticker(ticker, start, end):
        captured["start"] = start
        captured["end"] = end
        return _rows([("2024-01-02 09:30:00", 10.0), ("2024-01-02 09:31:00", 10.1)])

    monkeypatch.setattr(inc, "fetch_ticker", fake_fetch_ticker)

    out_path = inc.refresh_ticker("FAKETICKER")
    assert captured["start"] == inc.CANONICAL_FALLBACK_START
    assert out_path is not None
    assert out_path.exists()
    assert len(pd.read_csv(out_path)) == 2


def test_incremental_refresh_fetches_from_overlap_window(monkeypatch):
    _write_canonical("UGLTEST", _rows([("2026-08-20 09:30:00", 100.0),
                                        ("2026-08-21 15:59:00", 101.0)]))
    captured = {}

    def fake_fetch_ticker(ticker, start, end):
        captured["start"] = start
        captured["end"] = end
        return _rows([("2026-08-24 09:30:00", 102.0)])

    monkeypatch.setattr(inc, "fetch_ticker", fake_fetch_ticker)
    out_path = inc.refresh_ticker("UGLTEST", overlap_days=3)

    # last canonical bar is 2026-08-21 -> fetch_start must be last_ts - 3 days = 2026-08-18
    assert captured["start"] == date(2026, 8, 18)
    assert captured["end"] == date.today()
    assert out_path is not None


def test_merge_dedups_overlap_keeping_fresh_pull_value(monkeypatch):
    """The core correctness claim: a bar that exists in BOTH canonical and the
    fresh pull (the overlap window) must resolve to the FRESH pull's value
    (upstream corrections in the overlap window must win), and the merged
    result's start date must be UNCHANGED from canonical's (never narrower --
    the exact property promote_market_data_pull.py's guard depends on)."""
    _write_canonical("KORUTEST", _rows([("2026-08-15 09:30:00", 50.0),
                                         ("2026-08-20 09:30:00", 55.0),  # "corrected" below
                                         ("2026-08-21 15:59:00", 56.0)]))

    def fake_fetch_ticker(ticker, start, end):
        return _rows([("2026-08-20 09:30:00", 999.0),  # corrected overlap-window value
                       ("2026-08-24 09:30:00", 60.0)])  # genuinely new bar

    monkeypatch.setattr(inc, "fetch_ticker", fake_fetch_ticker)
    out_path = inc.refresh_ticker("KORUTEST", overlap_days=3)

    assert out_path is not None
    df = pd.read_csv(out_path, parse_dates=["timestamp"])
    assert len(df) == 4  # 3 original + 1 genuinely new; overlap bar deduped, not duplicated

    corrected_row = df[df["timestamp"] == pd.Timestamp("2026-08-20 09:30:00", tz="America/New_York")]
    assert corrected_row["Close"].iloc[0] == 999.0  # fresh pull won, not the stale canonical value

    # never narrower than canonical's real start
    assert df["timestamp"].min() == pd.Timestamp("2026-08-15 09:30:00", tz="America/New_York")
    # filename encodes the true (unchanged) start date, matching promote_market_data_pull.py's
    # narrowing guard's expectation
    assert "2026-08-15" in out_path.name


def test_no_new_bars_returns_none_and_stages_nothing(monkeypatch):
    _write_canonical("QUIETTICKER", _rows([("2026-08-20 09:30:00", 10.0),
                                            ("2026-08-21 15:59:00", 11.0)]))

    def fake_fetch_ticker(ticker, start, end):
        # API returns only bars already covered by canonical (nothing genuinely new)
        return _rows([("2026-08-21 15:59:00", 11.0)])

    monkeypatch.setattr(inc, "fetch_ticker", fake_fetch_ticker)
    out_path = inc.refresh_ticker("QUIETTICKER", overlap_days=3)

    assert out_path is None
    assert not (inc.PULLS_DIR / "QUIETTICKER_1m_2026-08-20_2026-08-21.csv").exists()


def test_fetch_returns_no_rows_at_all(monkeypatch):
    _write_canonical("EMPTYRESPTICKER", _rows([("2026-08-20 09:30:00", 10.0)]))

    monkeypatch.setattr(inc, "fetch_ticker", lambda ticker, start, end: [])
    assert inc.refresh_ticker("EMPTYRESPTICKER", overlap_days=3) is None
