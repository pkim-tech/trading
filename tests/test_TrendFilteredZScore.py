#!/usr/bin/env python3
"""
TrendFilteredZScore adds a 50-day trend filter: price must also be above
the 50-day SMA to trigger BUY. Tests require 90 days of history so the
trend filter has enough data to produce a non-NaN value.
"""
import sys
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

from active_signals import compute_buy_signal
from tests.conftest import make_synthetic_csv, cleanup_csv, fake_node, run_tests

TICKER   = 'TEST_TFZS'
STRATEGY = 'TrendFilteredZScore'
CACHE_DIR = Path("./cache")

def node(**kw): return fake_node(TICKER, STRATEGY, **kw)

results = []


def make_trend_csv(last_close, trend_direction='up'):
    """
    Constructs price history that puts the 50-day trend filter on a known side
    of the current price so we can test the trend filter condition in isolation.

    'up':   first 40 days ~80, next 30 days ~110 → 50-day SMA ends ~97
            last bar set to last_close (~103) which is above trend (~97)
            and below the 20-day SMA (~110) lower band → triggers BUY

    'down': first 40 days ~120, next 30 days ~90 → 50-day SMA ends ~103
            last bar set to last_close (~70) which is below trend (~103)
            → trend filter blocks entry → HOLD
    """
    np.random.seed(0)
    dates = pd.bdate_range("2025-01-01", periods=90)
    market_hours = [9, 10, 11, 12, 13, 14, 15]
    timestamps = [
        pd.Timestamp(f"{d.date()} {h:02d}:30:00")
        for d in dates for h in market_hours
    ]
    n = len(timestamps)
    split = n * 40 // 90

    if trend_direction == 'up':
        # 70 days low (~60), 20 days high (~120)
        # → 50-day SMA ≈ 84 (dragged down by the low period)
        # → 20-day SMA ≈ 120, lower band ≈ 119
        # → last_close=90 sits above trend (84) and below lower band (119) → BUY
        split2 = n * 70 // 90
        base = np.concatenate([np.full(split2, 60.0), np.full(n - split2, 120.0)])
    else:
        # 70 days high (~120), 20 days low (~60)
        # → 50-day SMA ≈ 96 (still high from the long run)
        # → 20-day SMA ≈ 60, lower band ≈ 59
        # → last_close=50 below lower band but also below trend (96) → HOLD
        split2 = n * 70 // 90
        base = np.concatenate([np.full(split2, 120.0), np.full(n - split2, 60.0)])

    prices = base + np.random.normal(0, 0.3, n)
    prices[-1] = last_close

    df = pd.DataFrame({'Close': prices}, index=timestamps)
    df.index.name = 'Datetime'
    df.to_csv(CACHE_DIR / f"{TICKER}_1h.csv")


# BUY: price below lower band (~119) AND above 50-day trend (~84)
make_trend_csv(last_close=90.0, trend_direction='up')
sig = compute_buy_signal(node())
results += run_tests("BUY — below lower band, above trend filter (uptrend)", [
    ("returns result",  sig is not None,                True),
    ("signal == BUY",   sig['signal'] if sig else None, 'BUY'),
])

# HOLD: price below lower band BUT also below 50-day trend (~103) — downtrend blocks entry
make_trend_csv(last_close=70.0, trend_direction='down')
sig = compute_buy_signal(node())
results += run_tests("HOLD — below lower band but below trend filter (downtrend blocks)", [
    ("signal == HOLD", sig['signal'] if sig else None, 'HOLD'),
])

# HOLD: price above lower band (~119) — no entry regardless of trend
make_trend_csv(last_close=121.0, trend_direction='up')
sig = compute_buy_signal(node())
results += run_tests("HOLD — price above lower band", [
    ("signal == HOLD", sig['signal'] if sig else None, 'HOLD'),
])

cleanup_csv(TICKER)

passed = sum(results)
total  = len(results)
print(f"\n{'='*40}")
print(f"  TrendFilteredZScore: {passed}/{total} passed {'✓' if passed == total else '✗'}")
print(f"{'='*40}\n")
sys.exit(0 if passed == total else 1)
