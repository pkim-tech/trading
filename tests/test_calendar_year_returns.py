import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.calendar_year_returns import calendar_year_breakdown, format_calendar_years


def _df_h(timestamps):
    return pd.DataFrame(index=pd.DatetimeIndex(timestamps))


def test_buckets_by_exit_year_not_entry_year():
    # entry in 2024, exit in 2025 -- must land in 2025 (realized/taxable at exit)
    ts = pd.date_range("2024-12-20", periods=10, freq="D")
    df_h = _df_h(ts)
    trades = [{"entry_i": 0, "exit_i": 9, "ret": 0.10}]  # exits 2024-12-29 -> still 2024
    breakdown = calendar_year_breakdown(trades, df_h)
    assert set(breakdown.keys()) == {2024}
    assert breakdown[2024]["trades"] == 1


def test_compounds_within_a_year():
    ts = pd.date_range("2025-01-01", periods=5, freq="D")
    df_h = _df_h(ts)
    trades = [
        {"entry_i": 0, "exit_i": 1, "ret": 0.10},
        {"entry_i": 2, "exit_i": 3, "ret": -0.05},
    ]
    breakdown = calendar_year_breakdown(trades, df_h)
    expected = (1.10 * 0.95 - 1.0) * 100.0
    assert breakdown[2025]["trades"] == 2
    assert breakdown[2025]["compounded_pct"] == pytest.approx(expected)


def test_ytd_flag_only_on_current_year():
    # df_h's last row defines "now" -- 2026 is the latest year present, so only it is YTD
    ts = list(pd.date_range("2024-06-01", periods=3, freq="D")) + \
         list(pd.date_range("2025-06-01", periods=3, freq="D")) + \
         list(pd.date_range("2026-06-01", periods=3, freq="D"))
    df_h = _df_h(ts)
    trades = [
        {"entry_i": 0, "exit_i": 1, "ret": 0.05},
        {"entry_i": 3, "exit_i": 4, "ret": 0.05},
        {"entry_i": 6, "exit_i": 7, "ret": 0.05},
    ]
    breakdown = calendar_year_breakdown(trades, df_h)
    assert breakdown[2024]["ytd"] is False
    assert breakdown[2025]["ytd"] is False
    assert breakdown[2026]["ytd"] is True


def test_skips_trades_with_no_exit():
    ts = pd.date_range("2025-01-01", periods=3, freq="D")
    df_h = _df_h(ts)
    trades = [{"entry_i": 0, "exit_i": None, "ret": None}]
    breakdown = calendar_year_breakdown(trades, df_h)
    assert breakdown == {}


def test_empty_trades_returns_empty():
    df_h = _df_h(pd.date_range("2025-01-01", periods=3, freq="D"))
    assert calendar_year_breakdown([], df_h) == {}


def test_format_calendar_years():
    breakdown = {
        2024: {"compounded_pct": 42.1, "trades": 3, "ytd": False},
        2025: {"compounded_pct": 9.7, "trades": 1, "ytd": True},
    }
    assert format_calendar_years(breakdown) == "2024:+42.1% 2025(YTD):+9.7%"
    assert format_calendar_years({}) == "-"
