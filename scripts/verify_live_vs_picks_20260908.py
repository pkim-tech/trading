"""One-off (2026-09-08): real, grounded comparison of 14 hand-picked replacement
candidates against each ticker's actual current live watch_list config -- dispatched
by peer session "research (3)". Read-only against watch_list/candidate_nodes except
for registering the live configs themselves (5 tickers -- AGQ/DPST/GDXU/SOXL/UGL --
have no existing candidate_nodes row for their exact live param tuple) as new
candidate_nodes rows under a dedicated version tag, then running the same real
Phase4 pipeline (run_candidate_nodes_campaign_verification.run_phase4) already used
tonight for KORU/HIBL against both the live rows and the 14 picks.

Phase5 was retired earlier tonight (2026-09-08, commit a7f5a58 lineage) --
Phase4 now computes core/addon/drought/core_both CAGR itself, off whatever trades
resolution is actually available (1s if a massive_second_derived build is active
for the ticker, else 1m) -- there is no live dual 1m/1s pair anymore for a fresh
Phase4 run; `phase4_results` is the current source of truth, not the older
`candidate_verification_results` table Phase5 used to populate.

Usage:
    .venv/bin/python scripts/verify_live_vs_picks_20260908.py
"""
import os
import sys
import sqlite3
import time

ROOT = os.path.dirname(os.path.abspath(__file__)) + "/.."
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from run_optimization_sweep import DB_PATH, compute_bh_returns, run_single_backtest_node_ground_truth_isolated
from locate_best_node import ensure_candidate_nodes_table, get_or_create_candidate_node
import run_candidate_nodes_campaign_verification as rcncv

LIVE_VERSION = "live-config-resim-2026-09-08-massive-w2021-08-23_2026-08-21"
LIVE_START, LIVE_END = "2021-08-23", "2026-08-21"
DATA_SOURCE = "massive"

# arm_pct for the 5 TrailingExit tickers is watch_list.take_profit (NOT arm_sell_pct,
# which is genuinely NULL/unused for this strategy in watch_list -- confirmed by direct
# query, 2026-09-08: TrailingExitZScoreBreakout's real arm value lives in take_profit).
LIVE_CONFIGS = {
    "AGQ":  dict(strategy="TrailingExitZScoreBreakout",  window=10, z=1.0, fixed_sl=2.0, arm_pct=4.0,  trail_buy_pct=0.0, trail_sell_pct=11.0, max_hold_hours=91,  entry_timing="open_check"),
    "DFEN": dict(strategy="TrailingBothZScoreBreakout",  window=20, z=1.0, fixed_sl=3.0, arm_pct=30.0, trail_buy_pct=3.0, trail_sell_pct=1.0, max_hold_hours=133, entry_timing="open_check"),
    "DPST": dict(strategy="TrailingExitZScoreBreakout",  window=20, z=1.0, fixed_sl=1.0, arm_pct=13.0, trail_buy_pct=0.0, trail_sell_pct=13.0, max_hold_hours=140, entry_timing="open_check"),
    "ETHU": dict(strategy="TrailingBothZScoreBreakout",  window=10, z=1.0, fixed_sl=1.0, arm_pct=1.0,  trail_buy_pct=2.0, trail_sell_pct=7.0, max_hold_hours=56,  entry_timing="open_check"),
    "GDXU": dict(strategy="TrailingExitZScoreBreakout",  window=20, z=1.5, fixed_sl=1.0, arm_pct=9.0,  trail_buy_pct=0.0, trail_sell_pct=16.0, max_hold_hours=77,  entry_timing="open_check"),
    "HIBL": dict(strategy="TrailingBothZScoreBreakout",  window=10, z=1.0, fixed_sl=3.0, arm_pct=28.0, trail_buy_pct=3.0, trail_sell_pct=3.0, max_hold_hours=91,  entry_timing="open_check"),
    "JNUG": dict(strategy="TrailingBothZScoreBreakout",  window=20, z=1.5, fixed_sl=3.0, arm_pct=30.0, trail_buy_pct=3.0, trail_sell_pct=6.0, max_hold_hours=126, entry_timing="open_check"),
    "KORU": dict(strategy="TrailingBothZScoreBreakout",  window=10, z=1.0, fixed_sl=3.0, arm_pct=20.0, trail_buy_pct=10.0, trail_sell_pct=1.0, max_hold_hours=98,  entry_timing="open_check"),
    "LABU": dict(strategy="TrailingBothZScoreBreakout",  window=20, z=1.0, fixed_sl=5.0, arm_pct=13.0, trail_buy_pct=7.0, trail_sell_pct=1.0, max_hold_hours=77,  entry_timing="open_check"),
    "NUGT": dict(strategy="TrailingBothZScoreBreakout",  window=10, z=1.0, fixed_sl=2.0, arm_pct=25.0, trail_buy_pct=5.0, trail_sell_pct=1.0, max_hold_hours=77,  entry_timing="open_check"),
    "OILU": dict(strategy="TrailingBothZScoreBreakout",  window=20, z=1.0, fixed_sl=2.0, arm_pct=2.0,  trail_buy_pct=1.0, trail_sell_pct=7.0, max_hold_hours=112, entry_timing="open_check"),
    "SOXL": dict(strategy="TrailingExitZScoreBreakout",  window=10, z=1.0, fixed_sl=5.0, arm_pct=8.0,  trail_buy_pct=0.0, trail_sell_pct=8.0, max_hold_hours=28,  entry_timing="open_check"),
    "UGL":  dict(strategy="TrailingExitZScoreBreakout",  window=10, z=1.0, fixed_sl=2.0, arm_pct=2.0,  trail_buy_pct=0.0, trail_sell_pct=5.0, max_hold_hours=119, entry_timing="open_check"),
    "WEBL": dict(strategy="TrailingBothZScoreBreakout",  window=10, z=1.5, fixed_sl=4.0, arm_pct=28.0, trail_buy_pct=9.0, trail_sell_pct=4.0, max_hold_hours=140, entry_timing="open_check"),
}

