"""Fresh, single-candidate core/addon/drought/core_both CAGR -- one coherent
computation per candidate, avoiding the per-field cross-table stitching
scripts/compare_live_vs_candidate.py's stored-data path can hit (see that
script's docstring: candidate_verification_results and phase4_results can
each have only SOME of the 4 fields populated for an old candidate, so a
naive per-field COALESCE mixes two different historical computation runs
into one row -- confirmed broadly across the 2026-09-08 live-vs-picks
comparison, 12/14 picks affected).

Reuses the SAME real, already-persisted 1-second trade list Phase5 verified
this candidate against (candidate_verification_store.get_phase5_1s_trades,
`phase5_trades` table) and the SAME consolidated formula Phase4 itself now
uses (run_optimization_sweep._stacked_overlay_cagrs_gt, landed 2026-09-08,
commit 24a6776) -- no new simulation logic, no third independent re-
derivation of the trade list. If a candidate has no stored 1s trades
(Phase5 never ran against it), this script says so explicitly rather than
falling back to a fresh kernel resim (that fallback is a materially
different, more expensive operation -- out of scope here by design; see
run_single_backtest_node_ground_truth_isolated / verify_live_vs_picks_
20260908.py's register_live_node for that path if a real resim is wanted).

Usage:
    .venv/bin/python scripts/fresh_single_candidate_overlay.py --candidate-id 35688
    .venv/bin/python scripts/fresh_single_candidate_overlay.py --candidate-id 35688 --candidate-id 49565
"""
import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__)) + "/.."
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import strategies  # noqa: E402
from backtester import simulate_drought_overlay_ground_truth, drought_included_excluded_ground_truth  # noqa: E402
from candidate_verification_store import get_phase5_1s_trades  # noqa: E402
from sim_1s_vs_1m_groundtruth_overlays import load_hourly  # noqa: E402
from candidate_summary_report import _window_dates_from_version  # noqa: E402
from run_optimization_sweep import (  # noqa: E402
    DB_PATH, _campaign_years_for_window, _summarize_trades_ground_truth,
    _stacked_overlay_cagrs_gt, compute_bh_returns, GT_DROUGHT_IE_VOL_GATE,
)

DATA_SOURCE = "massive"


def _load_node(conn, candidate_id):
    row = conn.execute(
        "SELECT id, ticker, strategy, version, window, z, fixed_sl, arm_pct, trail_buy_pct, "
        "trail_sell_pct, max_hold_hours, entry_timing FROM candidate_nodes WHERE id=?",
        (candidate_id,)).fetchone()
    if row is None:
        raise SystemExit(f"candidate_nodes id={candidate_id} not found")
    return dict(row)


def compute_fresh(conn, candidate_id):
    node = _load_node(conn, candidate_id)
    trades = get_phase5_1s_trades(conn, candidate_id, ticker=node["ticker"],
                                   strategy=node["strategy"], fixed_sl=node["fixed_sl"])
    if not trades:
        return dict(candidate_id=candidate_id, ticker=node["ticker"], ok=False,
                     reason="no stored 1s trades in phase5_trades -- Phase5 never verified "
                            "this candidate (real for every candidate registered fresh today, "
                            "e.g. the live-config rows -- Phase5 is no longer invoked per-"
                            "campaign as of the 2026-09-08 consolidation, so no NEW candidate "
                            "will ever get a phase5_trades row going forward either)")

    start_date, end_date = _window_dates_from_version(node["version"])
    years = _campaign_years_for_window(node["ticker"], start_date, end_date, data_source=DATA_SOURCE)
    if not years:
        return dict(candidate_id=candidate_id, ticker=node["ticker"], ok=False,
                     reason=f"could not resolve a real years span from version={node['version']!r}")

    _, spy_bh = compute_bh_returns(node["ticker"], start_date=start_date, end_date=end_date,
                                    data_source=DATA_SOURCE)
    core_cagr = _summarize_trades_ground_truth(trades, spy_bh, years)[5]

    drought = drought_ie = None
    if strategies.uses_arm_trail_exit(node["strategy"]):
        # Sliced to the candidate's real campaign window (start_date/end_date, parsed
        # from its own version string above) -- MUST match gt_rows_for_scope's own
        # df_hourly_windowed, not the ticker's full unwindowed history. Confirmed by
        # direct comparison (candidate 35688): full-history drought search found a
        # different "best" confirm_days/vol_gate combo than the windowed search
        # (83.9 vs the real stored 27.2), a real result-changing discrepancy, not
        # just noise -- drought's own grid search is sensitive to how much history
        # it's allowed to look across.
        dfh_full = load_hourly(node["ticker"], data_source=DATA_SOURCE)
        dfh = dfh_full.loc[start_date:end_date + " 23:59:59"] if start_date else dfh_full
        drought = simulate_drought_overlay_ground_truth(
            trades, dfh, node["ticker"], node["fixed_sl"],
            arm_pct=node["arm_pct"], trail_sell_pct=node["trail_sell_pct"])
        if drought is not None and drought.get("best_confirm_days") is not None:
            drought_ie = drought_included_excluded_ground_truth(
                trades, dfh, node["ticker"], node["fixed_sl"],
                arm_pct=node["arm_pct"], trail_sell_pct=node["trail_sell_pct"],
                confirm_days=drought["best_confirm_days"], vol_gate=GT_DROUGHT_IE_VOL_GATE)

    addon_ungated, drought_ungated, both_gated = _stacked_overlay_cagrs_gt(trades, drought, drought_ie, years)
    return dict(candidate_id=candidate_id, ticker=node["ticker"], ok=True, n_trades=len(trades),
                years=years, core=core_cagr, addon=addon_ungated, drought=drought_ungated, both=both_gated)


def fmt(v):
    return f"{v:.1f}" if isinstance(v, (int, float)) else "n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate-id", action="append", type=int, required=True, dest="candidate_ids")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    results = [compute_fresh(conn, cid) for cid in args.candidate_ids]
    conn.close()

    print(f"{'candidate_id':>12s} {'ticker':6s} {'n_trades':>8s} {'years':>6s} "
          f"{'core':>7s} {'addon':>7s} {'drought':>7s} {'both':>7s}  note")
    for r in results:
        if not r["ok"]:
            print(f"{r['candidate_id']:>12d} {r['ticker']:6s} {'':8s} {'':6s} "
                  f"{'n/a':>7s} {'n/a':>7s} {'n/a':>7s} {'n/a':>7s}  SKIPPED: {r['reason']}")
            continue
        print(f"{r['candidate_id']:>12d} {r['ticker']:6s} {r['n_trades']:>8d} {r['years']:>6.2f} "
              f"{fmt(r['core']):>7s} {fmt(r['addon']):>7s} {fmt(r['drought']):>7s} {fmt(r['both']):>7s}")


if __name__ == "__main__":
    main()
