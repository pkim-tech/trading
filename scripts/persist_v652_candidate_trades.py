"""Generates + persists real drought-only (core GT trades + drought-window trades) data
for specific candidate_nodes rows into phase5_trades/phase5_drought_windows -- a narrower,
immediate workaround for the "Phase4/Phase5 consolidation never ported trade-level
persistence" gap (docs/deep_backlog.md, found 2026-09-13) while the real general fix
(add a persist step to candidate_full_review.gt_full_review_rows's curated-population
trade list) is unbuilt. Dispatched need: a real roth capital-overlap walk (JNUG+addon vs
KORU/DFEN idle capital) needs real trade-by-trade entry/exit timing for 3 v6.5.2
candidates, all drought-overlay-only (no addon).

Reuses the exact existing patterns, not reimplemented:
- kernel call: scripts/dump_gt_trades_single_node.py's load_hourly/load_minutes/
  daily_indicators + backtester.run_backtest_ground_truth(need_times=True) pattern.
- core-trade persistence: candidate_verification_store.insert_trades, the same
  phase5_trades schema/upsert scripts/phase5_second_level_overlay_check.py already
  uses (has 'armed'/'arm_time'/'arm_price' per trade -- confirmed sufficient for a
  later analysis to derive the addon leg's timing on top, without a separate
  with-addon resimulation).

New, small: phase5_drought_windows table. phase5_trades' schema has no way to
represent a drought window (no fixed-SL/arm/trail-exit shape -- it's a separate
buy-and-manage interval between real signals, see backtester.
simulate_drought_overlay_ground_truth's own docstring) without conflating it with a
real strategy trade, so drought windows get their own table instead of being forced
into phase5_trades' trade_idx/armed columns for a meaning they don't have.

Only 1m resolution is generated here (not 1m+1s) -- this task only needs real
entry/exit timing for a capital-overlap walk, not Phase5's own 1m-vs-1s granularity-
sensitivity comparison, so the ~1-4min 1-second-file load per ticker is skipped as
real, unneeded cost.

Usage:
    .venv/bin/python scripts/persist_v652_candidate_trades.py
"""
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__)) + "/.."
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from backtester import run_backtest_ground_truth, simulate_drought_overlay_ground_truth  # noqa: E402
from sim_minute_groundtruth_independent import load_hourly, load_minutes, daily_indicators  # noqa: E402
from candidate_verification_store import insert_trades  # noqa: E402
from run_optimization_sweep import DB_PATH  # noqa: E402

START = "2021-08-23"
END = "2026-08-21"
DATA_SOURCE = "massive"
RESOLUTION = "1m"

CANDIDATE_IDS = [53043, 56740, 61509]  # KORU, DFEN, JNUG -- all drought-only per dispatch


def ensure_drought_windows_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS phase5_drought_windows (
            candidate_id INTEGER, ticker TEXT, strategy TEXT, version TEXT,
            resolution TEXT, window_idx INTEGER, start_time TEXT, end_time TEXT,
            return_pct REAL, confirm_days INTEGER, vol_gate REAL, created_at TEXT,
            UNIQUE(candidate_id, resolution, window_idx)
        )""")


def insert_drought_windows(conn, cid, ticker, strategy, version, resolution, drought):
    ensure_drought_windows_table(conn)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    buffer = []
    for i, (ret, (start_t, end_t)) in enumerate(zip(drought["best_rets"], drought["best_window_times"])):
        buffer.append((
            cid, ticker, strategy, version, resolution, i, str(start_t), str(end_t),
            ret * 100, drought["best_confirm_days"], drought["best_vol_gate"], now_iso,
        ))
    before = conn.execute(
        "SELECT COUNT(*) FROM phase5_drought_windows WHERE candidate_id=? AND resolution=?",
        (cid, resolution)).fetchone()[0]
    conn.executemany("""
        INSERT OR IGNORE INTO phase5_drought_windows
            (candidate_id, ticker, strategy, version, resolution, window_idx,
             start_time, end_time, return_pct, confirm_days, vol_gate, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, buffer)
    conn.commit()
    after = conn.execute(
        "SELECT COUNT(*) FROM phase5_drought_windows WHERE candidate_id=? AND resolution=?",
        (cid, resolution)).fetchone()[0]
    return after - before, len(buffer)


