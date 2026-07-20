"""Exit-side gap-through-trigger fix (2026-07-20): SL and trailing-stop are
intrabar-continuous exit triggers just like the entry-side trailing-buy
trigger was -- an overnight/intraday gap can blow the Open past either level
before the bar's Low ever gets checked, and the kernel used to fill at the
stale theoretical stop_price/trail_stop instead of the real (worse) Open.
Mirrors test_TrailingBuyZScoreBreakout.py's entry-side gap tests."""
import sys
import numpy as np
import pandas as pd
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtester import run_backtest_v18, run_backtest_v110
from strategies import TrailingExitZScoreBreakout
from tests.conftest import cleanup_csv

CACHE_DIR = Path("./cache/research")


def _base_frame(days=90):
    dates = pd.bdate_range("2025-01-01", periods=days)
    market_hours = [9, 10, 11, 12, 13, 14, 15]
    timestamps = [
        pd.Timestamp(f"{d.date()} {h:02d}:30:00")
        for d in dates for h in market_hours
    ]
    np.random.seed(11)
    n = len(timestamps)
    closes = 100.0 + np.random.normal(0, 0.3, n)
    opens  = closes.copy()
    highs  = closes + np.abs(np.random.normal(0, 0.5, n))
    lows   = closes - np.abs(np.random.normal(0, 0.5, n))
    return timestamps, opens, highs, lows, closes


def _write(ticker, timestamps, opens, highs, lows, closes):
    df = pd.DataFrame({'Close': closes, 'Open': opens, 'High': highs, 'Low': lows}, index=timestamps)
    df.index.name = 'Datetime'
    df.to_csv(CACHE_DIR / f"{ticker}_1h.csv")