PICK_IDS = {
    "AGQ": 35688, "DFEN": 36520, "JNUG": 41198, "GDXU": 39781, "HIBL": 40390,
    "LABU": 43258, "NUGT": 43981, "SOXL": 46210, "UGL": 47161, "WEBL": 48128,
    "DPST": 37751, "KORU": 42146, "ETHU": 38604, "OILU": 45191,
}

LOG = os.path.join(ROOT, "logs", "verify_live_vs_picks_20260908.log")


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def register_live_node(conn, ticker, cfg):
    """Returns candidate_nodes.id for this ticker's live config under LIVE_VERSION,
    registering a fresh row (computing a real GT-kernel alpha/trades snapshot to
    satisfy candidate_nodes' NOT NULL robust_alpha/trades columns) if none exists
    yet. The registered robust_alpha is a real single-fill-resolution alpha_calc
    (compounded - spy_bh), NOT the 3-way possible/pessimistic/certain MIN that
    'robust_alpha' means elsewhere in this DB -- acceptable here since this row
    exists only to give Phase4 a real candidate_nodes anchor; the real reported
    numbers for this task come from phase4_results, not this field."""
    is_both = cfg["strategy"] == "TrailingBothZScoreBreakout"
    existing = conn.execute(
        "SELECT id FROM candidate_nodes WHERE ticker=? AND version=? AND strategy=? AND window=?",
        (ticker, LIVE_VERSION, cfg["strategy"], cfg["window"])).fetchone()
    if existing:
        log(f"{ticker}: live-config candidate_nodes row already registered, id={existing[0]}")
        return existing[0]

    log(f"{ticker}: computing real GT-kernel alpha/trades for live config (window={cfg['window']})...")
    asset_bh, spy_bh = compute_bh_returns(ticker, start_date=LIVE_START, end_date=LIVE_END, data_source=DATA_SOURCE)

    arm_pct = cfg["arm_pct"]
    if is_both:
        tp, sl, trail_pct_pct = arm_pct, cfg["trail_buy_pct"], cfg["trail_sell_pct"]
    else:
        tp, sl, trail_pct_pct = arm_pct, cfg["trail_sell_pct"], 0.0

    args = (ticker, cfg["strategy"], LIVE_VERSION, tp, sl, cfg["max_hold_hours"], cfg["window"],
            spy_bh, cfg["z"], cfg["fixed_sl"], trail_pct_pct, cfg["entry_timing"], True,
            LIVE_START, LIVE_END, DATA_SOURCE, "second")
    t0 = time.monotonic()
    result = run_single_backtest_node_ground_truth_isolated(args)
    log(f"{ticker}: kernel run status={result.get('status')} elapsed={time.monotonic()-t0:.1f}s")
    if result.get("status") != "SUCCESS":
        log(f"{ticker}: FAILED to register live config -- {result}")
        return None
    alpha_calc, n_trades, win_rate, compounded, win_twin_rate, node_cagr = result["payload"]

    node = {
        "ticker": ticker, "strategy": cfg["strategy"], "version": LIVE_VERSION,
        "window": cfg["window"], "z": cfg["z"], "fixed_sl": cfg["fixed_sl"],
        "arm_pct": arm_pct,
        "trail_buy_pct": cfg["trail_buy_pct"], "trail_sell_pct": cfg["trail_sell_pct"],
        "max_hold_hours": cfg["max_hold_hours"], "entry_timing": cfg["entry_timing"],
        "robust_alpha": alpha_calc, "trades": n_trades,
        "computed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    cid = get_or_create_candidate_node(conn, node)
    log(f"{ticker}: registered live-config candidate_nodes id={cid} (trades={n_trades}, cagr={node_cagr})")
    return cid


def run_scope(ticker, version, window, label):
    log(f"=== Phase4: {label} -- {ticker} / window={window} ===")
    t0 = time.monotonic()
    try:
        rcncv.run_phase4(ticker, version, window, DATA_SOURCE)
    except Exception as e:
        log(f"{ticker} ({label}) Phase4 FAILED: {e!r}")
        return
    log(f"{ticker} ({label}) Phase4 done in {time.monotonic()-t0:.1f}s")


def main():
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    log("=== starting: live-config registration + Phase4 for 14 lives + 14 picks ===")
    conn = sqlite3.connect(DB_PATH)
    ensure_candidate_nodes_table(conn)

    live_ids = {}
    for ticker, cfg in LIVE_CONFIGS.items():
        live_ids[ticker] = register_live_node(conn, ticker, cfg)
    conn.close()

    tickers_in_order = list(LIVE_CONFIGS.keys())
    for ticker in tickers_in_order:
        run_scope(ticker, LIVE_VERSION, LIVE_CONFIGS[ticker]["window"], "LIVE")

    pick_version = "v6.5.1-bench-inmemory-v6-massive-w2021-08-23_2026-08-21-z0.5-1.0-1.5-2.0-isl3-pv4"
    conn = sqlite3.connect(DB_PATH)
    for ticker in tickers_in_order:
        row = conn.execute("SELECT window FROM candidate_nodes WHERE id=?", (PICK_IDS[ticker],)).fetchone()
        pick_window = row[0]
        run_scope(ticker, pick_version, pick_window, "PICK")
    conn.close()

    log("=== all scopes done -- building final table ===")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    out_lines = ["ticker | live core | live addon | live drought | live core_both | live worst_nbr | "
                 "pick core | pick addon | pick drought | pick core_both | pick worst_nbr"]
    for ticker in tickers_in_order:
        lid = live_ids[ticker]
        pid = PICK_IDS[ticker]

        def fetch(cid):
            if cid is None:
                return None
            p4 = conn.execute("SELECT * FROM phase4_results WHERE candidate_id=?", (cid,)).fetchone()
            cn = conn.execute("SELECT worst_neighbor_cagr FROM candidate_nodes WHERE id=?", (cid,)).fetchone()
            return p4, cn

        lp4, lcn = fetch(lid)
        pp4, pcn = fetch(pid)

        def g(row, key):
            return f"{row[key]:.2f}" if row and row[key] is not None else "n/a"

        line = (f"{ticker} | {g(lp4,'cagr_pct')} | {g(lp4,'addon_cagr_pct')} | {g(lp4,'drought_compounded_pct')} | "
                f"{g(lp4,'core_both_cagr_pct')} | {g(lcn,'worst_neighbor_cagr') if lcn else 'n/a'} | "
                f"{g(pp4,'cagr_pct')} | {g(pp4,'addon_cagr_pct')} | {g(pp4,'drought_compounded_pct')} | "
                f"{g(pp4,'core_both_cagr_pct')} | {g(pcn,'worst_neighbor_cagr') if pcn else 'n/a'}")
        out_lines.append(line)
    conn.close()

    out_path = os.path.join(ROOT, "output", "live_vs_picks_20260908.txt")
    os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
    with open(out_path, "w") as f:
        f.write("\n".join(out_lines))
    log(f"=== DONE -- table written to {out_path} ===")
    for line in out_lines:
        log(line)


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