def process_candidate(conn, cid):
    row = conn.execute("SELECT * FROM candidate_nodes WHERE id=?", (cid,)).fetchone()
    cols = [d[0] for d in conn.execute("SELECT * FROM candidate_nodes WHERE id=?", (cid,)).description]
    node = dict(zip(cols, row))
    ticker = node["ticker"]
    is_both = node["strategy"] == "TrailingBothZScoreBreakout"

    t0 = time.time()
    print(f"[{ticker} id={cid}] loading hourly+minute ({DATA_SOURCE})...", flush=True)
    dfh = load_hourly(ticker, data_source=DATA_SOURCE)
    mdf = load_minutes(ticker, data_source=DATA_SOURCE)
    ind = daily_indicators(dfh, int(node["window"]))
    bars = dfh.loc[START:END + " 23:59:59"]
    print(f"      {len(dfh):,} hourly rows, {len(mdf):,} minute rows, "
          f"elapsed={time.time()-t0:.1f}s", flush=True)

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
        conn, cid, RESOLUTION, node["version"], ticker, node["strategy"], node["fixed_sl"], trades)
    print(f"      phase5_trades: {n_new}/{n_total} new rows (resolution={RESOLUTION})", flush=True)

    print(f"[{ticker} id={cid}] running drought overlay...", flush=True)
    drought = simulate_drought_overlay_ground_truth(
        trades, bars, ticker, fixed_sl=node["fixed_sl"], arm_pct=node["arm_pct"],
        trail_sell_pct=node["trail_sell_pct"])
    if drought is None or drought.get("best_rets") is None:
        print(f"      no real drought windows found for {ticker} id={cid}", flush=True)
        n_dw_new = n_dw_total = 0
    else:
        n_dw_new, n_dw_total = insert_drought_windows(
            conn, cid, ticker, node["strategy"], node["version"], RESOLUTION, drought)
        print(f"      phase5_drought_windows: {n_dw_new}/{n_dw_total} new rows "
              f"(confirm_days={drought['best_confirm_days']}, vol_gate={drought['best_vol_gate']})",
              flush=True)

    # Sanity check against the already-stored Phase4 aggregate CAGR for this candidate.
    years = (bars.index.max() - bars.index.min()).total_seconds() / (365.25 * 86400)
    core_bal = 1.0
    for t in trades:
        core_bal *= (1 + t["Return"])
    core_cagr = (core_bal ** (1 / years) - 1) * 100
    stored = conn.execute(
        "SELECT cagr_pct, core_drought_cagr_ungated_pct FROM phase4_results WHERE candidate_id=?",
        (cid,)).fetchone()
    print(f"      resimulated core CAGR={core_cagr:.2f}% vs stored phase4 cagr_pct="
          f"{stored[0]:.2f}% (n_trades resim={len(trades)} vs stored={node['trades']})", flush=True)
    if drought is not None and drought.get("combined_compounded_pct") is not None:
        combined_bal = 1.0 + drought["combined_compounded_pct"] / 100.0
        combined_cagr = (combined_bal ** (1 / years) - 1) * 100
        print(f"      resimulated core+drought CAGR={combined_cagr:.2f}% vs stored "
              f"core_drought_cagr_ungated_pct={stored[1]:.2f}%", flush=True)

    return {"ticker": ticker, "cid": cid, "n_trades": len(trades), "core_cagr": core_cagr,
            "stored_cagr": stored[0], "n_trades_new": n_new, "n_dw_new": n_dw_new}


def main():
    conn = sqlite3.connect(DB_PATH)
    results = []
    for cid in CANDIDATE_IDS:
        results.append(process_candidate(conn, cid))
        print(f"--- done: candidate_id={cid} ---\n", flush=True)
    conn.close()
    print("Summary:")
    for r in results:
        print(f"  {r['ticker']} id={r['cid']}: {r['n_trades']} trades, "
              f"resim CAGR={r['core_cagr']:.2f}% vs stored={r['stored_cagr']:.2f}%, "
              f"{r['n_trades_new']} trade rows / {r['n_dw_new']} drought-window rows written")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
