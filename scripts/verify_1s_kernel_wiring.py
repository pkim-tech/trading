"""One-off verification (2026-09-06): confirms the real Phase2.5/Phase4 1s wiring
(run_optimization_sweep._load_node_inputs_ground_truth's fill_resolution param,
run_single_backtest_node_ground_truth_isolated's optional 17th tuple element)
actually runs at 1s resolution and produces sane output, for the same 3 real
candidates the standalone PoC (scripts/poc_1s_cliffbox_check.py) already checked.

NOT expected to reproduce the PoC's own 1s numbers exactly: the PoC (mirroring
Phase5's own precedent) read the RAW, UNADJUSTED 1s CSV; this wiring deliberately
uses db_cache.get_massive_second_ohlcv (the real dividend-ADJUSTED table) instead
(see docs/deep_backlog.md's 2026-09-06 entry for why). A ticker with real
dividends (SOXL: 31 records) should show a real, expected divergence from the
PoC's raw numbers; a ticker with none/few (GDXU/ETHU) should match closely.

Usage:
  .venv/bin/python scripts/verify_1s_kernel_wiring.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from run_optimization_sweep import run_single_backtest_node_ground_truth_isolated

# (candidate_id label, ticker, strategy, tp/arm, sl/trail_buy, hold, window, z, fixed_sl,
#  tpct/trail_sell, entry_timing) -- own cell from each PoC run.
CASES = [
    ("GDXU id=40169", "GDXU", "TrailingExitZScoreBreakout", 21, 13, 133, 20, 1.0, 8.0, 13.0, "open_check"),
    ("SOXL id=23768", "SOXL", "TrailingBothZScoreBreakout", 8, 7, 70, 5, 1.0, 7.0, 3.0, "close"),
    ("ETHU id=38547", "ETHU", "TrailingBothZScoreBreakout", 3, 2, 21, 5, 0.5, 6.0, 3.0, "open_check"),
]

START, END = "2021-08-23", "2026-08-21"

for label, ticker, strategy, tp, sl, hold, w, z, fixed_sl, tpct, entry_timing in CASES:
    for resolution in ("minute", "second"):
        args = (ticker, strategy, "verify-1s-wiring", tp, sl, hold, w, 0.0, z, fixed_sl,
                tpct, entry_timing, True, START, END, "massive", resolution)
        res = run_single_backtest_node_ground_truth_isolated(args)
        alpha, n_trades, wr, compounded, wtw, node_cagr = res["payload"]
        print(f"{label:16s} [{resolution:6s}] status={res['status']:8s} trades={n_trades:4d} "
              f"cagr={node_cagr if node_cagr is None else f'{node_cagr:.2f}%'}")
