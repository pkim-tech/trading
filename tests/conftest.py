"""
Shared test utilities for strategy signal tests.
"""

import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

CACHE_DIR = Path("./cache")


def _synthetic_timestamps(days=90):
    """Same hourly-bar timestamp grid make_synthetic_csv() writes to disk --
    shared so fake_position() can place signal_time exactly N bars back from
    the last bar (bars-ago, not wall-clock-hours-ago: the fixture data lives
    on a fixed 2025 date range, and _bars_held() counts rows, not elapsed
    calendar time, so wall-clock deltas don't land in the right place)."""
    dates = pd.bdate_range("2025-01-01", periods=days)
    market_hours = [9, 10, 11, 12, 13, 14, 15]
    return [
        pd.Timestamp(f"{d.date()} {h:02d}:30:00")
        for d in dates for h in market_hours
    ]


def make_synthetic_csv(ticker, last_close, days=90):
    """
    Writes a synthetic hourly CSV to cache/{ticker}_1h.csv.
    Prices are ~100 with low variance; last bar is set to last_close.
    days=90 ensures enough history for window=20 + 50-day trend filter.
    Call cleanup_csv() after the test.
    """
    np.random.seed(0)
    timestamps = _synthetic_timestamps(days)
    prices = 100.0 + np.random.normal(0, 0.3, len(timestamps))
    prices[-1] = last_close

    df = pd.DataFrame({'Close': prices}, index=timestamps)
    df.index.name = 'Datetime'
    df.to_csv(CACHE_DIR / f"{ticker}_1h.csv")


def cleanup_csv(ticker):
    path = CACHE_DIR / f"{ticker}_1h.csv"
    path.unlink(missing_ok=True)


def fake_node(ticker, strategy, window=20, tp=10, sl=5, hold=56):
    return {
        'ticker':         ticker,
        'strategy':       strategy,
        'version':        'test',
        'window':         window,
        'take_profit':    tp,
        'stop_loss':      sl,
        'max_hold_hours': hold,
    }


def fake_position(ticker, strategy, entry_price, hours_ago=10, tp=10, sl=5, hold=56, window=20, days=90):
    """hours_ago is really bars-ago -- see _synthetic_timestamps()."""
    timestamps = _synthetic_timestamps(days)
    entry_time = timestamps[-1 - hours_ago] if hours_ago < len(timestamps) else timestamps[0]
    return {
        'id':             999,
        'ticker':         ticker,
        'strategy':       strategy,
        'version':        'test',
        'window':         window,
        'take_profit':    tp,
        'stop_loss':      sl,
        'max_hold_hours': hold,
        'signal_price':   entry_price,
        'signal_time':    entry_time.strftime('%Y-%m-%d %H:%M:%S'),
        'entry_price':    entry_price,
        'entry_time':     entry_time.strftime('%Y-%m-%d %H:%M:%S'),
    }
