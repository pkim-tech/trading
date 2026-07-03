#!/usr/bin/env python3
import sys
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtester import run_backtest_v19, run_backtest_v110
from active_signals import compute_buy_signal, check_sell_condition
from tests.conftest import make_synthetic_csv, cleanup_csv, fake_node, fake_position, run_tests

TICKER_BUY  = 'TEST_TRBUY'
TICKER_BOTH = 'TEST_TRBOTH'
STRAT_BUY   = 'TrailingBuyZScoreBreakout'
STRAT_BOTH  = 'TrailingBothZScoreBreakout'

def node_buy(**kw):  return fake_node(TICKER_BUY,  STRAT_BUY,  **kw)
def node_both(**kw): return fake_node(TICKER_BOTH, STRAT_BOTH, **kw)
def pos_buy(**kw):   return fake_position(TICKER_BUY,  STRAT_BUY,  **kw)
def pos_both(**kw):  return fake_position(TICKER_BOTH, STRAT_BOTH, **kw)


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

    # Last 3 bars: signal → dip → bounce
    prices[-3] = signal_close          # bar at target hour: signal fires
    lows[-3]   = signal_close
    prices[-2] = dip_low               # price dips further
    lows[-2]   = dip_low
    highs[-2]  = dip_low * 1.01
    bounce_trigger = dip_low * (1 + bounce)
    prices[-1] = bounce_trigger        # price bounces to trigger entry
    highs[-1]  = bounce_trigger * 1.02
    lows[-1]   = dip_low * 1.005

    df = pd.DataFrame({'Close': prices, 'High': highs, 'Low': lows}, index=timestamps)
    df.index.name = 'Datetime'
    df.to_csv(Path('./cache') / f"{ticker}_1h.csv")


results = []

# ── Signal check (same z-score trigger as ZSB) ─────────────────────────────
make_synthetic_csv(TICKER_BUY, last_close=85.0)
sig = compute_buy_signal(node_buy())
results += run_tests("TrailingBuy BUY signal — price below lower band", [
    ("returns result",  sig is not None,                True),
    ("signal == BUY",   sig['signal'] if sig else None, 'BUY'),
])

make_synthetic_csv(TICKER_BUY, last_close=101.0)
sig = compute_buy_signal(node_buy())
results += run_tests("TrailingBuy HOLD signal — price above lower band", [
    ("signal == HOLD", sig['signal'] if sig else None, 'HOLD'),
])
cleanup_csv(TICKER_BUY)

make_synthetic_csv(TICKER_BOTH, last_close=85.0)
sig = compute_buy_signal(node_both())
results += run_tests("TrailingBoth BUY signal — price below lower band", [
    ("signal == BUY", sig['signal'] if sig else None, 'BUY'),
])
cleanup_csv(TICKER_BOTH)

# ── Exit checks for v1.9 (same as ZSB: fixed TP/SL/TIME) ──────────────────
results += run_tests("TrailingBuy exit — TP hit", [
    ("reason == TP", check_sell_condition(pos_buy(entry_price=100.0, tp=10), 112.0, datetime.now())[0], 'TP'),
])
results += run_tests("TrailingBuy exit — SL hit", [
    ("reason == SL", check_sell_condition(pos_buy(entry_price=100.0, sl=8), 91.0, datetime.now())[0], 'SL'),
])
results += run_tests("TrailingBuy exit — TIME", [
    ("reason == TIME", check_sell_condition(pos_buy(entry_price=100.0, hours_ago=60, hold=56), 101.0, datetime.now())[0], 'TIME'),
])

# ── Kernel: v1.9 produces trades and trailing entry fires ──────────────────
make_dip_bounce_csv('_v19_test')
df = pd.read_csv('cache/_v19_test_1h.csv', index_col=0, parse_dates=True)
df.index = pd.to_datetime(df.index).tz_localize(None)

from strategies import TrailingBuyZScoreBreakout, TrailingBothZScoreBreakout
strat = TrailingBuyZScoreBreakout(window=10, z_score_threshold=2.0)
df_daily = df.resample('D').last().dropna(subset=['Close'])
df_ind = strat.generate_daily_indicators(df_daily)
t19 = run_backtest_v19(df, df_ind, '_v19_test',
                       take_profit=0.10, stop_loss=0.15, max_hours_to_hold=56,
                       z_score_threshold=2.0, trail_buy_pct=0.03)
results += run_tests("v1.9 kernel — fires trades on dip+bounce data", [
    ("has trades", len(t19) > 0, True),
])

# ── Kernel: v1.10 produces trades ─────────────────────────────────────────
t110 = run_backtest_v110(df, df_ind, '_v19_test',
                         take_profit=0.10, stop_loss=0.15, max_hours_to_hold=56,
                         z_score_threshold=2.0, trail_buy_pct=0.03, trail_pct=0.03)
results += run_tests("v1.10 kernel — fires trades on dip+bounce data", [
    ("has trades", len(t110) > 0, True),
])

cleanup_csv('_v19_test')

# ── Real data sanity: AGQ and SOXL ────────────────────────────────────────
for ticker in ['AGQ', 'SOXL']:
    cache = Path(f'cache/{ticker}_1h.csv')
    if cache.exists():
        df_r = pd.read_csv(cache, index_col=0, parse_dates=True)
        df_r.index = pd.to_datetime(df_r.index).tz_localize(None)
        close_col = 'Adj Close' if 'Adj Close' in df_r.columns else 'Close'
        df_rd = df_r.resample('D').last().dropna(subset=[close_col])
        s = TrailingBuyZScoreBreakout(window=10, z_score_threshold=2.0)
        ind = s.generate_daily_indicators(df_rd)
        t = run_backtest_v19(df_r, ind, ticker, take_profit=0.10, stop_loss=0.08,
                             max_hours_to_hold=112, z_score_threshold=2.0, trail_buy_pct=0.03)
        closed = [x for x in t if x['Result'] in ('WIN', 'LOSS', 'TWIN', 'TLOSS')]
        results += run_tests(f"v1.9 real data — {ticker}", [
            ("has closed trades", len(closed) > 0, True),
        ])

passed = sum(results)
total  = len(results)
print(f"\n{'='*40}")
print(f"  TrailingBuy/Both: {passed}/{total} passed {'✓' if passed == total else '✗'}")
print(f"{'='*40}\n")
sys.exit(0 if passed == total else 1)
