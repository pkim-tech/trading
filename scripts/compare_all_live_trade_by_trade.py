"""One-off (not a pytest gate): extend compare_soxl_full_history_prototype_vs_kernel.py's
byte-identical comparison to all 12 real live tickers, each over its own hourly data's full
real range, using the exact same methodology (which itself extends
tests/test_ground_truth_kernel_parity.py's trusted 2yr comparison).

Purpose: SOXL alone was confirmed byte-identical over its full 3yr window. Minute-data
coverage varies a lot by ticker (per sim_minute_groundtruth_independent.py's own docstring:
HIBL ~25%, DFEN ~53%, WEBL ~51%, KORU ~57% vs SOXL's much higher coverage), so SOXL's result
does not necessarily generalize -- this checks the other 11 tickers directly rather than
assuming it does. Read-only/compute-only -- does not modify any kernel file, does not touch
live state, does not launch a grid sweep (one real live config per ticker, not a grid search).
"""
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from backtester import run_backtest_ground_truth  # noqa: E402
from sim_minute_groundtruth_independent import (  # noqa: E402
    load_nodes, load_hourly, load_minutes, simulate as proto_simulate, daily_indicators,
)

HOURLY_DIR = os.path.join(ROOT, "cache", "research")

# Real live node ids for the 12 real capital-at-stake tickers, verified fresh
# 2026-08-22 via: SELECT id, ticker FROM watch_list WHERE state='live'
# AND archived_at IS NULL AND starting_notional >= 5000 ORDER BY ticker
LIVE_NODE_IDS = {
    "AGQ": 203, "DFEN": 232, "DPST": 197, "GDXU": 229, "HIBL": 235, "JNUG": 231,
    "KORU": 202, "LABU": 236, "NUGT": 230, "SOXL": 92, "UGL": 233, "WEBL": 234,
}

END = "2026-08-21"  # today, real end of both hourly and minute data for every ticker


def hourly_start(ticker):
    path = os.path.join(HOURLY_DIR, f"{ticker}_1h.csv")
    df = pd.read_csv(path, index_col=0, parse_dates=True, nrows=1)
    return df.index[0].strftime("%Y-%m-%d")


def run_one(ticker, node_id, start, same_bar_reentry):
    n = load_nodes((node_id,))[0]
    dfh = load_hourly(n["ticker"])
    mdf = load_minutes(n["ticker"])

    proto_trades = proto_simulate(n, dfh, mdf, start, END, intrabar="kernel",
                                   backstop=False, same_bar_reentry=same_bar_reentry)

    ind = daily_indicators(dfh, int(n["window"]))
    bars = dfh.loc[start:END + " 23:59:59"]
    kernel_trades = run_backtest_ground_truth(
        bars, ind, n["ticker"], mdf,
        fixed_sl=n["fixed_sl"], arm_pct=n["arm_pct"], trail_buy_pct=n["trail_buy_pct"],
        trail_sell_pct=n["trail_sell_pct"], max_hours_to_hold=int(n["max_hold_hours"]),
        z_score_threshold=float(n["z_score_threshold"]),
        is_both=(n["strategy"] == "TrailingBothZScoreBreakout"),
        open_check_entry_timing=(n["entry_timing"] == "open_check"),
        same_bar_reentry=same_bar_reentry,
    )

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

    ok = (len(proto_trades) == len(kernel_trades) and not mismatches)
    detail = {
        "ticker": ticker, "same_bar_reentry": same_bar_reentry, "start": start, "end": END,
        "proto_n": len(proto_trades), "kernel_n": len(kernel_trades),
        "n_compare": n_compare, "n_mismatch": len(mismatches), "ok": ok,
        "mismatches": mismatches[:10],
        "extra_proto": proto_trades[n_compare] if len(proto_trades) > n_compare else None,
        "extra_kernel": kernel_trades[n_compare] if len(kernel_trades) > n_compare else None,
    }
    return detail


if __name__ == "__main__":
    all_results = []
    for ticker, node_id in LIVE_NODE_IDS.items():
        start = hourly_start(ticker)
        print(f"\n########## {ticker} (node {node_id}), window {start}..{END} ##########")
        for sbr in (True, False):
            d = run_one(ticker, node_id, start, sbr)
            all_results.append(d)
            status = "BYTE-IDENTICAL" if d["ok"] else "DIVERGENT"
            print(f"  same_bar_reentry={sbr}: {status}  "
                  f"proto={d['proto_n']} kernel={d['kernel_n']} mismatches={d['n_mismatch']}")
            if not d["ok"]:
                for i, p, k in d["mismatches"]:
                    print(f"    --- mismatch idx {i} ---")
                    print(f"      prototype: entry={p.entry_time} entry_px={p.entry_price:.6f} "
                          f"exit={p.exit_time} exit_px={p.exit_price:.6f} reason={p.reason}")
                    print(f"      kernel:    entry={k['Entry Time']} entry_px={k['Entry Price']:.6f} "
                          f"exit={k['Exit Time']} exit_px={k['Exit Price']:.6f} reason={k['exit_reason']}")
                if d["extra_proto"] is not None:
                    p = d["extra_proto"]
                    print(f"    prototype has {d['proto_n'] - d['n_compare']} extra trailing trades, first: "
                          f"entry={p.entry_time} entry_px={p.entry_price:.6f} exit={p.exit_time} "
                          f"exit_px={p.exit_price:.6f} reason={p.reason}")
                if d["extra_kernel"] is not None:
                    k = d["extra_kernel"]
                    print(f"    kernel has {d['kernel_n'] - d['n_compare']} extra trailing trades, first: "
                          f"entry={k['Entry Time']} entry_px={k['Entry Price']:.6f} exit={k['Exit Time']} "
                          f"exit_px={k['Exit Price']:.6f} reason={k['exit_reason']}")

    print("\n\n=== FINAL SUMMARY ===")
    for d in all_results:
        status = "BYTE-IDENTICAL" if d["ok"] else "DIVERGENT"
        print(f"{d['ticker']:6s} sbr={str(d['same_bar_reentry']):5s} [{d['start']}..{d['end']}]  "
              f"{status}  proto={d['proto_n']:4d} kernel={d['kernel_n']:4d} mismatches={d['n_mismatch']}")
