"""One-off (not a pytest gate): extend tests/test_ground_truth_kernel_parity.py's exact
byte-identical comparison methodology from its 2yr window (2024-08-21..2026-08-20) to SOXL's
full available history (2023-07-24, SOXL_1h.csv's real start, through 2026-08-21, today).

Purpose: the existing parity test proved the numba-ported production kernel
(backtester.run_backtest_ground_truth) matches the original independent clean-room prototype
(scripts/sim_minute_groundtruth_independent.py) byte-for-byte over 2 years. This script checks
whether that agreement holds over the full 3-year window the data actually supports, or whether
something in the extra ~1yr breaks parity. Read-only/compute-only -- does not modify any kernel
file, does not touch live state, does not launch a grid sweep (single real SOXL config only).
"""
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from backtester import run_backtest_ground_truth  # noqa: E402
from sim_minute_groundtruth_independent import (  # noqa: E402
    load_nodes, load_hourly, load_minutes, simulate as proto_simulate, daily_indicators,
)

HOURLY_DIR = os.path.join(ROOT, "cache", "research")
START, END = "2023-07-24", "2026-08-21"
SOXL_NODE_ID = 92


def run_one(same_bar_reentry):
    n = load_nodes((SOXL_NODE_ID,))[0]
    dfh = load_hourly(n["ticker"])
    mdf = load_minutes(n["ticker"])

    proto_trades = proto_simulate(n, dfh, mdf, START, END, intrabar="kernel",
                                   backstop=False, same_bar_reentry=same_bar_reentry)

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

    print(f"\n=== same_bar_reentry={same_bar_reentry} ===")
    print(f"node config: strategy={n['strategy']} window={n['window']} z={n['z_score_threshold']} "
          f"fixed_sl={n['fixed_sl']} arm_pct={n['arm_pct']} trail_buy_pct={n['trail_buy_pct']} "
          f"trail_sell_pct={n['trail_sell_pct']} max_hold_hours={n['max_hold_hours']} "
          f"entry_timing={n['entry_timing']}")
    print(f"prototype trades: {len(proto_trades)}   kernel trades: {len(kernel_trades)}")

    n_compare = min(len(proto_trades), len(kernel_trades))
    mismatches = []
    for i in range(n_compare):
        p, k = proto_trades[i], kernel_trades[i]
        fields_ok = (
            p.entry_time == k["Entry Time"]
            and abs(p.entry_price - k["Entry Price"]) < 1e-9
            and p.exit_time == k["Exit Time"]
            and abs(p.exit_price - k["Exit Price"]) < 1e-9
            and p.reason == k["exit_reason"]
        )
        if not fields_ok:
            mismatches.append((i, p, k))

    if len(proto_trades) == len(kernel_trades) and not mismatches:
        print(f"VERDICT: byte-identical over the full {START}..{END} window "
              f"({len(proto_trades)} trades).")
        return True

    print(f"VERDICT: DIVERGENT. count mismatch={len(proto_trades) != len(kernel_trades)}, "
          f"field mismatches in first {n_compare} common trades: {len(mismatches)}")

    if len(proto_trades) != len(kernel_trades):
        print(f"  prototype={len(proto_trades)} trades, kernel={len(kernel_trades)} trades "
              f"(diverge at index {n_compare} onward, or earlier if field mismatches below)")

    for i, p, k in mismatches[:20]:
        print(f"  --- mismatch at trade index {i} ---")
        print(f"    prototype: entry={p.entry_time} entry_px={p.entry_price:.6f} "
              f"exit={p.exit_time} exit_px={p.exit_price:.6f} reason={p.reason}")
        print(f"    kernel:    entry={k['Entry Time']} entry_px={k['Entry Price']:.6f} "
              f"exit={k['Exit Time']} exit_px={k['Exit Price']:.6f} reason={k['exit_reason']}")

    if len(mismatches) > 20:
        print(f"  ... {len(mismatches) - 20} more mismatches not shown")

    # If counts differ, show the tail of whichever side is longer, to see where divergence starts
    if len(proto_trades) > n_compare:
        print(f"  prototype has {len(proto_trades) - n_compare} extra trailing trades, first one:")
        p = proto_trades[n_compare]
        print(f"    entry={p.entry_time} entry_px={p.entry_price:.6f} exit={p.exit_time} "
              f"exit_px={p.exit_price:.6f} reason={p.reason}")
    if len(kernel_trades) > n_compare:
        print(f"  kernel has {len(kernel_trades) - n_compare} extra trailing trades, first one:")
        k = kernel_trades[n_compare]
        print(f"    entry={k['Entry Time']} entry_px={k['Entry Price']:.6f} exit={k['Exit Time']} "
              f"exit_px={k['Exit Price']:.6f} reason={k['exit_reason']}")

    return False


if __name__ == "__main__":
    results = {}
    for sbr in (True, False):
        results[sbr] = run_one(sbr)
    print("\n=== SUMMARY ===")
    for sbr, ok in results.items():
        print(f"same_bar_reentry={sbr}: {'BYTE-IDENTICAL' if ok else 'DIVERGENT'}")
