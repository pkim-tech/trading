"""Regression test for a real bug found by the independent Opus review,
2026-08-05, while reviewing paper_trading.reconcile_daily_track_nodes' exit
counterfactual: scripts/export_trades.py::simulate_trail_exit_chaos hardcoded
arm_i=None in every trade dict it emitted, regardless of whether the trade
actually armed (crossed take_profit into the trailing-stop phase) -- even
though the strategy it mirrors (TrailingExitZScoreBreakout, strategies.py)
genuinely has an arm-then-trail state machine, the same as TrailingBoth's.
Consumers that used arm_i to distinguish "genuine SL/unarmed-TIME exit" from
"trailing-stop exit" (reconcile's wick counterfactual) always took the wrong
branch for this strategy family. Fixed by tracking arm_bar the same way the
other simulate_trail_* mirrors in this file already do."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtester import WIN, LOSS, TWIN, TLOSS
from scripts.export_trades import simulate_trail_exit_chaos


def _p(prices, opens=None, highs=None, lows=None):
    n = len(prices)
    prices = np.array(prices, dtype=np.float64)
    opens = np.array(opens if opens is not None else prices, dtype=np.float64)
    highs = np.array(highs if highs is not None else prices, dtype=np.float64)
    lows = np.array(lows if lows is not None else prices, dtype=np.float64)
    # Every bar hour=9 (a signal-eligible hour), one bar per "day" for simplicity.
    return dict(
        prices=prices, opens=opens, highs=highs, lows=lows,
        hours=np.full(n, 9, dtype=np.int64),
        daily_idx=np.zeros(n, dtype=np.int64),  # every bar maps to the one constant sma/std row
        sma_arr=np.full(1, 100.0), std_arr=np.full(1, 1.0),
        trend_arr=np.zeros(1), has_trend=False,
        timestamps=list(range(n)),
    )


def _run(prices, **kw):
    rng = np.random.default_rng(0)
    p = _p(prices, **kw)
    return simulate_trail_exit_chaos(p, take_profit=0.05, stop_loss=0.02, max_hours_to_hold=50,
                                      trail_pct=0.03, target_h0=9, target_h1=9, z_thresh=1.0,
                                      rng=rng, entry_miss_mode='drop', entry_miss_rate=0.0,
                                      exit_miss_mode='drop', exit_miss_rate=0.0)


def test_genuine_trailing_stop_exit_reports_arm_i():
    # Bar 0: signal (Close=90 <= lower_band=98) -> entry at 90. Bar 1: rises to 96 (>=
    # tp_price=90*1.05=94.5) -> arms, peak=96. Bar 2: rises to 100 -> peak updates to 100.
    # Bar 3: drops to 95 -> trail_stop=100*0.97=97 -- Low(95)<=97 -> trailing-stop exit.
    prices = [90.0, 96.0, 100.0, 95.0]
    trades = _run(prices)
    assert len(trades) == 1
    t = trades[0]
    assert t['result'] in (WIN, LOSS)
    assert t['arm_i'] is not None
    assert t['arm_i'] == 1  # armed at bar 1, when cp first crossed tp_price


def test_genuine_sl_exit_reports_no_arm():
    # Bar 0: entry at 90 (stop_price=90*0.98=88.2). Bar 1: drops straight to 85 -- SL hit,
    # never armed.
    prices = [90.0, 85.0]
    trades = _run(prices)
    assert len(trades) == 1
    t = trades[0]
    assert t['result'] == LOSS
    assert t['arm_i'] is None


def test_unarmed_time_forced_exit_reports_no_arm():
    # Entry at 90, price hovers flat (never arms, never breaches SL) until
    # max_hours_to_hold=50 forces a TIME exit. (Price stays low enough that a
    # second entry re-fires immediately after -- only the first trade matters here.)
    prices = [90.0] + [91.0] * 51
    trades = _run(prices)
    t = trades[0]
    assert t['result'] in (TWIN, TLOSS)
    assert t['arm_i'] is None


def test_still_open_armed_trade_reports_arm_i():
    # Entry, arms, never exits before the data ends -- the still-open (OPEN) trade
    # at end-of-loop must also report arm_i correctly, not just closed trades.
    from backtester import OPEN
    prices = [90.0, 96.0, 97.0]
    trades = _run(prices)
    assert len(trades) == 1
    t = trades[0]
    assert t['result'] == OPEN
    assert t['arm_i'] == 1
