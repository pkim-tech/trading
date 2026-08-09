"""Pins the 2026-08-09 certain-resolution kernel fix (backtester.py's
_simulate_trail_both certain branch + the standalone _simulate_certain_only)
so neither regresses silently the way the two stale Python mirrors
(export_trades.py, verify_fill_resolution_accuracy.py) did before being
caught by a paired Opus session-wrap review. Two real cases:

1. Frozen-trigger: a bar whose own Low doesn't move running_low has its
   trigger fixed for the entire bar -- a High-touch is exactly as certain as
   an Open-gap and must fill immediately, not defer to Close-confirmation.
2. Close-confirm bound: a bar whose own Low DOES move running_low (genuinely
   ambiguous) but whose Close still clears the new trigger must credit
   min(buy_trigger_prior, high) -- the true worst-case price provable over
   both intrabar orderings -- not the bar's raw Close (which has no proven
   relation to the real fill) and not the old buggy buy_trigger_updated
   (which was optimistic/unobtainable).
"""
import sys
import numpy as np
import pandas as pd
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtester import run_backtest_v110, run_backtest_certain_only, prep_inputs
from strategies import TrailingBothZScoreBreakout
from tests.conftest import cleanup_csv


def _base_frame(days=90):
    dates = pd.bdate_range("2025-01-01", periods=days)
    market_hours = [9, 10, 11, 12, 13, 14, 15]
    timestamps = [
        pd.Timestamp(f"{d.date()} {h:02d}:30:00")
        for d in dates for h in market_hours
    ]
    np.random.seed(3)
    n = len(timestamps)
    closes = 100.0 + np.random.normal(0, 0.3, n)
    opens = closes.copy()
    highs = closes + np.abs(np.random.normal(0, 0.5, n))
    lows = closes - np.abs(np.random.normal(0, 0.5, n))
    return timestamps, opens, highs, lows, closes


def _write(ticker, timestamps, opens, highs, lows, closes):
    df = pd.DataFrame({'Close': closes, 'Open': opens, 'High': highs, 'Low': lows}, index=timestamps)
    df.index.name = 'Datetime'
    df.to_csv(Path('./cache/research') / f"{ticker}_1h.csv")


def make_frozen_trigger_csv(ticker):
    """Signal bar Close=85 (a real dip well below the lower band, like the
    project's existing gap/dip-bounce tests) -- running_low=85,
    trigger=89.25 @ trail_buy_pct=5%. Next bar's own Low (85.5) doesn't
    undercut 85 -- trigger stays frozen at 89.25 -- and its High (90) clears
    it. Certain must fill THIS bar at 89.25, not defer to Close-confirmation
    (Close=88 < 89.25, so the pre-fix code would have deferred for days)."""
    timestamps, opens, highs, lows, closes = _base_frame()
    opens[-2] = 85.0; closes[-2] = 85.0; highs[-2] = 85.1; lows[-2] = 85.0
    opens[-1] = 86.0; highs[-1] = 90.0; lows[-1] = 85.5; closes[-1] = 88.0
    _write(ticker, timestamps, opens, highs, lows, closes)


def make_close_confirm_bound_csv(ticker):
    """Signal bar Close=85 (running_low=85, trigger_prior=89.25). Next bar's
    own Low (75) DOES undercut 85 -- genuinely ambiguous -- giving
    buy_trigger_updated=78.75. Close=79 confirms a fill, but the true
    worst-case price is min(buy_trigger_prior=89.25, high=80)=80, not the raw
    Close (79) and not the old buggy buy_trigger_updated (78.75)."""
    timestamps, opens, highs, lows, closes = _base_frame()
    opens[-2] = 85.0; closes[-2] = 85.0; highs[-2] = 85.1; lows[-2] = 85.0
    opens[-1] = 76.0; highs[-1] = 80.0; lows[-1] = 75.0; closes[-1] = 79.0
    _write(ticker, timestamps, opens, highs, lows, closes)


