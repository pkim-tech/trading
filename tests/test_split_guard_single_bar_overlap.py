"""
Regression test for the 2026-08-16 fix to data_manager.fetch_live_data_smart's split-guard:
a 1-bar overlap between the local cache and a fresh delta fetch used to hardcode
`consistent=True` (no way to compute a std-dev-based check from a single point), meaning one
corrupted/artifact bar whose price ratio happened to look like a split factor could drive a
spurious whole-history rescale. Now gated on signals_helpers.real_split_confirmed_since
(the same real-confirmed-split check _apply_split_artifact_fix already uses) instead of
trusting the price ratio alone when only 1 bar overlaps.
"""
import pandas as pd
import pytest

import data_manager


def _write_local_cache(path, index, close):
    pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": [1000] * len(index)},
        index=index,
    ).to_csv(path)


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(data_manager, "CACHE_DIR", tmp_path)
    return tmp_path


def _mock_download_factory(overlap_ts, ratio):
    """yf.download mock returning one overlapping bar at `ratio`x the local cache's price
    (simulating either a real split or a corrupted single-bar artifact, depending on the
    test) plus one new bar so the sync path has something fresh to append."""
    def _mock(ticker, period, interval):
        idx = pd.to_datetime([overlap_ts, overlap_ts + pd.Timedelta(hours=1)])
        close = [100.0 / ratio, 101.0 / ratio]
        return pd.DataFrame(
            {"Open": close, "High": close, "Low": close, "Close": close, "Volume": [1000, 1000]},
            index=idx,
        )
    return _mock


def test_single_bar_overlap_does_not_rescale_without_real_split_confirmation(isolated_cache, monkeypatch):
    # Two cached bars: an EARLIER one that never appears in the fresh delta (so its price
    # can only change via the split-guard's whole-history rescale, never via the unrelated
    # dedup-on-fresh-data step), plus the overlap bar itself.
    earlier_ts = pd.Timestamp("2026-08-13 09:30")
    overlap_ts = pd.Timestamp("2026-08-14 09:30")
    _write_local_cache(isolated_cache / "TEST_1h.csv", pd.to_datetime([earlier_ts, overlap_ts]), [100.0, 100.0])

    monkeypatch.setattr(data_manager.yf, "download", _mock_download_factory(overlap_ts, ratio=20.0))
    monkeypatch.setattr(data_manager, "real_split_confirmed_since", lambda ticker, since: False)
    monkeypatch.setattr(data_manager, "_apply_split_artifact_fix", lambda ticker, df: df)

    df_daily, df_hourly = data_manager.fetch_live_data_smart("TEST")

    # No real confirmed split -> the EARLIER bar (untouched by dedup) must NOT have been
    # rescaled by the bogus 20x ratio (would land at 5.0 if the bug were still present).
    assert df_hourly.loc[earlier_ts, "Close"] == pytest.approx(100.0)


def test_single_bar_overlap_rescales_with_real_split_confirmation(isolated_cache, monkeypatch):
    earlier_ts = pd.Timestamp("2026-08-13 09:30")
    overlap_ts = pd.Timestamp("2026-08-14 09:30")
    _write_local_cache(isolated_cache / "TEST_1h.csv", pd.to_datetime([earlier_ts, overlap_ts]), [100.0, 100.0])

    monkeypatch.setattr(data_manager.yf, "download", _mock_download_factory(overlap_ts, ratio=20.0))
    monkeypatch.setattr(data_manager, "real_split_confirmed_since", lambda ticker, since: True)
    monkeypatch.setattr(data_manager, "_apply_split_artifact_fix", lambda ticker, df: df)
    monkeypatch.setattr(data_manager.db_cache, "log_data_mutation", lambda *a, **k: None)

    df_daily, df_hourly = data_manager.fetch_live_data_smart("TEST")

    # Real confirmed split -> the EARLIER bar SHOULD be rescaled to 5.0 (100/20).
    assert df_hourly.loc[earlier_ts, "Close"] == pytest.approx(5.0)
