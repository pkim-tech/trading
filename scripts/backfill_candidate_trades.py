"""Generalized backfill of real trade-by-trade data into phase5_trades/
phase5_drought_windows for a given list of candidate_nodes ids -- generalizes
scripts/persist_v652_candidate_trades.py's one-off 3-candidate pattern (kept in the
tree as a reference of the original ad hoc version) into a reusable tool for any
future "these specific candidates need real trade data now" need (docs/watchlist_
candidate_checklist.md check 19's real-world use case: a promoted node with no
recorded trades because it predates the Phase 9 write step in candidate_full_review.
gt_full_review_rows, or was promoted from outside the curated population Phase 9
covers).

Calls the existing unmodified GT kernel functions only (backtester.
run_backtest_ground_truth / simulate_drought_overlay_ground_truth) -- no kernel-module
edit, so the CLAUDE.md paired-review gate does not apply to this script itself.

Skips (by default) any candidate_id that already has phase5_trades rows -- this is a
backfill for genuinely missing data, not a forced re-resim; pass --force to
regenerate anyway (e.g. after a real kernel bugfix makes existing rows stale).

Drought windows are only generated for strategies backtester.simulate_drought_overlay_
ground_truth actually supports (strategies.uses_arm_trail_exit) -- matches that
function's own real strategy gate, not a new assumption.

Usage:
    .venv/bin/python scripts/backfill_candidate_trades.py --candidate-ids 56298,55138,58312
    .venv/bin/python scripts/backfill_candidate_trades.py --candidate-ids 50073 --force
"""
import argparse
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__)) + "/.."
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import strategies  # noqa: E402
from backtester import run_backtest_ground_truth, simulate_drought_overlay_ground_truth  # noqa: E402
from sim_minute_groundtruth_independent import load_hourly, load_minutes, daily_indicators  # noqa: E402
from candidate_verification_store import insert_trades, insert_drought_windows  # noqa: E402
from run_optimization_sweep import DB_PATH  # noqa: E402
from candidate_summary_report import _window_dates_from_version  # noqa: E402

DATA_SOURCE = "massive"
RESOLUTION = "1m"


def process_candidate(conn, cid, force):
    row = conn.execute("SELECT * FROM candidate_nodes WHERE id=?", (cid,)).fetchone()
    if row is None:
        print(f"[id={cid}] MISSING from candidate_nodes -- skipping", flush=True)
        return None
    cols = [d[0] for d in conn.execute("SELECT * FROM candidate_nodes WHERE id=?", (cid,)).description]
    node = dict(zip(cols, row))
    ticker = node["ticker"]
    strategy = node["strategy"]

    existing = conn.execute(
        "SELECT COUNT(*) FROM phase5_trades WHERE candidate_id=?", (cid,)).fetchone()[0]
    if existing and not force:
        print(f"[{ticker} id={cid}] already has {existing} phase5_trades rows -- skipping "
              f"(pass --force to regenerate)", flush=True)
        return {"ticker": ticker, "cid": cid, "skipped": True}

    is_both = strategy == "TrailingBothZScoreBreakout"
    win_start, win_end = _window_dates_from_version(node["version"])

    t0 = time.time()
    print(f"[{ticker} id={cid}] loading hourly+minute ({DATA_SOURCE})...", flush=True)
    dfh = load_hourly(ticker, data_source=DATA_SOURCE)
    mdf = load_minutes(ticker, data_source=DATA_SOURCE)
    ind = daily_indicators(dfh, int(node["window"]))
    bars = dfh.loc[win_start:win_end + " 23:59:59"]

    print(f"[{ticker} id={cid}] running GT kernel...", flush=True)
    trades = run_backtest_ground_truth(
        bars, ind, ticker, mdf,
        fixed_sl=node["fixed_sl"], arm_pct=node["arm_pct"], trail_buy_pct=node["trail_buy_pct"],
        trail_sell_pct=node["trail_sell_pct"], max_hours_to_hold=node["max_hold_hours"],
        z_score_threshold=node["z"], is_both=is_both,
        open_check_entry_timing=(node["entry_timing"] == "open_check"),
        same_bar_reentry=True, need_times=True,
    )
    print(f"      {len(trades)} core trades, elapsed={time.time()-t0:.1f}s", flush=True)

    n_new, n_total = insert_trades(
        conn, cid, RESOLUTION, node["version"], ticker, strategy, node["fixed_sl"], trades)
    print(f"      phase5_trades: {n_new}/{n_total} new rows (resolution={RESOLUTION})", flush=True)

    n_dw_new = n_dw_total = 0
    if strategies.uses_arm_trail_exit(strategy):
        drought = simulate_drought_overlay_ground_truth(
            trades, bars, ticker, fixed_sl=node["fixed_sl"], arm_pct=node["arm_pct"],
            trail_sell_pct=node["trail_sell_pct"])
        n_dw_new, n_dw_total = insert_drought_windows(
            conn, cid, ticker, strategy, node["version"], RESOLUTION, drought)
        print(f"      phase5_drought_windows: {n_dw_new}/{n_dw_total} new rows", flush=True)
    else:
        print(f"      {strategy} does not support the drought overlay -- skipping", flush=True)

    years = (bars.index.max() - bars.index.min()).total_seconds() / (365.25 * 86400)
    core_bal = 1.0
    for t in trades:
        core_bal *= (1 + t["Return"])
    core_cagr = (core_bal ** (1 / years) - 1) * 100 if years else None
    stored = conn.execute(
        "SELECT cagr_pct FROM phase4_results WHERE candidate_id=?", (cid,)).fetchone()
    stored_cagr = stored[0] if stored else None
    print(f"      resimulated core CAGR={core_cagr:.2f}% vs stored phase4 cagr_pct="
          f"{stored_cagr if stored_cagr is None else round(stored_cagr, 2)}% "
          f"(n_trades resim={len(trades)} vs candidate_nodes.trades={node['trades']})", flush=True)

    return {"ticker": ticker, "cid": cid, "n_trades": len(trades), "core_cagr": core_cagr,
            "stored_cagr": stored_cagr, "n_new": n_new, "n_dw_new": n_dw_new, "skipped": False}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate-ids", required=True,
                     help="comma-separated candidate_nodes ids to backfill.")
    ap.add_argument("--force", action="store_true",
                     help="regenerate even if phase5_trades already has rows for a candidate.")
    args = ap.parse_args()
    ids = [int(x) for x in args.candidate_ids.split(",") if x.strip()]

    conn = sqlite3.connect(DB_PATH)
    results = []
    for cid in ids:
        r = process_candidate(conn, cid, args.force)
        if r is not None:
            results.append(r)
        print(f"--- done: candidate_id={cid} ---\n", flush=True)
    conn.close()

    print("Summary:")
    for r in results:
        if r.get("skipped"):
            print(f"  {r['ticker']} id={r['cid']}: skipped (already had data)")
        else:
            print(f"  {r['ticker']} id={r['cid']}: {r['n_trades']} trades, "
                  f"resim CAGR={r['core_cagr']:.2f}% vs stored={r['stored_cagr']}, "
                  f"{r['n_new']} trade rows / {r['n_dw_new']} drought-window rows written")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
