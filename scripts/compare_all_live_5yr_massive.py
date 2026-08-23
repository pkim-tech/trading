"""5yr Massive-data extension of compare_all_live_trade_by_trade.py (3yr Yahoo).
Same methodology, same two implementations (prototype vs production kernel), just
data_source="massive" and the full 2021-08-23..2026-08-21 window every ticker's
massive_hourly_derived/massive_hourly_derived build now covers. Read-only/compute-only.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from backtester import run_backtest_ground_truth  # noqa: E402
from sim_minute_groundtruth_independent import (  # noqa: E402
    load_nodes, load_hourly, load_minutes, simulate as proto_simulate, daily_indicators,
)

LIVE_NODE_IDS = {
    "AGQ": 203, "DFEN": 232, "DPST": 197, "GDXU": 229, "HIBL": 235, "JNUG": 231,
    "KORU": 202, "LABU": 236, "NUGT": 230, "SOXL": 92, "UGL": 233, "WEBL": 234,
}

START = "2021-08-23"
END = "2026-08-21"
DATA_SOURCE = "massive"


def run_one(ticker, node_id, same_bar_reentry):
    n = load_nodes((node_id,))[0]
    dfh = load_hourly(n["ticker"], data_source=DATA_SOURCE)
    mdf = load_minutes(n["ticker"], data_source=DATA_SOURCE)

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
    return {
        "ticker": ticker, "same_bar_reentry": same_bar_reentry,
        "proto_n": len(proto_trades), "kernel_n": len(kernel_trades),
        "n_mismatch": len(mismatches), "ok": ok, "mismatches": mismatches[:5],
    }


if __name__ == "__main__":
    all_results = []
    for ticker, node_id in LIVE_NODE_IDS.items():
        for sbr in (True, False):
            d = run_one(ticker, node_id, sbr)
            all_results.append(d)
            status = "BYTE-IDENTICAL" if d["ok"] else "DIVERGENT"
            print(f"{ticker:6s} sbr={str(sbr):5s} {status}  proto={d['proto_n']:4d} "
                  f"kernel={d['kernel_n']:4d} mismatches={d['n_mismatch']}")
            for i, p, k in d["mismatches"]:
                print(f"    --- mismatch idx {i} ---")
                print(f"      prototype: entry={p.entry_time} entry_px={p.entry_price:.6f} "
                      f"exit={p.exit_time} exit_px={p.exit_price:.6f} reason={p.reason}")
                print(f"      kernel:    entry={k['Entry Time']} entry_px={k['Entry Price']:.6f} "
                      f"exit={k['Exit Time']} exit_px={k['Exit Price']:.6f} reason={k['exit_reason']}")

    print("\n=== SUMMARY ===")
    n_ok = sum(1 for d in all_results if d["ok"])
    print(f"{n_ok}/{len(all_results)} byte-identical")
