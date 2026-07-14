import sys
import numpy as np
import pandas as pd
import pytest
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtester import run_backtest_v19, run_backtest_v110
from active_signals import compute_buy_signal, check_sell_condition
from strategies import TrailingBuyZScoreBreakout
from tests.conftest import make_synthetic_csv, cleanup_csv, fake_node, fake_position

TICKER_BUY  = 'TEST_TRBUY'
TICKER_BOTH = 'TEST_TRBOTH'
STRAT_BUY   = 'TrailingBuyZScoreBreakout'
STRAT_BOTH  = 'TrailingBothZScoreBreakout'


def node_buy(**kw):  return fake_node(TICKER_BUY,  STRAT_BUY,  **kw)
def node_both(**kw): return fake_node(TICKER_BOTH, STRAT_BOTH, **kw)
def pos_buy(**kw):   return fake_position(TICKER_BUY,  STRAT_BUY,  **kw)


def make_dip_bounce_csv(ticker, signal_close=85.0, dip_low=82.0, bounce=0.05, days=90):
    """Synthetic data with a deliberate dip-and-bounce at the last 3 bars."""
    np.random.seed(42)
    dates = pd.bdate_range("2025-01-01", periods=days)
    market_hours = [9, 10, 11, 12, 13, 14, 15]
    timestamps = [
        pd.Timestamp(f"{d.date()} {h:02d}:30:00")
        for d in dates for h in market_hours
    ]
    prices = 100.0 + np.random.normal(0, 0.3, len(timestamps))
    highs  = prices + np.abs(np.random.normal(0, 0.5, len(timestamps)))
    lows   = prices - np.abs(np.random.normal(0, 0.5, len(timestamps)))

    # Last 3 bars: signal -> dip -> bounce
    prices[-3] = signal_close
    lows[-3]   = signal_close
    prices[-2] = dip_low
    lows[-2]   = dip_low
    highs[-2]  = dip_low * 1.01
    bounce_trigger = dip_low * (1 + bounce)
    prices[-1] = bounce_trigger
    highs[-1]  = bounce_trigger * 1.02
    lows[-1]   = dip_low * 1.005

    df = pd.DataFrame({'Close': prices, 'High': highs, 'Low': lows}, index=timestamps)
    df.index.name = 'Datetime'
    df.to_csv(Path('./cache') / f"{ticker}_1h.csv")


def test_trailingbuy_buy_signal_price_below_lower_band():
    make_synthetic_csv(TICKER_BUY, last_close=85.0)
    try:
        sig = compute_buy_signal(node_buy())
        assert sig is not None
        assert sig['signal'] == 'BUY'
    finally:
        cleanup_csv(TICKER_BUY)


def test_trailingbuy_hold_signal_price_above_lower_band():
    make_synthetic_csv(TICKER_BUY, last_close=101.0)
    try:
        sig = compute_buy_signal(node_buy())
        assert sig['signal'] == 'HOLD'
    finally:
        cleanup_csv(TICKER_BUY)


def test_trailingboth_buy_signal_price_below_lower_band():
    make_synthetic_csv(TICKER_BOTH, last_close=85.0)
    try:
        sig = compute_buy_signal(node_both())
        assert sig['signal'] == 'BUY'
    finally:
        cleanup_csv(TICKER_BOTH)


def test_trailingbuy_exit_tp_hit():
    reason, _, _ = check_sell_condition(pos_buy(entry_price=100.0, tp=10), 112.0, datetime.now())
    assert reason == 'TP'


def test_trailingbuy_exit_sl_hit():
    reason, _, _ = check_sell_condition(pos_buy(entry_price=100.0, sl=8), 91.0, datetime.now())
    assert reason == 'SL'


def test_trailingbuy_exit_time():
    make_synthetic_csv(TICKER_BUY, last_close=101.0)
    try:
        reason, _, _ = check_sell_condition(pos_buy(entry_price=100.0, hours_ago=60, hold=56), 101.0, datetime.now())
        assert reason == 'TIME'
    finally:
        cleanup_csv(TICKER_BUY)


@pytest.fixture
def dip_bounce_df():
    ticker = '_v19_test'
    make_dip_bounce_csv(ticker)
    df = pd.read_csv(f'cache/{ticker}_1h.csv', index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    yield df
    cleanup_csv(ticker)


def test_v19_kernel_fires_trades_on_dip_bounce_data(dip_bounce_df):
    df = dip_bounce_df
    strat = TrailingBuyZScoreBreakout(window=10, z_score_threshold=2.0)
    df_daily = df.resample('D').last().dropna(subset=['Close'])
    df_ind = strat.generate_daily_indicators(df_daily)
    t19 = run_backtest_v19(df, df_ind, '_v19_test',
                            take_profit=0.10, stop_loss=0.15, max_hours_to_hold=56,
                            z_score_threshold=2.0, trail_buy_pct=0.03)
    assert len(t19) > 0


def test_v110_kernel_fires_trades_on_dip_bounce_data(dip_bounce_df):
    df = dip_bounce_df
    strat = TrailingBuyZScoreBreakout(window=10, z_score_threshold=2.0)
    df_daily = df.resample('D').last().dropna(subset=['Close'])
    df_ind = strat.generate_daily_indicators(df_daily)
    t110 = run_backtest_v110(df, df_ind, '_v19_test',
                              take_profit=0.10, stop_loss=0.15, max_hours_to_hold=56,
                              z_score_threshold=2.0, trail_buy_pct=0.03, trail_pct=0.03)
    assert len(t110) > 0


@pytest.mark.parametrize("ticker", ['AGQ', 'SOXL'])
def test_v19_real_data_has_closed_trades(ticker):
    cache = Path(f'cache/{ticker}_1h.csv')
    if not cache.exists():
        pytest.skip(f"no cached data for {ticker}")
    df_r = pd.read_csv(cache, index_col=0, parse_dates=True)
    df_r.index = pd.to_datetime(df_r.index).tz_localize(None)
    close_col = 'Adj Close' if 'Adj Close' in df_r.columns else 'Close'
    df_rd = df_r.resample('D').last().dropna(subset=[close_col])
    s = TrailingBuyZScoreBreakout(window=10, z_score_threshold=2.0)
    ind = s.generate_daily_indicators(df_rd)
    t = run_backtest_v19(df_r, ind, ticker, take_profit=0.10, stop_loss=0.08,
                          max_hours_to_hold=112, z_score_threshold=2.0, trail_buy_pct=0.03)
    closed = [x for x in t if x['Result'] in ('WIN', 'LOSS', 'TWIN', 'TLOSS')]
    assert len(closed) > 0
