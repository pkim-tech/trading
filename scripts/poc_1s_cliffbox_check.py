"""PoC (2026-09-06, research session dispatch): does re-simulating a real
candidate's Phase2.5 cliffbox neighborhood at 1-second fill resolution instead
of today's minute resolution change the cliff-safety verdict, and is it
affordable? Scoped narrowly and deliberately independent of
docs/plans/one_shot_per_ticker_pipeline_design.md's open questions.

Reuses backtester.run_backtest_ground_truth directly (the same @njit kernel
every GT phase uses, confirmed granularity-agnostic by
scripts/phase5_second_level_overlay_check.py's own docstring) rather than
rerunning the full Phase1->Island search -- the real research question is
resolution sensitivity of the winner's own neighborhood, not re-deriving the
island search itself (which would cost hours, not seconds, for no added
signal here).

Method: pick one real candidate_nodes row, rebuild its Phase2.5 cliffbox
neighbor cell set (mirrors bench_phase1_phase2_inmemory.cliffbox_tasks_for_cell:
+-CLIFF_RADIUS on the two strategy-mapped axes, +-7h hold among HOLD_TIME_CAPS,
window/z held fixed), then run every cell through run_backtest_ground_truth
twice -- once with minute_df=1-minute-resampled bars (matches today's real
Phase1/2/2.5 path), once with minute_df=raw 1-second bars -- and compare
per-cell CAGR, worst_neighbor_cagr, and wall-clock cost.

Read-only: no backtester.py/bench_phase1_phase2_inmemory.py edit, no
candidate_nodes write. Prints a full comparison table and the summary numbers
to copy into docs/research_log.md by hand (research-log convention: append
directly on a real finding, not gated on session close).

Usage:
  .venv/bin/python scripts/poc_1s_cliffbox_check.py --candidate-id 40169
"""
import argparse
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import pandas as pd

from backtester import run_backtest_ground_truth
from sim_1s_vs_1m_groundtruth_overlays import (
    load_hourly, resample_seconds_to_minutes, daily_indicators, SECOND_DIR,
)


def load_seconds_lean(ticker, chunksize=250_000):
    """Memory-conservative variant of sim_1s_vs_1m_groundtruth_overlays.load_seconds
    for this PoC only -- that shared loader reads the full raw CSV (Volume/VWAP/
    NumTrades included) into memory as chunks BEFORE filtering to regular-session
    bars, then concats; for a 2.6GB ticker (SOXL, ~22M rows) that repeatedly OOM-
    killed this PoC on a 15GB box. This version filters to regular-session bars
    and drops unused columns PER CHUNK, before concatenation, so peak memory is
    bounded by one chunk's raw size plus the already-small filtered running total,
    not the full raw file. Same tz handling/session window as the original."""
    path = os.path.join(SECOND_DIR, f"{ticker}_1s.csv")
    parts = []
    for chunk in pd.read_csv(path, chunksize=chunksize,
                              usecols=["timestamp", "Open", "High", "Low", "Close"]):
        ts = pd.to_datetime(chunk["timestamp"], utc=True).dt.tz_convert("US/Eastern").dt.tz_localize(None)
        chunk = chunk.set_index(ts)[["Open", "High", "Low", "Close"]].astype("float32")
        t = chunk.index.time
        keep = (t >= pd.Timestamp("09:30").time()) & (t < pd.Timestamp("16:00").time())
        parts.append(chunk.loc[keep])
    return pd.concat(parts).sort_index()

DB_PATH = os.path.join(ROOT, "cache", "research", "trading_universe.db")
CLIFF_RADIUS = 2
HOLD_TIME_CAPS = [7, 14, 21, 28, 35, 42, 49, 56, 63, 70, 77, 84, 91, 98, 105,
                  112, 119, 126, 133, 140]
START = "2021-08-23"
END = "2026-08-21"


def load_candidate(candidate_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, ticker, strategy, version, window, z, fixed_sl, arm_pct, "
        "trail_buy_pct, trail_sell_pct, max_hold_hours, entry_timing, trades, "
        "worst_neighbor_cagr FROM candidate_nodes WHERE id=?", (candidate_id,),
    ).fetchone()
    conn.close()
    if row is None:
        raise SystemExit(f"candidate_nodes id={candidate_id} not found")
    return dict(row)


