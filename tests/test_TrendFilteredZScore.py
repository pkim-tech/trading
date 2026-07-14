"""
TrendFilteredZScore adds a 50-day trend filter: price must also be above
the 50-day SMA to trigger BUY. Tests require 90 days of history so the
trend filter has enough data to produce a non-NaN value.
"""
import sys
import numpy as np
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from active_signals import compute_buy_signal
from tests.conftest import cleanup_csv, fake_node

TICKER    = 'TEST_TFZS'
STRATEGY  = 'TrendFilteredZScore'
CACHE_DIR = Path("./cache/research")


def node(**kw): return fake_node(TICKER, STRATEGY, **kw)


def make_trend_csv(last_close, trend_direction='up'):
    """
    Constructs price history that puts the 50-day trend filter on a known side
    of the current price so we can test the trend filter condition in isolation.

    'up':   70 days low (~60), 20 days high (~120) -> 50-day SMA ~84
            last_close=90 sits above trend (84) and below lower band (~119) -> BUY

    'down': 70 days high (~120), 20 days low (~60) -> 50-day SMA ~96
            last_close=70 below lower band but also below trend (96) -> HOLD
    """
    np.random.seed(0)
    dates = pd.bdate_range("2025-01-01", periods=90)
    market_hours = [9, 10, 11, 12, 13, 14, 15]
    timestamps = [
        pd.Timestamp(f"{d.date()} {h:02d}:30:00")
        for d in dates for h in market_hours
    ]
    n = len(timestamps)
    split2 = n * 70 // 90

    if trend_direction == 'up':
        base = np.concatenate([np.full(split2, 60.0), np.full(n - split2, 120.0)])
    else:
        base = np.concatenate([np.full(split2, 120.0), np.full(n - split2, 60.0)])

    prices = base + np.random.normal(0, 0.3, n)
    prices[-1] = last_close

    df = pd.DataFrame({'Close': prices}, index=timestamps)
    df.index.name = 'Datetime'
    df.to_csv(CACHE_DIR / f"{TICKER}_1h.csv")


def test_buy_below_lower_band_above_trend_filter():
    make_trend_csv(last_close=90.0, trend_direction='up')
    try:
        sig = compute_buy_signal(node())
        assert sig is not None
        assert sig['signal'] == 'BUY'
    finally:
        cleanup_csv(TICKER)


def test_hold_below_lower_band_but_below_trend_filter():
    make_trend_csv(last_close=70.0, trend_direction='down')
    try:
        sig = compute_buy_signal(node())
        assert sig['signal'] == 'HOLD'
    finally:
        cleanup_csv(TICKER)


def test_hold_price_above_lower_band():
    make_trend_csv(last_close=121.0, trend_direction='up')
    try:
        sig = compute_buy_signal(node())
        assert sig['signal'] == 'HOLD'
    finally:
        cleanup_csv(TICKER)
