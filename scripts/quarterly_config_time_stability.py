"""
Cross-applies each quarterly rolling-window campaign's winning ("best safe node") config
against every OTHER window's already-computed backtest_cache data, for the same ticker --
built for scripts/run_quarterly_soxl_sweep.sh's real 2026-08-15 SOXL run.

Answers a different question than the rolling-window sweep itself: the sweep tells you "what's
the best config if you re-optimize fresh at the start of each quarter" -- this tells you "does
last quarter's winning config still work in the quarters that follow it," i.e. real time-decay
of a fixed config, using data that's already sitting in backtest_cache (no re-sweep needed).

Requires the windowed campaign to already have run via run_quarterly_soxl_sweep.sh (or any
run_optimization_sweep.py --version v5-w{start}_{end} --start-date --end-date campaign) for
every window being compared -- this only reads backtest_cache, never runs the kernel.

Usage: .venv/bin/python scripts/quarterly_config_time_stability.py --ticker SOXL
       .venv/bin/python scripts/quarterly_config_time_stability.py --ticker SOXL --node-ids 360 363 358 357 362 355 369 365
"""
import argparse
import sqlite3
from pathlib import Path

DB_PATH = Path("cache/research/trading_universe.db")


def get_node(conn, node_id):
    row = conn.execute(
        "SELECT ticker, strategy, version, window, z, fixed_sl, arm_pct, trail_buy_pct, "
        "trail_sell_pct, max_hold_hours, entry_timing, robust_alpha, trades "
        "FROM candidate_nodes WHERE id=?", (node_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no candidate_nodes row for id={node_id}")
    cols = ["ticker", "strategy", "version", "window", "z", "fixed_sl", "arm_pct",
            "trail_buy_pct", "trail_sell_pct", "max_hold_hours", "entry_timing",
            "robust_alpha", "trades"]
    return dict(zip(cols, row))


def all_windowed_versions(conn, ticker):
    rows = conn.execute(
        "SELECT DISTINCT version FROM backtest_cache WHERE ticker=? AND version LIKE 'v5-w%' "
        "ORDER BY version", (ticker,)
    ).fetchall()
    return [r[0] for r in rows]


def lookup(conn, node, version):
    # take_profit holds the arm-sell/TP threshold for TrailingExitZScoreBreakout nodes;
    # arm_sell_pct holds it for TrailingBothZScoreBreakout (never both populated on the
    # same row -- see docs/research_log.md's 2026-08-04 correction entry).
    if node["strategy"] == "TrailingBothZScoreBreakout":
        tp_clause, tp_val = "arm_sell_pct=?", node["arm_pct"]
    else:
        tp_clause, tp_val = "take_profit=?", node["arm_pct"]

    row = conn.execute(f"""
        SELECT strategy_return, alpha_vs_spy, alpha_vs_spy_pessimistic, alpha_vs_spy_certain, trades
        FROM backtest_cache
        WHERE ticker=? AND strategy=? AND version=? AND window=? AND z_score_threshold=?
          AND fixed_sl=? AND {tp_clause} AND trail_buy_pct=? AND trail_sell_pct=?
          AND max_hold_hours=? AND entry_timing=?
    """, (node["ticker"], node["strategy"], version, node["window"], node["z"], node["fixed_sl"],
          tp_val, node["trail_buy_pct"], node["trail_sell_pct"], node["max_hold_hours"],
          node["entry_timing"])).fetchone()
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="SOXL")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--node-ids", nargs="*", type=int, default=None,
                     help="candidate_nodes ids to cross-apply, one per window (default: "
                          "auto-detect the 'best safe node' from each windowed version)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    versions = all_windowed_versions(conn, args.ticker)
    if not versions:
        print(f"no windowed (v5-w*) backtest_cache data for {args.ticker}")
        return

    if args.node_ids:
        node_ids = args.node_ids
    else:
        node_ids = []
        for v in versions:
            row = conn.execute(
                "SELECT id FROM candidate_nodes WHERE ticker=? AND version=? "
                "ORDER BY robust_alpha DESC LIMIT 1", (args.ticker, v)
            ).fetchone()
            if row:
                node_ids.append(row[0])

    nodes = [get_node(conn, nid) for nid in node_ids]
    # sort by the window's start date (encoded in the version string) so the matrix reads
    # chronologically
    nodes.sort(key=lambda n: n["version"])
    versions.sort()

    print(f"{args.ticker}: {len(nodes)} configs x {len(versions)} windows\n")
    header = "config (from window)".ljust(28) + "".join(v[4:14].rjust(13) for v in versions)
    print(header)
    for node in nodes:
        origin = node["version"][4:14]
        cells = []
        for v in versions:
            r = lookup(conn, node, v)
            if r is None:
                cells.append("no data".rjust(13))
            else:
                strategy_return, alpha, alpha_p, alpha_c, trades = r
                cells.append(f"{alpha:+.1f}%(n={trades})".rjust(13))
        print(f"{origin} [{node['strategy'][:12]}]".ljust(28) + "".join(cells))

    print(f"\nDiagonal cells (config's own origin window) are the alpha already reported by "
          f"candidate_full_review.py. Off-diagonal cells show the same exact config applied to "
          f"data from other windows -- real time-stability, no re-sweep.")


if __name__ == "__main__":
    main()
