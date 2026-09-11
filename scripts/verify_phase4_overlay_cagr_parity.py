"""Parity check: Phase4's own stacked overlay CAGRs vs Phase5's overlay_cagrs().

Serves the 2026-09-08 Phase5-consolidation work (run_optimization_sweep.
_stacked_overlay_cagrs_gt): Phase4 now computes core+addon / core+drought / gated
core_both itself, so Phase5 no longer runs per campaign. This script proves the ported
computation reproduces Phase5's own post-10f1945 numbers on the IDENTICAL trade list.

Method (deliberately holds everything except the computation itself fixed):
  * trade list  -- the real stored 1-second trades from `phase5_trades`, loaded through
                   the same candidate_verification_store.get_phase5_1s_trades Phase4's
                   build_candidate_report_ground_truth itself prefers.
  * hourly bars -- Phase5's own load_hourly() frame, passed to BOTH sides (Phase4's real
                   runtime frame is that same frame windowed to the campaign dates; the
                   window is applied here too so neither side sees extra bars).
  * years       -- Phase5's own hourly-bar-span years, passed to both sides.
So any delta is a real logic/formula difference, not an input difference. The one KNOWN,
intended delta is Phase4's drought included-vs-excluded REAL_SELECTION vol-gate override
(candidate_full_review.py's long-standing behavior, which Phase5 never had) -- the script
reports Phase4 both with and without it so that delta is isolated, not hand-waved.

Usage:
  .venv/bin/python scripts/verify_phase4_overlay_cagr_parity.py --limit 8
  .venv/bin/python scripts/verify_phase4_overlay_cagr_parity.py --candidate-ids 49546,49545
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategies  # noqa: E402
from backtester import simulate_drought_overlay_ground_truth  # noqa: E402
from run_optimization_sweep import (  # noqa: E402
    DB_PATH, _stacked_overlay_cagrs_gt, drought_included_excluded_ground_truth,
    GT_DROUGHT_IE_VOL_GATE,
)
from candidate_verification_store import get_phase5_1s_trades  # noqa: E402
from phase5_second_level_overlay_check import overlay_cagrs  # noqa: E402
from sim_1s_vs_1m_groundtruth_overlays import load_hourly  # noqa: E402


def _window_from_version(version):
    for part in version.split("-"):
        if part.startswith("w20"):
            body = part[1:]
            break
    else:
        return None, None
    tail = version.split("w", 1)[1]
    start = tail[:10]
    end = tail[11:21]
    return start, end


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--candidate-ids", default=None)
    ap.add_argument("--data-source", default="massive")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    if args.candidate_ids:
        ids = [int(x) for x in args.candidate_ids.split(",")]
        q = ("SELECT id, ticker, strategy, version, window, z, fixed_sl, arm_pct, trail_buy_pct, "
             "trail_sell_pct, max_hold_hours, entry_timing FROM candidate_nodes WHERE id IN "
             f"({','.join('?' * len(ids))})")
        cands = conn.execute(q, ids).fetchall()
    else:
        cands = conn.execute(
            "SELECT n.id, n.ticker, n.strategy, n.version, n.window, n.z, n.fixed_sl, n.arm_pct, "
            "n.trail_buy_pct, n.trail_sell_pct, n.max_hold_hours, n.entry_timing "
            "FROM candidate_nodes n WHERE n.id IN "
            "(SELECT DISTINCT candidate_id FROM phase5_trades WHERE resolution='1s') "
            "ORDER BY n.id DESC LIMIT ?", (args.limit,)).fetchall()

    dfh_cache = {}
    print(f"{'cand':>7} {'ticker':>6} {'strat':>5} {'n_tr':>5} | "
          f"{'P4 addon':>9} {'P5 addon':>9} | {'P4 drght':>9} {'P5 drght':>9} | "
          f"{'P4 both':>9} {'P5 both':>9} {'delta':>8}  note")
    for (cid, ticker, strategy, version, window, z, fixed_sl, arm_pct, trail_buy_pct,
         trail_sell_pct, max_hold_hours, entry_timing) in cands:
        start, end = _window_from_version(version)
        trades = get_phase5_1s_trades(conn, cid, ticker=ticker, strategy=strategy,
                                      fixed_sl=fixed_sl, start_date=start, end_date=end)
        if not trades:
            print(f"{cid:>7} {ticker:>6}  -- no usable stored 1s trades (window-validated out)")
            continue
        if ticker not in dfh_cache:
            dfh_cache[ticker] = load_hourly(ticker, data_source=args.data_source)
        dfh = dfh_cache[ticker].loc[start:end + " 23:59:59"]
        years = (dfh.index.max() - dfh.index.min()).total_seconds() / (365.25 * 86400)

        node = dict(ticker=ticker, strategy=strategy, window=window, z_score_threshold=z,
                    fixed_sl=fixed_sl, arm_pct=arm_pct, trail_buy_pct=trail_buy_pct,
                    trail_sell_pct=trail_sell_pct, max_hold_hours=max_hold_hours,
                    entry_timing=entry_timing, id=cid)
        _p5_core, p5_addon, p5_drought, p5_both = overlay_cagrs(trades, ticker, dfh, node, years)

        drought = None
        drought_ie = None
        if strategies.uses_arm_trail_exit(strategy):
            drought = simulate_drought_overlay_ground_truth(
                trades, dfh, ticker, fixed_sl, arm_pct=arm_pct, trail_sell_pct=trail_sell_pct)
            if drought is not None and drought.get("best_confirm_days") is not None:
                drought_ie = drought_included_excluded_ground_truth(
                    trades, dfh, ticker, fixed_sl, arm_pct=arm_pct,
                    trail_sell_pct=trail_sell_pct, confirm_days=drought["best_confirm_days"],
                    vol_gate=GT_DROUGHT_IE_VOL_GATE)
        p4_addon, p4_drought, p4_both = _stacked_overlay_cagrs_gt(trades, drought, drought_ie, years)
        # Same call with the REAL_SELECTION override suppressed -- isolates the one
        # intended divergence from Phase5.
        _, _, p4_both_no_ie = _stacked_overlay_cagrs_gt(trades, drought, None, years)

        def pct(v, scale=1.0):
            return "n/a" if v is None else f"{v * scale:8.3f}%"

        note = ""
        if drought_ie is not None and drought_ie.get("verdict") == "REAL_SELECTION":
            note = f"REAL_SELECTION override (no-IE both={pct(p4_both_no_ie)})"
        delta = ("n/a" if (p4_both is None or p5_both is None)
                 else f"{p4_both - p5_both * 100.0:+8.4f}")
        print(f"{cid:>7} {ticker:>6} {strategy[:5]:>5} {len(trades):>5} | "
              f"{pct(p4_addon)} {pct(p5_addon, 100)} | {pct(p4_drought)} {pct(p5_drought, 100)} | "
              f"{pct(p4_both)} {pct(p5_both, 100)} {delta:>8}  {note}")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
