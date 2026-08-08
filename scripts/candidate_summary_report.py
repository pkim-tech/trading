"""Consolidated liquidity-screen candidate summary -- core alpha, worst
neighbor (cliff-safety, computed for the ACTUAL winning node regardless of
whether it passes -- unlike top_safe_nodes.py, which only reports a node if
it clears the safety bar), liquidity, and drought/add-on overlay results,
all in one table. Built 2026-08-08 after repeatedly rebuilding pieces of
this table by hand in conversation -- consolidates locate_best_node.py's
winner pick, top_safe_nodes.py's neighbor-check logic, and
candidate_overlay_results into one script.

Usage:
  .venv/bin/python scripts/candidate_summary_report.py TNA URTY SQQQ ...
  .venv/bin/python scripts/candidate_summary_report.py --all-swept
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from top_safe_nodes import CLIFF_RADIUS

DB_PATH = "cache/research/trading_universe.db"
ROBUST_ALPHA_SQL = ("MIN(alpha_vs_spy, COALESCE(alpha_vs_spy_pessimistic, alpha_vs_spy), "
                     "COALESCE(alpha_vs_spy_certain, alpha_vs_spy))")


def best_node(conn, ticker, version="v5"):
    c = conn.cursor()
    c.execute(f"""
        SELECT strategy, window, z_score_threshold, entry_timing, stop_loss,
               COALESCE(take_profit, arm_sell_pct) AS tp, max_hold_hours,
               trail_buy_pct, trail_sell_pct, {ROBUST_ALPHA_SQL} AS ralpha,
               strategy_return, trades, win_rate
        FROM backtest_cache
        WHERE ticker=? AND version=? AND trades>0
        ORDER BY ralpha DESC LIMIT 1
    """, (ticker, version))
    return c.fetchone()


def worst_neighbor(conn, ticker, version, strategy, window, z, entry_timing,
                    sl, tp, hold, tb, ts):
    """Worst robust_alpha among the CLIFF_RADIUS-step neighborhood on
    take_profit/stop_loss (index-based nearest-distinct-values, same
    convention as prune_backtest_cache.py), holding every other axis fixed."""
    c = conn.cursor()

    def nearest_values(col_sql, center):
        c.execute(f"""
            SELECT DISTINCT {col_sql} FROM backtest_cache
            WHERE ticker=? AND version=? AND strategy=? AND window=? AND z_score_threshold=?
                  AND entry_timing=? AND trail_buy_pct=? AND trail_sell_pct=?
        """, (ticker, version, strategy, window, z, entry_timing, tb, ts))
        vals = sorted(set(r[0] for r in c.fetchall() if r[0] is not None))
        if center not in vals:
            return {center}
        idx = vals.index(center)
        return set(vals[max(0, idx - CLIFF_RADIUS):min(len(vals), idx + CLIFF_RADIUS + 1)])

    tp_keep = list(nearest_values("COALESCE(take_profit, arm_sell_pct)", tp))
    sl_keep = list(nearest_values("stop_loss", sl))
    tp_ph = ",".join("?" * len(tp_keep))
    sl_ph = ",".join("?" * len(sl_keep))
    c.execute(f"""
        SELECT MIN({ROBUST_ALPHA_SQL}) FROM backtest_cache
        WHERE ticker=? AND version=? AND strategy=? AND window=? AND z_score_threshold=?
              AND entry_timing=? AND trail_buy_pct=? AND trail_sell_pct=?
              AND COALESCE(take_profit, arm_sell_pct) IN ({tp_ph})
              AND stop_loss IN ({sl_ph})
              AND ABS(max_hold_hours - ?) <= 24 AND trades > 0
    """, [ticker, version, strategy, window, z, entry_timing, tb, ts] + tp_keep + sl_keep + [hold])
    return c.fetchone()[0]


def compounded(rets):
    prod = 1.0
    for r in rets:
        prod *= (1 + r)
    return (prod - 1) * 100


def overlay_summary(conn, ticker, mechanism):
    c = conn.cursor()
    c.execute("""
        SELECT cor.ret FROM candidate_overlay_results cor
        JOIN candidate_nodes cn ON cn.id = cor.candidate_node_id
        WHERE cn.ticker=? AND cor.mechanism=?
    """, (ticker, mechanism))
    rets = [r[0] for r in c.fetchall()]
    if not rets:
        return None
    wr = sum(1 for r in rets if r > 0) / len(rets) * 100
    return len(rets), compounded(rets), wr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--version", default="v5")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    tickers = args.tickers
    if not tickers:
        c = conn.cursor()
        c.execute("SELECT DISTINCT ticker FROM candidate_nodes")
        tickers = [r[0] for r in c.fetchall()]

    rows = []
    for ticker in tickers:
        node = best_node(conn, ticker, args.version)
        if node is None:
            rows.append((ticker, None))
            continue
        (strategy, window, z, entry_timing, sl, tp, hold, tb, ts,
         ralpha, sret, trades, wr) = node
        wn = worst_neighbor(conn, ticker, args.version, strategy, window, z,
                             entry_timing, sl, tp, hold, tb, ts)
        addon = overlay_summary(conn, ticker, "addon")
        drought = overlay_summary(conn, ticker, "drought")
        cliff = "CLIFF" if (wn is not None and wn < 0) else ("SAFE" if wn is not None else "?")
        # NAIVE multiplicative combination -- (1 + core) * (1 + overlay) - 1.
        # This is an APPROXIMATION, not real stacked-model math: drought fills
        # core's own time gaps sequentially (so multiplying compounded
        # multipliers is roughly defensible), but add-on runs CONCURRENTLY
        # with an open core position (parallel capital, not sequential), so
        # multiplying it against core here overstates/misrepresents the real
        # combined effect -- v5_stacked_backtest.py's proper parallel-return
        # model is the rigorous version of this, not this quick estimate.
        core_mult = 1 + sret / 100
        addon_mult = (core_mult * (1 + addon[1] / 100) - 1) * 100 if addon else None
        drought_mult = (core_mult * (1 + drought[1] / 100) - 1) * 100 if drought else None
        rows.append((ticker, ralpha, sret, wn, cliff, addon, drought, addon_mult, drought_mult))

    conn.close()

    hdr = "%-8s %9s %9s %9s %6s %20s %20s %12s %12s" % (
        "Ticker", "CoreA%", "AbsRet%", "WorstNb%", "Status", "Addon(n,comp%,WR%)",
        "Drought(n,comp%,WR%)", "x Addon%", "x Drought%")
    print(hdr)
    print("(x columns are a NAIVE multiplicative estimate, not real stacked-model math -- see docstring)")
    for row in sorted(rows, key=lambda r: -(r[1] if r[1] is not None else -1e9)):
        if row[1] is None:
            print(f"{row[0]:8} NO_DATA")
            continue
        ticker, ralpha, sret, wn, cliff, addon, drought, addon_mult, drought_mult = row
        wn_str = f"{wn:>9.1f}" if wn is not None else "      n/a"
        ao_str = f"{addon[0]},{addon[1]:+.2f}%,{addon[2]:.0f}%" if addon else "-"
        dr_str = f"{drought[0]},{drought[1]:+.2f}%,{drought[2]:.0f}%" if drought else "-"
        am_str = f"{addon_mult:+.1f}" if addon_mult is not None else "-"
        dm_str = f"{drought_mult:+.1f}" if drought_mult is not None else "-"
        print(f"{ticker:8} {ralpha:>9.1f} {sret:>9.1f} {wn_str} {cliff:>6} {ao_str:>20} {dr_str:>20} {am_str:>12} {dm_str:>12}")


if __name__ == "__main__":
    main()
