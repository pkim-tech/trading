"""Mandatory parity gate for the v6 ground-truth kernel (backtester.run_backtest_ground_truth /
_simulate_trail_ground_truth), per docs/plans/ground_truth_kernel_rebuild.md Step 2a: the numba
port must reproduce scripts/sim_minute_groundtruth_independent.py's per-trade output
byte-identically before any of its output is trusted for anything else. Same pattern as
tests/test_certain_resolution_fix.py's kernel-parity checks.

Originally scoped to SOXL only (per user direction, 2026-08-21); extended to all 12 real
`state='live'` nodes (both TrailingBothZScoreBreakout and TrailingExitZScoreBreakout) after
the paired-review independent-cold pass found a real entry-timestamp bug that only showed up
on non-SOXL tickers (GT_MJ_BAR_OPEN_FIRST_MINUTE resolving to a bar's first REAL minute print
instead of the bar's own nominal timestamp — diverges whenever that minute isn't exactly H:30,
reproduced on AGQ/GDXU/NUGT/DFEN/WEBL/UGL) — fixed, and this test now covers exactly the
tickers/strategy that exposed it, not just the one that didn't.

Requires cache/research/{TICKER}_1h.csv and cache/research/minute_data/{TICKER}_1m.csv (real
cached data, not fixtures) — skips tickers missing either file.
"""
import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from backtester import run_backtest_ground_truth  # noqa: E402

HOURLY_DIR = os.path.join(ROOT, "cache", "research")
MINUTE_DIR = os.path.join(ROOT, "cache", "research", "minute_data")
START, END = "2024-08-21", "2026-08-20"

# The 12 real `state='live'` watch_list nodes as of 2026-08-21 (soxl_ira + ira accounts) —
# see sim_minute_groundtruth_independent.LIVE_NODE_IDS, kept in sync manually since this
# test intentionally pins a fixed real-node set rather than re-querying live state at
# collect time (a live DB read at test-collection time would make CI/offline runs flaky).
LIVE_NODE_IDS = (92, 197, 202, 231, 235, 236, 203, 229, 230, 232, 233, 234)


def _has_cache(ticker):
    return (os.path.exists(os.path.join(HOURLY_DIR, f"{ticker}_1h.csv"))
            and os.path.exists(os.path.join(MINUTE_DIR, f"{ticker}_1m.csv")))


def _live_nodes():
    from sim_minute_groundtruth_independent import load_nodes
    try:
        nodes = load_nodes(LIVE_NODE_IDS)
    except Exception:
        return []
    return [n for n in nodes if _has_cache(n["ticker"])]


_NODES = _live_nodes()


@pytest.mark.skipif(not _NODES, reason="real live-node cache/DB not present")
@pytest.mark.parametrize("node", _NODES, ids=lambda n: n["ticker"])
@pytest.mark.parametrize("same_bar_reentry", [True, False])
def test_kernel_matches_prototype_byte_identical(node, same_bar_reentry):
    from sim_minute_groundtruth_independent import (
        load_hourly, load_minutes, simulate as proto_simulate, daily_indicators,
    )

    n = node
    dfh = load_hourly(n["ticker"])
    mdf = load_minutes(n["ticker"])

    proto_trades = proto_simulate(n, dfh, mdf, START, END, intrabar="kernel",
                                   backstop=False, same_bar_reentry=same_bar_reentry)

    # Slice dfh to the same window as the prototype's own `bars = df_hourly.loc[start:end]`
    # before handing it to the kernel — passing full-history dfh and post-filtering by
    # entry time (an earlier version of this test) lets the kernel enter the window
    # mid-HOLD/mid-WAIT with in-progress state the prototype never carries in, which can
    # silently add or drop a trade at the window boundary (found by the paired-review
    # independent-cold pass, 2026-08-21: reproduced on DPST 62-vs-63, UGL 91-vs-90 when
    # extended beyond SOXL). ind stays full-history, matching the prototype.
    ind = daily_indicators(dfh, int(n["window"]))
    bars = dfh.loc[START:END + " 23:59:59"]
    kernel_trades = run_backtest_ground_truth(
        bars, ind, n["ticker"], mdf,
        fixed_sl=n["fixed_sl"], arm_pct=n["arm_pct"], trail_buy_pct=n["trail_buy_pct"],
        trail_sell_pct=n["trail_sell_pct"], max_hours_to_hold=int(n["max_hold_hours"]),
        z_score_threshold=float(n["z_score_threshold"]),
        is_both=(n["strategy"] == "TrailingBothZScoreBreakout"),
        open_check_entry_timing=(n["entry_timing"] == "open_check"),
        same_bar_reentry=same_bar_reentry,
    )

    assert len(kernel_trades) == len(proto_trades), (
        f"trade count mismatch: prototype={len(proto_trades)} kernel={len(kernel_trades)}")

    for i, (p, k) in enumerate(zip(proto_trades, kernel_trades)):
        assert p.entry_time == k["Entry Time"], f"trade {i} entry_time"
        assert abs(p.entry_price - k["Entry Price"]) < 1e-9, f"trade {i} entry_price"
        assert p.exit_time == k["Exit Time"], f"trade {i} exit_time"
        assert abs(p.exit_price - k["Exit Price"]) < 1e-9, f"trade {i} exit_price"
        assert p.reason == k["exit_reason"], f"trade {i} reason"
