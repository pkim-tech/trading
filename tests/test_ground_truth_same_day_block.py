"""Pinned regression test for the TrailingBoth same_day_block port to the GT kernel
(_simulate_trail_ground_truth / run_backtest_ground_truth, backtester.py), ported from
the legacy kernel's same_day_block (backtester._simulate_trail_both, used today by
scripts/checklist_v65.py's check 9). See docs/backlog_cache.md's "port same_day_block
sensitivity (checklist check 9) to the GT kernel, for TrailingBoth first" item.

Manually verified real trade this pins (DFEN, real `state='live'` config, full cached
history): with same_day_block=False, an SL exit at 2025-03-12 09:56 is followed by a
same-day re-entry at 2025-03-12 11:53 (also exits SL, on 2025-04-03). With
same_day_block=True, that 11:53 re-entry is blocked (last_exit_day == this signal's
daily_idx).

Real-data behavior-difference finding (not just a toy case): across DFEN's full history,
same_day_block=True changes the trade sequence from 54 to 43 trades and *improves* net
compounded return over the same 708-day span (239.79% -> 278.10%, ~87.9% -> ~98.5% approx
CAGR) -- directly answering "does same_day_block ever produce a useful signal
historically" with real numbers for at least this one real scope. Not a claim this holds
for every ticker.

test_same_day_block_never_reenters_same_day is the real proof of the mechanism: a direct
invariant check that with same_day_block=True, no trade's entry date ever equals any
prior trade's exit date. An earlier version of this file (2026-08-29) only pinned the
2025-03-12 example above, which happened to pass even against a real bug found by the
2026-08-30 paired review: `blocked_sdb` was computed once per bar before that bar's own
exits, so a same-bar close_check re-entry right after a same-day exit slipped through
uncaught (the GT kernel, unlike the legacy kernel, allows same-bar re-entry via
same_bar_reentry). 12 of 46 same_day_block=True trades on this same DFEN scope violated
the invariant before the fix (recomputing the block flag at the close_check site using
the exit-updated last_exit_day). The pinned example above happened not to exercise that
path, which is exactly why an invariant check across every trade -- not one hand-picked
example -- is required to actually prove the mechanism.

Requires cache/research/DFEN_1h.csv and cache/research/minute_data/DFEN_1m.csv (real
cached data, not fixtures) -- skips if either is missing.
"""
import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import strategies  # noqa: E402
from backtester import run_backtest_ground_truth  # noqa: E402

HOURLY_DIR = os.path.join(ROOT, "cache", "research")
MINUTE_DIR = os.path.join(ROOT, "cache", "research", "minute_data")
TICKER = "DFEN"

# Real state='live' DFEN watch_list config as of 2026-08-29 (watchlist_id=65) -- pinned
# here rather than re-queried at collect time, same rationale as
# test_ground_truth_kernel_parity.py's LIVE_NODE_IDS (a live DB read at collection time
# would make CI/offline runs flaky).
DFEN_CFG = dict(
    window=20, z_score_threshold=1.0, trail_buy_pct=3.0, arm_sell_pct=30.0,
    trail_sell_pct=1.0, fixed_sl=3.0, max_hold_hours=133, entry_timing="open_check",
)


def _has_cache():
    return (os.path.exists(os.path.join(HOURLY_DIR, f"{TICKER}_1h.csv"))
            and os.path.exists(os.path.join(MINUTE_DIR, f"{TICKER}_1m.csv")))


def _load_trades(same_day_block):
    from sim_minute_groundtruth_independent import load_minutes

    df_hourly = pd.read_csv(os.path.join(HOURLY_DIR, f"{TICKER}_1h.csv"), index_col=0, parse_dates=True)
    df_hourly.index = pd.to_datetime(df_hourly.index).tz_localize(None)
    df_hourly = df_hourly.sort_index()
    close_col = "Adj Close" if "Adj Close" in df_hourly.columns else "Close"
    df_daily = df_hourly.resample("D").last().dropna(subset=[close_col])
    strat = strategies.TrailingBothZScoreBreakout(
        window=DFEN_CFG["window"], z_score_threshold=DFEN_CFG["z_score_threshold"])
    df_daily_ind = strat.generate_daily_indicators(df_daily)
    minute_df = load_minutes(TICKER)

    return run_backtest_ground_truth(
        df_hourly, df_daily_ind, TICKER, minute_df,
        fixed_sl=DFEN_CFG["fixed_sl"], arm_pct=DFEN_CFG["arm_sell_pct"],
        trail_buy_pct=DFEN_CFG["trail_buy_pct"], trail_sell_pct=DFEN_CFG["trail_sell_pct"],
        max_hours_to_hold=DFEN_CFG["max_hold_hours"], z_score_threshold=DFEN_CFG["z_score_threshold"],
        is_both=True, open_check_entry_timing=(DFEN_CFG["entry_timing"] == "open_check"),
        same_bar_reentry=True, same_day_block=same_day_block, need_times=True,
    )


@pytest.mark.skipif(not _has_cache(), reason="real DFEN cache not present")
def test_same_day_block_never_reenters_same_day():
    """The real proof: with same_day_block=True, no trade's entry date may equal any
    PRIOR trade's exit date, across the entire real trade sequence -- not just one
    hand-picked example. This is the check that actually would have caught the
    2026-08-30 stale-blocked_sdb bug (12/46 violations before the fix)."""
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    trades_on = _load_trades(same_day_block=True)
    df_on = pd.DataFrame(trades_on).sort_values("Entry Time").reset_index(drop=True)

    prev_exit_date = None
    violations = []
    for _, row in df_on.iterrows():
        if prev_exit_date is not None and row["Entry Time"].date() == prev_exit_date:
            violations.append((row["Entry Time"], prev_exit_date))
        prev_exit_date = row["Exit Time"].date()

    assert violations == [], (
        f"same_day_block=True must never allow a same-day re-entry after a prior "
        f"exit, found {len(violations)} violation(s): {violations}"
    )


@pytest.mark.skipif(not _has_cache(), reason="real DFEN cache not present")
def test_same_day_block_blocks_the_real_2025_03_12_reentry():
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    trades_off = _load_trades(same_day_block=False)
    trades_on = _load_trades(same_day_block=True)

    df_off = pd.DataFrame(trades_off)

    reentry = df_off[(df_off["Entry Time"] == pd.Timestamp("2025-03-12 11:53:00"))]
    assert len(reentry) == 1, "expected the known same-day re-entry to be present with same_day_block=False"
    prior_exit = df_off[(df_off["Exit Time"] == pd.Timestamp("2025-03-12 09:56:00"))]
    assert len(prior_exit) == 1, "expected the known same-day SL exit to be present"

    on_entries = set(pd.DataFrame(trades_on)["Entry Time"])
    assert pd.Timestamp("2025-03-12 11:53:00") not in on_entries, (
        "same_day_block=True should block the same-day re-entry that follows the "
        "09:56 SL exit on the same trading day"
    )


@pytest.mark.skipif(not _has_cache(), reason="real DFEN cache not present")
def test_same_day_block_changes_trade_count_on_real_scope():
    trades_off = _load_trades(same_day_block=False)
    trades_on = _load_trades(same_day_block=True)

    # Pinned real-data finding, 2026-08-30 (post stale-blocked_sdb fix): same_day_block
    # =True blocks a real, non-trivial number of DFEN trades -- not a no-op on this scope.
    assert len(trades_off) == 54
    assert len(trades_on) == 43
