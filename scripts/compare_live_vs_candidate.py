"""Step 0 of docs/watchlist_candidate_checklist.md (added 2026-09-08): a cheap,
read-only pre-check comparing a ticker's real live watch_list config against a
candidate replacement, using ONLY already-stored numbers (phase4_results,
falling back to the older candidate_verification_results/Phase5 table) --
never triggers a fresh Phase4 scope recompute, which is a much larger job (see
docs/research_log.md's 2026-09-08 "live vs 14 hand-picked replacements" entry --
a naive per-pick campaign-wide recompute took ~7m45s for a single ticker's
scope population, ~60-100min projected for 14).

Real units bug this script guards against (found the hard way, same session):
candidate_verification_results.core_cagr_1m/1s (and addon/drought/core_both)
are raw FRACTIONS (sim_1s_vs_1m_groundtruth_overlays.cagr() returns
bal**(1/years)-1, no *100), while phase4_results' matching columns
(run_optimization_sweep._cagr_from_total_return) are already-scaled PERCENT.
A naive COALESCE across the two tables silently produces a ~100x-too-small
number for any candidate whose data came from the older table. This script's
_resolve() multiplies any Phase5 fallback value by 100 before use, and always
reports which table a number actually came from.

Also guards against the "no candidate_nodes row at all" case: 5 of the first
14 tickers checked this way (AGQ/DPST/GDXU/SOXL/UGL) had a live watch_list
config with no matching candidate_nodes row whatsoever (their real arm value
lives in watch_list.take_profit for TrailingExitZScoreBreakout, not
arm_sell_pct, which is genuinely NULL/unused for that strategy) -- this script
detects that case explicitly and reports it, rather than silently returning
all-n/a live numbers or crashing.

Usage:
    .venv/bin/python scripts/compare_live_vs_candidate.py \\
        --pair AGQ:35688 --pair DFEN:36520 --pair KORU:42146
    .venv/bin/python scripts/compare_live_vs_candidate.py \\
        --pair AGQ:35688 --live-id AGQ:49565   # skip auto-resolution, use a
                                                 # known already-registered live
                                                 # candidate_nodes id directly
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import sqlite3  # noqa: E402
from run_optimization_sweep import DB_PATH  # noqa: E402
from live_db import get_conn as get_live_conn  # noqa: E402

DIMS = [
    ("core", "cagr_pct", "core_cagr_1s"),
    ("addon", "addon_cagr_pct", "addon_cagr_1s"),
    ("drought", "core_drought_cagr_ungated_pct", "drought_cagr_1s"),
    ("both", "core_both_cagr_pct", "core_both_cagr_1s"),
]


def find_live_config(ticker):
    """Real live watch_list row for this ticker, or None. TrailingExitZScoreBreakout's
    real arm value is watch_list.take_profit (arm_sell_pct is genuinely NULL/unused for
    that strategy) -- see this module's own docstring."""
    con = get_live_conn()
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT ticker, strategy, take_profit, window, z_score_threshold, fixed_sl, "
        "arm_sell_pct, trail_buy_pct, trail_sell_pct, max_hold_hours, entry_timing "
        "FROM watch_list WHERE state='live' AND archived_at IS NULL AND ticker=?",
        (ticker,)).fetchone()
    con.close()
    if row is None:
        return None
    is_both = row["strategy"] == "TrailingBothZScoreBreakout"
    arm_pct = row["arm_sell_pct"] if is_both else row["take_profit"]
    return dict(ticker=row["ticker"], strategy=row["strategy"], window=row["window"],
                z=row["z_score_threshold"], fixed_sl=row["fixed_sl"], arm_pct=arm_pct,
                trail_buy_pct=row["trail_buy_pct"], trail_sell_pct=row["trail_sell_pct"],
                max_hold_hours=row["max_hold_hours"], entry_timing=row["entry_timing"])


def resolve_live_candidate_id(conn, ticker, cfg):
    """Existing candidate_nodes.id matching this ticker's exact live param tuple, across
    ANY version -- or None if no such row exists yet (the real, common case for several
    TrailingExitZScoreBreakout tickers, see module docstring)."""
    row = conn.execute(
        "SELECT id FROM candidate_nodes WHERE ticker=? AND strategy=? AND window=? AND z=? "
        "AND fixed_sl=? AND arm_pct=? AND trail_buy_pct=? AND trail_sell_pct=? "
        "AND max_hold_hours=? AND entry_timing=? ORDER BY id DESC LIMIT 1",
        (cfg["ticker"], cfg["strategy"], cfg["window"], cfg["z"], cfg["fixed_sl"],
         cfg["arm_pct"], cfg["trail_buy_pct"], cfg["trail_sell_pct"], cfg["max_hold_hours"],
         cfg["entry_timing"])).fetchone()
    return row[0] if row else None


def fetch(conn, candidate_id):
    if candidate_id is None:
        return None, None, None
    p4 = conn.execute("SELECT * FROM phase4_results WHERE candidate_id=?", (candidate_id,)).fetchone()
    p5 = conn.execute(
        "SELECT * FROM candidate_verification_results WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
        (candidate_id,)).fetchone()
    cn = conn.execute("SELECT worst_neighbor_cagr FROM candidate_nodes WHERE id=?", (candidate_id,)).fetchone()
    return p4, p5, cn


