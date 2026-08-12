import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.candidate_summary_report import resolve_version


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE backtest_cache (
            strategy TEXT, version TEXT, ticker TEXT, trades INTEGER
        )
    """)
    return conn


def _insert(conn, ticker, version, trades=10):
    conn.execute("INSERT INTO backtest_cache (strategy, version, ticker, trades) VALUES (?, ?, ?, ?)",
                  ("TrailingBothZScoreBreakout", version, ticker, trades))
    conn.commit()


def test_prefers_v51_when_present():
    conn = _conn()
    _insert(conn, "DFEN", "v5")
    _insert(conn, "DFEN", "v5.1")
    assert resolve_version(conn, "DFEN") == "v5.1"


def test_falls_back_to_v5_when_no_v51_rows():
    conn = _conn()
    _insert(conn, "SOXL", "v5")
    assert resolve_version(conn, "SOXL") == "v5"


def test_ignores_v51_rows_with_zero_trades():
    """A v5.1 row that exists but never actually computed a real trade
    (trades=0) shouldn't count as 'this ticker has v5.1 data' -- matches
    every other query in this module (`trades > 0`)."""
    conn = _conn()
    _insert(conn, "GDXU", "v5", trades=10)
    _insert(conn, "GDXU", "v5.1", trades=0)
    assert resolve_version(conn, "GDXU") == "v5"


def test_defaults_to_last_preferred_when_ticker_has_no_data_at_all():
    conn = _conn()
    assert resolve_version(conn, "NODATA") == "v5"