def neighbor_cells(cand):
    """Mirrors cliffbox_tasks_for_cell's neighborhood shape. For is_both=False
    (TrailingExitZScoreBreakout): 'take_profit' axis = arm_pct, 'stop_loss'
    axis = trail_sell_pct, trail_buy_pct stays 0.0 (unused for this strategy).
    For is_both=True (TrailingBothZScoreBreakout): 'take_profit' axis =
    arm_pct, 'stop_loss' axis = trail_buy_pct, trail_sell_pct (the tpct axis)
    held fixed at the candidate's own value -- same simplification as the
    GDXU PoC's trail_buy_pct=0 fixation, since the real trail_pct-neighbor
    step needs the full sweep's TRAIL_PCTS grid, not reconstructable here.
    window/z held fixed either way, matching the real Phase2.5 cliffbox."""
    is_both = cand["strategy"] == "TrailingBothZScoreBreakout"
    hold_c = cand["max_hold_hours"]
    holds = [h for h in HOLD_TIME_CAPS if abs(h - hold_c) <= 7]
    if is_both:
        arm_c, buy_c = cand["arm_pct"], cand["trail_buy_pct"]
        arms = [a for a in range(int(arm_c) - CLIFF_RADIUS, int(arm_c) + CLIFF_RADIUS + 1) if a >= 1]
        buys = [b for b in range(int(buy_c) - CLIFF_RADIUS, int(buy_c) + CLIFF_RADIUS + 1) if b >= 1]
        return [dict(arm_pct=float(a), trail_buy_pct=float(b), trail_sell_pct=cand["trail_sell_pct"],
                      max_hold_hours=h)
                for a in arms for b in buys for h in holds]
    arm_c, trail_c = cand["arm_pct"], cand["trail_sell_pct"]
    arms = [a for a in range(int(arm_c) - CLIFF_RADIUS, int(arm_c) + CLIFF_RADIUS + 1) if a >= 1]
    trails = [t for t in range(int(trail_c) - CLIFF_RADIUS, int(trail_c) + CLIFF_RADIUS + 1) if t >= 1]
    return [dict(arm_pct=float(a), trail_buy_pct=0.0, trail_sell_pct=float(t), max_hold_hours=h)
            for a in arms for t in trails for h in holds]


def run_cell(cand, cell, dfh, ind, minute_df):
    is_both = cand["strategy"] == "TrailingBothZScoreBreakout"
    trades = run_backtest_ground_truth(
        dfh, ind, cand["ticker"], minute_df,
        fixed_sl=cand["fixed_sl"], arm_pct=cell["arm_pct"], trail_buy_pct=cell["trail_buy_pct"],
        trail_sell_pct=cell["trail_sell_pct"], max_hours_to_hold=cell["max_hold_hours"],
        z_score_threshold=cand["z"], is_both=is_both,
        open_check_entry_timing=(cand["entry_timing"] == "open_check"),
        same_bar_reentry=True, need_times=False,
    )
    bal = 10_000.0
    for tr in trades:
        bal *= (1 + tr["Return"])
    return bal, len(trades)