def _resolve(p4, p5, p4_key, p5_key):
    """(value_pct, source) -- prefers phase4_results (post-10f1945-fix, per the
    2026-09-08 consolidation commit 24a6776), falls back to candidate_verification_
    results (Phase5, pre-fix for drought/core_both -- flagged via `source`, not
    silently blended). p5's fraction is *100'd here -- see module docstring."""
    if p4 is not None and p4[p4_key] is not None:
        return p4[p4_key], "phase4"
    if p5 is not None and p5[p5_key] is not None:
        return p5[p5_key] * 100.0, "phase5(pre-drought-fix)"
    return None, None


def compare_one(conn, ticker, pick_id, live_id_override=None):
    cfg = find_live_config(ticker)
    if cfg is None:
        print(f"{ticker}: NO real live watch_list row (state='live', archived_at IS NULL) -- skipping.")
        return None

    live_id = live_id_override or resolve_live_candidate_id(conn, ticker, cfg)
    if live_id is None:
        print(f"{ticker}: live config has NO matching candidate_nodes row at all "
              f"(strategy={cfg['strategy']}, window={cfg['window']}, z={cfg['z']}, "
              f"fixed_sl={cfg['fixed_sl']}, arm_pct={cfg['arm_pct']}) -- no stored numbers exist "
              f"to compare. Register one first (see scripts/verify_live_vs_picks_20260908.py's "
              f"register_live_node for the pattern) if a real comparison is needed.")
        live_row = dict.fromkeys(("core", "addon", "drought", "both", "worst_neighbor"))
        live_src = {}
    else:
        lp4, lp5, lcn = fetch(conn, live_id)
        live_row, live_src = {}, {}
        for dim, p4k, p5k in DIMS:
            live_row[dim], live_src[dim] = _resolve(lp4, lp5, p4k, p5k)
        live_row["worst_neighbor"] = lcn["worst_neighbor_cagr"] if lcn else None

    pp4, pp5, pcn = fetch(conn, pick_id)
    pick_row, pick_src = {}, {}
    for dim, p4k, p5k in DIMS:
        pick_row[dim], pick_src[dim] = _resolve(pp4, pp5, p4k, p5k)
    pick_row["worst_neighbor"] = pcn["worst_neighbor_cagr"] if pcn else None

    return dict(ticker=ticker, live_id=live_id, pick_id=pick_id,
                live=live_row, pick=pick_row, live_src=live_src, pick_src=pick_src)


def fmt(v):
    return f"{v:.1f}" if isinstance(v, (int, float)) else "n/a"


def print_table(results):
    header = (f"{'ticker':6s} | {'L-core':>7s} {'L-addon':>7s} {'L-drt':>7s} {'L-both':>7s} {'L-wn':>7s} | "
              f"{'P-core':>7s} {'P-addon':>7s} {'P-drt':>7s} {'P-both':>7s} {'P-wn':>7s} | winners (core/addon/drt/both)")
    print(header)
    for r in results:
        if r is None:
            continue
        L, P = r["live"], r["pick"]
        winners = []
        for dim in ("core", "addon", "drought", "both"):
            lv, pv = L.get(dim), P.get(dim)
            if lv is None and pv is None:
                winners.append("n/a")
            elif lv is None:
                winners.append("PICK")
            elif pv is None:
                winners.append("LIVE")
            else:
                winners.append("PICK" if pv > lv else "LIVE")
        stale_flags = []
        for dim in ("drought", "both"):
            if r["live_src"].get(dim) == "phase5(pre-drought-fix)":
                stale_flags.append(f"LIVE-{dim} pre-fix")
            if r["pick_src"].get(dim) == "phase5(pre-drought-fix)":
                stale_flags.append(f"PICK-{dim} pre-fix")
        note = "/".join(winners)
        if stale_flags:
            note += "  [" + "; ".join(stale_flags) + "]"
        print(f"{r['ticker']:6s} | {fmt(L.get('core')):>7s} {fmt(L.get('addon')):>7s} "
              f"{fmt(L.get('drought')):>7s} {fmt(L.get('both')):>7s} {fmt(L.get('worst_neighbor')):>7s} | "
              f"{fmt(P.get('core')):>7s} {fmt(P.get('addon')):>7s} {fmt(P.get('drought')):>7s} "
              f"{fmt(P.get('both')):>7s} {fmt(P.get('worst_neighbor')):>7s} | {note}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", action="append", required=True, metavar="TICKER:CANDIDATE_ID",
                     help="repeatable; candidate_nodes id to compare this ticker's live config against")
    ap.add_argument("--live-id", action="append", default=[], metavar="TICKER:CANDIDATE_ID",
                     help="repeatable; skip live-config auto-resolution and use this known "
                          "candidate_nodes id directly for the ticker's live side")
    args = ap.parse_args()

    live_overrides = {}
    for item in args.live_id:
        t, cid = item.split(":")
        live_overrides[t] = int(cid)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    results = []
    for item in args.pair:
        ticker, pick_id = item.split(":")
        results.append(compare_one(conn, ticker, int(pick_id), live_overrides.get(ticker)))
    conn.close()
    print_table(results)


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
