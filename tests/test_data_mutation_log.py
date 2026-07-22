"""
Tests for db_cache.log_data_mutation/get_data_mutations -- the traceability
log for data_manager.py's split-guard rescale (docs/research_log.md's
2026-07-22 entry: traceability, not full immutability/versioning).
"""
import pandas as pd
import pytest

import db_cache


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_cache, "DB_PATH", str(tmp_path / "test_universe.db"))
    return db_cache.DB_PATH


def _sample_df():
    return pd.DataFrame(
        {"Open": [100.0, 101.0], "High": [102.0, 103.0], "Low": [99.0, 100.0],
         "Close": [101.0, 102.0], "Volume": [1000, 1100]},
        index=pd.to_datetime(["2026-07-14 09:30", "2026-07-14 10:30"]),
    )


def test_log_and_retrieve_mutation(isolated_db):
    df = _sample_df()
    db_cache.log_data_mutation(
        ticker="KORU", factor=20.0, overlap_bar_time="2026-07-15 09:30",
        price_before=481.0, price_after=24.05, notes="split-guard rescale, 1 overlap bar(s)",
        pre_mutation_df=df,
    )
    rows = db_cache.get_data_mutations(ticker="KORU")
    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "KORU"
    assert row["factor"] == 20.0
    assert row["price_before"] == 481.0
    assert row["price_after"] == 24.05


def test_pre_mutation_snapshot_is_recoverable(isolated_db):
    df = _sample_df()
    db_cache.log_data_mutation(
        ticker="KORU", factor=20.0, overlap_bar_time="2026-07-15 09:30",
        price_before=481.0, price_after=24.05, notes="test", pre_mutation_df=df,
    )
    import sqlite3
    with sqlite3.connect(isolated_db) as conn:
        snapshot_csv = conn.execute(
            "SELECT pre_mutation_snapshot FROM data_mutation_log WHERE ticker='KORU'"
        ).fetchone()[0]
    recovered = pd.read_csv(pd.io.common.StringIO(snapshot_csv), index_col=0, parse_dates=True)
    assert recovered["Close"].tolist() == df["Close"].tolist()


def test_get_data_mutations_filters_by_ticker(isolated_db):
    df = _sample_df()
    db_cache.log_data_mutation("KORU", 20.0, "t1", 481.0, 24.05, "n", df)
    db_cache.log_data_mutation("GDXD", 10.0, "t2", 51.0, 5.1, "n", df)
    assert len(db_cache.get_data_mutations(ticker="KORU")) == 1
    assert len(db_cache.get_data_mutations(ticker="GDXD")) == 1
    assert len(db_cache.get_data_mutations()) == 2


def test_get_data_mutations_empty_table(isolated_db):
    assert db_cache.get_data_mutations() == []