def cagr(bal, years, start_bal=10_000.0):
    if bal <= 0:
        return -100.0
    return ((bal / start_bal) ** (1 / years) - 1) * 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate-id", type=int, required=True)
    args = ap.parse_args()

    cand = load_candidate(args.candidate_id)
    print(f"Candidate: {cand}")
    cells = neighbor_cells(cand)
    print(f"{len(cells)} neighbor cells (incl. the winner's own cell)")

    print(f"\nLoading {cand['ticker']} hourly (massive) data...")
    dfh = load_hourly(cand["ticker"], data_source="massive")
    ind = daily_indicators(dfh, int(cand["window"]))
    bars = dfh.loc[START:END + " 23:59:59"]
    years = (bars.index.max() - bars.index.min()).total_seconds() / (365.25 * 86400)
    print(f"years={years:.2f}")

    t0 = time.monotonic()
    df_1s = load_seconds_lean(cand["ticker"])
    print(f"  {len(df_1s):,} 1s rows, load took {time.monotonic()-t0:.1f}s")
    df_1m = resample_seconds_to_minutes(df_1s)
    print(f"  {len(df_1m):,} 1m rows (resampled)")

    results = []
    t_1m0 = time.monotonic()
    for cell in cells:
        bal, n = run_cell(cand, cell, bars, ind, df_1m)
        results.append(dict(cell, bal_1m=bal, cagr_1m=cagr(bal, years), trades_1m=n))
    t_1m = time.monotonic() - t_1m0
    print(f"\n1m pass: {t_1m:.1f}s total, {t_1m/len(cells):.3f}s/cell")

    t_1s0 = time.monotonic()
    for r in results:
        bal, n = run_cell(cand, r, bars, ind, df_1s)
        r["bal_1s"], r["cagr_1s"], r["trades_1s"] = bal, cagr(bal, years), n
    t_1s = time.monotonic() - t_1s0
    print(f"1s pass: {t_1s:.1f}s total, {t_1s/len(cells):.3f}s/cell")

    winner_1m = max(results, key=lambda r: r["cagr_1m"])
    winner_1s = max(results, key=lambda r: r["cagr_1s"])
    own_cell = next(r for r in results if r["arm_pct"] == cand["arm_pct"]
                     and r["trail_buy_pct"] == cand["trail_buy_pct"]
                     and r["trail_sell_pct"] == cand["trail_sell_pct"]
                     and r["max_hold_hours"] == cand["max_hold_hours"])
    worst_1m = min(r["cagr_1m"] for r in results)
    worst_1s = min(r["cagr_1s"] for r in results)

    print(f"\n=== Own cell (arm={cand['arm_pct']}, trail_sell={cand['trail_sell_pct']}, "
          f"hold={cand['max_hold_hours']}) ===")
    print(f"  1m: cagr={own_cell['cagr_1m']:.2f}% trades={own_cell['trades_1m']} "
          f"(real campaign recorded {cand['trades']} trades)")
    print(f"  1s: cagr={own_cell['cagr_1s']:.2f}% trades={own_cell['trades_1s']}")

    print(f"\n=== Neighborhood-wide winner ===")
    print(f"  1m winner: arm={winner_1m['arm_pct']} trail_buy={winner_1m['trail_buy_pct']} "
          f"trail_sell={winner_1m['trail_sell_pct']} hold={winner_1m['max_hold_hours']} "
          f"cagr={winner_1m['cagr_1m']:.2f}%")
    print(f"  1s winner: arm={winner_1s['arm_pct']} trail_buy={winner_1s['trail_buy_pct']} "
          f"trail_sell={winner_1s['trail_sell_pct']} hold={winner_1s['max_hold_hours']} "
          f"cagr={winner_1s['cagr_1s']:.2f}%")
    same_winner = (winner_1m["arm_pct"] == winner_1s["arm_pct"]
                   and winner_1m["trail_buy_pct"] == winner_1s["trail_buy_pct"]
                   and winner_1m["trail_sell_pct"] == winner_1s["trail_sell_pct"]
                   and winner_1m["max_hold_hours"] == winner_1s["max_hold_hours"])
    print(f"  Same cell wins under both resolutions: {same_winner}")

    print(f"\n=== worst_neighbor_cagr ===")
    print(f"  1m: {worst_1m:.2f}%  (real campaign recorded {cand['worst_neighbor_cagr']:.2f}%)")
    print(f"  1s: {worst_1s:.2f}%")
    print(f"  delta: {worst_1s - worst_1m:+.2f}pp")

    print(f"\n=== Cost ===")
    print(f"  1m pass: {t_1m:.1f}s ({len(cells)} cells)")
    print(f"  1s pass: {t_1s:.1f}s ({len(cells)} cells)")
    print(f"  1s/1m cost ratio: {t_1s/t_1m:.1f}x")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
