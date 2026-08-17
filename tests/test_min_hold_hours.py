"""min_hold_hours compliance-hold floor (2026-08-17) -- pins the two real bugs
found and fixed by paired Opus review before any sweep touched the feature:
(1) HIGH -- a blocked SL fell through the elif chain into TP-arming,
permanently disabling the stop instead of delaying the exit; (2) MEDIUM --
peak/trail_stop tracking silently froze on a blocked gap-exit bar. Calls
_simulate_trail_both directly with synthetic arrays (no CSV/file I/O) --
mirrors the ad hoc reproduction scripts used to find/verify both bugs during
the session, promoted to a real regression test per both paired reviews'
finding that this diff otherwise has zero test coverage."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtester import _simulate_trail_both, _RESULT_NAMES


def _run(opens, prices, highs, lows, hours, sma, std, min_hold_hours,
         take_profit=0.02, stop_loss=0.05, max_hours_to_hold=50,
         trail_buy_pct=0.0, trail_pct=0.03, target_h0=9, target_h1=9, z_thresh=1.0):
    daily_idx = np.zeros(len(prices), dtype=np.int64)
    trend_arr = np.zeros(1)
    (ei, xi, ep, xp, held, res, ret, *_rest) = _simulate_trail_both(
        np.array(prices), np.array(highs), np.array(lows),
        np.array(hours, dtype=np.int64), daily_idx,
        np.array([sma]), np.array([std]), trend_arr, False,
        take_profit, stop_loss, max_hours_to_hold, trail_buy_pct, trail_pct,
        target_h0, target_h1, z_thresh,
        np.array(opens), True, False, min_hold_hours,
    )
    return [
        {"hours_held": int(h), "Result": _RESULT_NAMES[r], "Return": float(rr)}
        for h, r, rr in zip(held, res, ret)
    ]


def test_min_hold_zero_reproduces_default_behavior():
    """min_hold_hours=0 must be a pure no-op -- the entire feature's backward-
    compat guarantee."""
    opens  = [100.0, 100.0, 105.0, 105.0, 105.0, 105.0, 105.0]
    prices = [100.0, 100.0, 105.0, 105.0, 105.0, 105.0, 105.0]
    highs  = [100.0, 100.0, 110.0, 105.0, 105.0, 105.0, 105.0]
    lows   = [100.0, 100.0,  90.0, 105.0, 105.0, 105.0, 105.0]
    hours  = [9, 10, 11, 12, 13, 9, 10]

    baseline = _run(opens, prices, highs, lows, hours, sma=110.0, std=1.0, min_hold_hours=0)
    again    = _run(opens, prices, highs, lows, hours, sma=110.0, std=1.0, min_hold_hours=0)
    assert baseline == again  # deterministic
    assert len(baseline) >= 1
    # bar 2's low=90 breaches a 5% SL from entry@100 (stop_price=95) -- with no
    # floor this must fire immediately, held=1, tagged LOSS.
    assert baseline[0]["hours_held"] == 1
    assert baseline[0]["Result"] == "LOSS"


def test_blocked_sl_does_not_fall_through_to_tp_arm():
    """Regression for the HIGH bug: a blocked SL used to fall through the elif
    chain into TP-arming, permanently disabling the stop for the rest of the
    trade instead of just delaying the exit. Same bars as test_min_hold_zero's
    SL-breach case, plus a floor of 3 bars -- bar 2's SL breach (op=100,
    low=90, close=105 which also clears TP) must be BLOCKED, not converted
    into an arm."""
    opens  = [100.0, 100.0, 100.0, 105.0] + [97.0] * 10
    prices = [100.0, 100.0, 105.0, 97.0] + [97.0] * 10
    highs  = [100.0, 100.0, 110.0, 97.0] + [97.0] * 10
    lows   = [100.0, 100.0,  90.0, 97.0] + [97.0] * 10
    hours  = [9, 10, 11, 12] + [13, 9, 10, 11, 12, 13, 9, 10, 11, 12]

    trades = _run(opens, prices, highs, lows, hours, sma=110.0, std=1.0,
                   min_hold_hours=3, stop_loss=0.05, take_profit=0.02)

    # With the bug: bar 2's blocked SL fell through to cp>=tp_price, arming
    # trailing at peak=105, then a later bar's op<=trail_stop_gap fired a
    # LOSS/WIN exit around held=3-4. Fixed behavior: SL stays correctly
    # blocked, nothing else re-triggers (flat 97 price never re-breaches SL
    # or re-clears TP), so the trade just stays open to the end of data.
    assert len(trades) == 1
    assert trades[0]["Result"] == "OPEN"
    assert trades[0]["hours_held"] == len(prices) - 2  # bars since entry (bar 1)


def test_peak_keeps_updating_on_blocked_gap_exit_bar():
    """Regression for the MEDIUM bug: peak/trail_stop tracking used to freeze
    whenever a gap-exit bar's own exit was blocked by the floor (the peak
    update lived in the `else` of the gap check, only reached when NOT
    exiting via gap -- so a blocked-but-still-gapping bar never updated
    peak). Fixed: peak/trail_stop now update unconditionally every bar."""
    opens  = [100.0, 100.0, 103.0, 95.0,  105.0, 90.0, 90.0]
    prices = [100.0, 100.0, 103.0, 100.0, 105.0, 90.0, 90.0]
    highs  = [100.0, 100.0, 103.0, 110.0, 105.0, 90.0, 90.0]
    lows   = [100.0, 100.0, 103.0, 90.0,  105.0, 88.0, 88.0]
    hours  = [9, 10, 11, 12, 13, 9, 10]

    # take_profit=2%: bar2 close=103 arms trailing (entry@100 at bar1, peak=103).
    # bar3: op=95 gaps through trail_stop_gap=103*0.97=99.91 -- if peak correctly
    # absorbs bar3's own high=110 before that gap check (peak->110), the
    # eventual exit (once min_hold permits) reads a materially different
    # trail_stop than if peak had stayed frozen at 103.
    trades = _run(opens, prices, highs, lows, hours, sma=110.0, std=1.0,
                   min_hold_hours=5, stop_loss=0.50, take_profit=0.02, trail_pct=0.03)

    assert len(trades) == 1
    # Fixed behavior: peak reaches 110 by bar3 (unconditionally updated even
    # though that bar's gap-exit was blocked), so trail_stop_gap stays
    # 110*0.97=106.7 for the rest of the trade -- op=90 keeps satisfying the
    # gap condition every subsequent bar, and the first bar where
    # held>=min_hold_hours(5) is the real exit, at op=90.
    assert trades[0]["hours_held"] == 5
    assert trades[0]["Result"] == "LOSS"
    assert abs(trades[0]["Return"] - (90.0 - 100.0) / 100.0) < 1e-9