def _run_certain(ticker):
    df = pd.read_csv(f'cache/research/{ticker}_1h.csv', index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    strat = TrailingBothZScoreBreakout(window=10, z_score_threshold=2.0)
    df_daily = df.resample('D').last().dropna(subset=['Close'])
    df_ind = strat.generate_daily_indicators(df_daily)
    _, _, certain = run_backtest_v110(
        df, df_ind, ticker,
        take_profit=0.10, stop_loss=0.15, max_hours_to_hold=56,
        z_score_threshold=2.0, trail_buy_pct=0.05, trail_pct=0.05,
        return_bounds=True,
    )
    return certain


def test_certain_fills_immediately_on_frozen_trigger_bar():
    ticker = '_certain_frozen_test'
    try:
        make_frozen_trigger_csv(ticker)
        certain = _run_certain(ticker)
        assert len(certain) > 0, "certain produced no trades"
        entry = certain[0]['Entry Price']
        assert entry == pytest.approx(89.25, abs=0.01), (
            f"expected immediate fill at the frozen trigger (89.25), got {entry} "
            "-- pre-fix, certain would have deferred this bar entirely since "
            "Close (88.0) never confirms it"
        )
    finally:
        cleanup_csv(ticker)


def test_certain_close_confirm_credits_worst_case_bound_not_close_or_stale_trigger():
    ticker = '_certain_bound_test'
    try:
        make_close_confirm_bound_csv(ticker)
        certain = _run_certain(ticker)
        assert len(certain) > 0, "certain produced no trades"
        entry = certain[0]['Entry Price']
        assert entry == pytest.approx(80.0, abs=0.01), (
            f"expected min(buy_trigger_prior=89.25, high=80)=80.0, got {entry} "
            "-- must not be the raw Close (79.0, no proven relation to the "
            "real fill) or the old buggy buy_trigger_updated (78.75, an "
            "unobtainable optimistic price)"
        )
    finally:
        cleanup_csv(ticker)


@pytest.mark.parametrize("ticker", ['AGQ', 'SOXL'])
def test_simulate_certain_only_matches_bundled_kernel_on_real_data(ticker):
    """_simulate_certain_only must stay byte-identical to the corrected
    _simulate_trail_both certain branch -- this is the actual enforcement the
    contextual Opus review flagged as missing (previously a one-off manual
    check, not a pinned test)."""
    cache = Path(f'cache/research/{ticker}_1h.csv')
    if not cache.exists():
        pytest.skip(f"no cached data for {ticker}")
    df = pd.read_csv(cache, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    strat = TrailingBothZScoreBreakout(window=10, z_score_threshold=1.0)
    df_daily = df.resample('D').last().dropna(subset=['Close'])
    df_ind = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df, df_ind)
    kwargs = dict(target_hours=(9, 14), take_profit=0.29, stop_loss=0.02, max_hours_to_hold=70,
                  z_score_threshold=1.0, trail_buy_pct=0.05, trail_pct=0.07,
                  entry_timing='open_check', prep=p)

    _, _, bundled_certain = run_backtest_v110(df, df_ind, ticker, return_bounds=True, **kwargs)
    lean_certain = run_backtest_certain_only(df, df_ind, ticker, **kwargs)

    assert len(bundled_certain) == len(lean_certain), (
        f"{ticker}: bundled n={len(bundled_certain)} vs lean n={len(lean_certain)}"
    )
    for i, (a, b) in enumerate(zip(bundled_certain, lean_certain)):
        assert a['Entry Price'] == pytest.approx(b['Entry Price']), f"{ticker} trade {i}: entry price mismatch"
        assert a['Exit Price'] == pytest.approx(b['Exit Price']), f"{ticker} trade {i}: exit price mismatch"
        assert a['Entry Time'] == b['Entry Time'], f"{ticker} trade {i}: entry time mismatch"