def _load(ticker):
    df = pd.read_csv(CACHE_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


# ── v1.8 (TrailingExitZScoreBreakout): bar-close entry, no bounce wait ───────

def make_v18_sl_gap_csv(ticker, signal_close=85.0, gap_open=78.0):
    """Bar -2: bar-close entry at signal_close (stop_loss=0.05 -> stop_price
    ~80.75). Bar -1: gaps straight through stop_price at its own Open (78,
    well below 80.75) -- pre-fix, kernel exits at the stale 80.75; post-fix
    it should exit at the real gap_open."""
    timestamps, opens, highs, lows, closes = _base_frame()
    opens[-2] = signal_close; closes[-2] = signal_close
    highs[-2] = signal_close + 0.1; lows[-2] = signal_close - 0.1

    opens[-1] = gap_open; closes[-1] = gap_open - 0.5
    highs[-1] = gap_open + 0.5; lows[-1] = gap_open - 1.5
    _write(ticker, timestamps, opens, highs, lows, closes)


def make_v18_trailing_gap_csv(ticker, signal_close=85.0, tp_activate=94.0, gap_open=85.0):
    """Bar -3: bar-close entry at signal_close (take_profit=0.10 -> tp_price
    93.5, stop_loss=0.05 -> stop_price 80.75). Bar -2: Close clears tp_price,
    activating trailing with peak=94. Bar -1: gaps down to 85 at its own Open
    -- with trail_pct=0.05, trail_stop from peak=94 is 89.3, so the gap Open
    (85) is well past it, but this bar's own High (86) never raises peak.
    Pre-fix, kernel exits at the stale trail_stop (~89.3); post-fix it should
    exit at the real gap_open (85)."""
    timestamps, opens, highs, lows, closes = _base_frame()
    opens[-3] = signal_close; closes[-3] = signal_close
    highs[-3] = signal_close + 0.1; lows[-3] = signal_close - 0.1

    opens[-2] = tp_activate; closes[-2] = tp_activate
    highs[-2] = tp_activate + 0.2; lows[-2] = tp_activate - 0.2

    opens[-1] = gap_open; closes[-1] = gap_open + 0.5
    highs[-1] = gap_open + 1.0; lows[-1] = gap_open - 0.5
    _write(ticker, timestamps, opens, highs, lows, closes)


def test_v18_sl_exit_fills_at_open_on_gap_through_trigger():
    ticker = '_v18_sl_gap_test'
    make_v18_sl_gap_csv(ticker)
    try:
        df = _load(ticker)
        strat = TrailingExitZScoreBreakout(window=10, z_score_threshold=2.0)
        df_daily = df.resample('D').last().dropna(subset=['Close'])
        df_ind = strat.generate_daily_indicators(df_daily)
        trades = run_backtest_v18(df, df_ind, ticker,
                                   take_profit=0.10, stop_loss=0.05, max_hours_to_hold=56,
                                   z_score_threshold=2.0, trail_pct=0.05)
        assert len(trades) > 0
        exit_px = trades[-1]['Exit Price']
        assert exit_px == pytest.approx(78.0, abs=0.01)
        assert exit_px < 80.75, "fill should not be the stale stop_price (~80.75)"
    finally:
        cleanup_csv(ticker)


def test_v18_trailing_stop_exit_fills_at_open_on_gap_through_trigger():
    ticker = '_v18_trail_gap_test'
    make_v18_trailing_gap_csv(ticker)
    try:
        df = _load(ticker)
        strat = TrailingExitZScoreBreakout(window=10, z_score_threshold=2.0)
        df_daily = df.resample('D').last().dropna(subset=['Close'])
        df_ind = strat.generate_daily_indicators(df_daily)
        trades = run_backtest_v18(df, df_ind, ticker,
                                   take_profit=0.10, stop_loss=0.05, max_hours_to_hold=56,
                                   z_score_threshold=2.0, trail_pct=0.05)
        assert len(trades) > 0
        exit_px = trades[-1]['Exit Price']
        assert exit_px == pytest.approx(85.0, abs=0.01)
        assert exit_px < 89.3, "fill should not be the stale trail_stop (~89.3)"
    finally:
        cleanup_csv(ticker)


# ── v1.10 (TrailingBothZScoreBreakout): bounce-wait entry, three resolutions ─
# trail_buy_pct=0.0 makes the bounce trigger == running_low exactly, so the
# entry price is fully deterministic and independent of the random background
# noise -- isolates these tests to the exit-side gap fix only.

def make_v110_sl_gap_csv(ticker, signal_close=85.0, entry_low=84.9, gap_open=78.0):
    """Signal detection only fires at hour9/hour14 (target_hours default), so
    the tail uses the full last trading day (hours 9-15, indices -7..-1) to
    give the bounce-wait entry room after a valid hour9 signal bar."""
    timestamps, opens, highs, lows, closes = _base_frame()
    # Bar -7 (hour9): signal bar, starts the bounce wait.
    opens[-7] = signal_close; closes[-7] = signal_close
    highs[-7] = signal_close + 0.1; lows[-7] = signal_close
    # Bar -6 (hour10): running_low dips to entry_low, High clears the (0%-trail)
    # trigger on the same bar -- entry fills at exactly entry_low.
    opens[-6] = entry_low; closes[-6] = entry_low + 0.1
    highs[-6] = entry_low + 0.15; lows[-6] = entry_low
    # Bars -5..-3: neutral hold, well above stop_price.
    for j in (-5, -4, -3):
        opens[j] = entry_low + 0.2; closes[j] = entry_low + 0.2
        highs[j] = entry_low + 0.3; lows[j] = entry_low - 0.1
    # Bar -2 (hour14): gap bar, Open blows through stop_price.
    opens[-2] = gap_open; closes[-2] = gap_open - 0.5
    highs[-2] = gap_open + 0.5; lows[-2] = gap_open - 1.5
    # Bar -1 (hour15): after exit, neutral (no new signal check at this hour).
    opens[-1] = gap_open; closes[-1] = gap_open
    highs[-1] = gap_open + 0.2; lows[-1] = gap_open - 0.2
    _write(ticker, timestamps, opens, highs, lows, closes)


def test_v110_sl_exit_fills_at_open_on_gap_through_trigger_all_resolutions():
    ticker = '_v110_sl_gap_test'
    make_v110_sl_gap_csv(ticker)
    try:
        df = _load(ticker)
        strat = TrailingExitZScoreBreakout(window=10, z_score_threshold=2.0)
        df_daily = df.resample('D').last().dropna(subset=['Close'])
        df_ind = strat.generate_daily_indicators(df_daily)
        # entry_low=84.9 -> stop_price = 84.9*0.95 = 80.655
        possible, pessimistic, certain = run_backtest_v110(
            df, df_ind, ticker,
            take_profit=0.10, stop_loss=0.05, max_hours_to_hold=56,
            z_score_threshold=2.0, trail_buy_pct=0.0, trail_pct=0.05,
            return_bounds=True,
        )
        for label, trades in [('possible', possible), ('pessimistic', pessimistic), ('certain', certain)]:
            assert len(trades) > 0, f"{label} produced no trades"
            exit_px = trades[-1]['Exit Price']
            assert exit_px == pytest.approx(78.0, abs=0.01), f"{label}: expected exit at gap Open (78.0), got {exit_px}"
            assert exit_px < 80.655, f"{label}: fill should not be the stale stop_price (~80.655)"
    finally:
        cleanup_csv(ticker)


def make_v110_trailing_gap_csv(ticker, entry_low=84.9, gap_open=85.0):
    """trail_buy_pct=0.0 keeps entry fully deterministic (identical across all
    three resolutions, same reasoning as make_v110_sl_gap_csv), isolating this
    to the trailing-stop exit-side gap fix. Bar -14 (hour9, day D-1): signal
    bar. Bar -13 (hour10): running_low dips to entry_low, High clears the
    (0%-trail) trigger same bar -- entry fills at exactly entry_low (84.9).
    Bar -12 (hour11): Close clears tp_price (93.39) -- arms trailing,
    peak=93.5. Bars -11/-10 (hour12/13): build peak to 96 then 99 via High,
    no exit. Bar -9 (hour14): gap bar -- Open (85.0) blows straight through
    the trail_stop confirmed from peak=99 (94.05), but this bar's own High
    (85.5) never comes close to 94.05 either -- pre-fix, the kernel would
    still exit this same bar (Low 83.5 <= 94.05) but at the stale 94.05, a
    price that never actually traded; post-fix it exits at the real Open
    (85.0). Bar -8 (hour15) and everything before -14 is left as background
    noise -- hour15 never checks for a new signal (target_hours=(9,14)), and
    in_trade gates out the signal-check branch entirely while a trade is
    open, so neither can spuriously re-trigger."""
    timestamps, opens, highs, lows, closes = _base_frame()
    opens[-14] = 85.0; closes[-14] = 85.0
    highs[-14] = 85.1; lows[-14] = 85.0

    opens[-13] = entry_low; closes[-13] = entry_low + 0.1
    highs[-13] = entry_low + 0.15; lows[-13] = entry_low

    opens[-12] = 85.0; closes[-12] = 93.5
    highs[-12] = 93.6; lows[-12] = 84.8

    opens[-11] = 94.0; closes[-11] = 95.5
    highs[-11] = 96.0; lows[-11] = 95.0

    opens[-10] = 96.5; closes[-10] = 98.0
    highs[-10] = 99.0; lows[-10] = 97.5

    opens[-9] = gap_open; closes[-9] = gap_open - 0.5
    highs[-9] = gap_open + 0.5; lows[-9] = gap_open - 1.5

    opens[-8] = gap_open; closes[-8] = gap_open
    highs[-8] = gap_open + 0.2; lows[-8] = gap_open - 0.2

    _write(ticker, timestamps, opens, highs, lows, closes)


def test_v110_trailing_stop_exit_fills_at_open_on_gap_through_trigger_all_resolutions():
    ticker = '_v110_trail_gap_test'
    make_v110_trailing_gap_csv(ticker)
    try:
        df = _load(ticker)
        strat = TrailingExitZScoreBreakout(window=10, z_score_threshold=2.0)
        df_daily = df.resample('D').last().dropna(subset=['Close'])
        df_ind = strat.generate_daily_indicators(df_daily)
        # peak builds to 99 before the gap bar -> trail_stop = 99*0.95 = 94.05
        possible, pessimistic, certain = run_backtest_v110(
            df, df_ind, ticker,
            take_profit=0.10, stop_loss=0.05, max_hours_to_hold=56,
            z_score_threshold=2.0, trail_buy_pct=0.0, trail_pct=0.05,
            return_bounds=True,
        )
        for label, trades in [('possible', possible), ('pessimistic', pessimistic), ('certain', certain)]:
            assert len(trades) > 0, f"{label} produced no trades"
            exit_px = trades[-1]['Exit Price']
            assert exit_px == pytest.approx(85.0, abs=0.01), f"{label}: expected exit at gap Open (85.0), got {exit_px}"
            assert exit_px < 94.05, f"{label}: fill should not be the stale trail_stop (~94.05), which never traded"
    finally:
        cleanup_csv(ticker)
