import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.checklist_v65 import _has_ground_truth_v6


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


def test_true_when_real_ground_truth_v6_rows_present():
    """A ticker GT has actually swept must be refused by checklist_v65.py's
    legacy hourly replay() -- same bug class as locate_best_node.py's
    resolve_version() guard (a710c4c)."""
    conn = _conn()
    _insert(conn, "AGQ", "v5")
    _insert(conn, "AGQ", "v6", kernel_version="ground_truth_v6")
    assert _has_ground_truth_v6(conn, "AGQ") is True


def test_false_on_zero_trade_ground_truth_v6_rows():
    """A v6 row that never computed a real trade shouldn't count as 'this
    ticker has GT data' -- matches trades>0 used everywhere else."""
    conn = _conn()
    _insert(conn, "AGQ", "v5")
    _insert(conn, "AGQ", "v6", trades=0, kernel_version="ground_truth_v6")
    assert _has_ground_truth_v6(conn, "AGQ") is False


def test_false_when_ticker_has_no_data_at_all():
    conn = _conn()
    assert _has_ground_truth_v6(conn, "NODATA") is False


def test_false_for_legacy_only_ticker():
    conn = _conn()
    _insert(conn, "SOXL", "v5")
    _insert(conn, "SOXL", "v5.1")
    assert _has_ground_truth_v6(conn, "SOXL") is False
