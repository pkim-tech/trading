import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.candidate_summary_report import resolve_version


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE backtest_cache (
            strategy TEXT, version TEXT, ticker TEXT, trades INTEGER, kernel_version TEXT
        )
    """)
    return conn


def _insert(conn, ticker, version, trades=10, kernel_version=None):
    conn.execute("INSERT INTO backtest_cache (strategy, version, ticker, trades, kernel_version) "
                 "VALUES (?, ?, ?, ?, ?)",
                  ("TrailingBothZScoreBreakout", version, ticker, trades, kernel_version))
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


def test_refuses_when_real_ground_truth_v6_rows_present():
    """A ticker GT has actually swept must never silently resolve to stale
    v5/v5.1 data -- see scripts/locate_best_node.py's resolve_version()."""
    conn = _conn()
    _insert(conn, "AGQ", "v5")
    _insert(conn, "AGQ", "v6", kernel_version="ground_truth_v6")
    try:
        resolve_version(conn, "AGQ")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "ground_truth_v6" in str(e)


def test_does_not_refuse_on_zero_trade_ground_truth_v6_rows():
    """A v6 row that never computed a real trade shouldn't count as 'this
    ticker has GT data' -- matches the trades>0 condition used everywhere else."""
    conn = _conn()
    _insert(conn, "AGQ", "v5")
    _insert(conn, "AGQ", "v6", trades=0, kernel_version="ground_truth_v6")
    assert resolve_version(conn, "AGQ") == "v5"
