"""Consolidated liquidity-screen candidate summary -- core alpha, ANNUALIZED
excess return (CAGR-based, fair across tickers with different cached-history
lengths), 5-min real-fill accuracy, worst neighbor (cliff-safety, computed
for the ACTUAL winning node regardless of whether it passes -- unlike
top_safe_nodes.py, which only reports a node if it clears the safety bar),
liquidity, and drought/add-on overlay results, all in one table. Built
2026-08-08 after repeatedly rebuilding pieces of this table by hand in
conversation -- consolidates locate_best_node.py's winner pick,
top_safe_nodes.py's neighbor-check logic, annualized_alpha_report.py's CAGR
calc, verify_fill_resolution_accuracy.py's 5-min replay, and
candidate_overlay_results into one script. The canonical "everything at a
glance" candidate report -- keep adding to this rather than spinning up
another standalone comparison script, per the user's explicit call
2026-08-08 (later).

Usage:
  .venv/bin/python scripts/candidate_summary_report.py TNA URTY SQQQ ...
  .venv/bin/python scripts/candidate_summary_report.py --all-swept
  .venv/bin/python scripts/candidate_summary_report.py TNA --skip-5min  # faster, skip yfinance calls
"""
import argparse
import csv
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from top_safe_nodes import CLIFF_RADIUS
from annualized_alpha_report import calendar_days, cagr
from verify_fill_resolution_accuracy import fill_accuracy_for_node

DB_PATH = "cache/research/trading_universe.db"
ROBUST_ALPHA_SQL = ("MIN(alpha_vs_spy, COALESCE(alpha_vs_spy_pessimistic, alpha_vs_spy), "
                     "COALESCE(alpha_vs_spy_certain, alpha_vs_spy))")


def best_node(conn, ticker, version="v5"):
    c = conn.cursor()
    c.execute(f"""
        SELECT strategy, window, z_score_threshold, entry_timing, stop_loss,
               COALESCE(take_profit, arm_sell_pct) AS tp, max_hold_hours,
               trail_buy_pct, trail_sell_pct, {ROBUST_ALPHA_SQL} AS ralpha,
               strategy_return, trades, win_rate, spy_bh
        FROM backtest_cache
        WHERE ticker=? AND version=? AND trades>0
        ORDER BY ralpha DESC LIMIT 1
    """, (ticker, version))
    return c.fetchone()


def annualized_excess(ticker, strategy_return, spy_bh_full_window):
    """CAGR-based excess return over the ticker's full cached-data calendar
    span -- see annualized_alpha_report.py's docstring for why raw alpha_vs_spy
    isn't cross-ticker comparable (different tickers have very different
    amounts of cached history) and why this annualizes over the full window
    rather than invested-only time (user's explicit call, 2026-08-08)."""
    days = calendar_days(ticker)
    strat_cagr = cagr(strategy_return, days)
    spy_cagr = cagr(spy_bh_full_window, days)
    if strat_cagr is None or spy_cagr is None:
        return days, None
    return days, strat_cagr - spy_cagr


def fill_accuracy_summary(ticker, strategy, window, z, trail_buy_pct, hold):
    """(possible_win_rate_pct, possible_mean_abs_err_pct, n) from the 5-min
    real-fill replay, or None if this node's entry mechanism has no bounce-
    fill resolution to check (TrailingExitZScoreBreakout's market-buy entry,
    or a TrailingBoth row with trail_buy_pct=0)."""
    if strategy != "TrailingBothZScoreBreakout" or not trail_buy_pct:
        return None
    df = fill_accuracy_for_node(ticker, window, z, trail_buy_pct, hold)
    if df.empty:
        return None
    diff_cols = ['possible_diff_pct', 'pessimistic_diff_pct', 'certain_diff_pct']
    abs_diffs = df[diff_cols].abs()
    closest = abs_diffs.idxmin(axis=1)
    win_rate = (closest == 'possible_diff_pct').mean() * 100
    mean_abs_err = df['possible_diff_pct'].dropna().abs().mean()
    return win_rate, mean_abs_err, len(df)


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


def _write_csv(name, rows):
    out_path = Path("output") / (name if name.endswith(".csv") else f"{name}.csv")
    out_path.parent.mkdir(exist_ok=True)
    fieldnames = ["ticker", "core_alpha_pct", "abs_return_pct", "years", "trades",
                  "ann_excess_pct", "fillacc_possible_win_pct", "fillacc_possible_mean_err_pct",
                  "fillacc_n", "worst_neighbor_pct", "status",
                  "addon_n", "addon_compounded_pct", "addon_win_rate_pct",
                  "drought_n", "drought_compounded_pct", "drought_win_rate_pct",
                  "x_addon_pct", "x_drought_pct"]
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            if row[1] is None:
                w.writerow({"ticker": row[0]})
                continue
            (ticker, ralpha, sret, years, trades, ann_excess, fill_acc, wn, cliff,
             addon, drought, addon_mult, drought_mult) = row
            w.writerow({
                "ticker": ticker, "core_alpha_pct": ralpha, "abs_return_pct": sret,
                "years": years, "trades": trades, "ann_excess_pct": ann_excess,
                "fillacc_possible_win_pct": fill_acc[0] if fill_acc else None,
                "fillacc_possible_mean_err_pct": fill_acc[1] if fill_acc else None,
                "fillacc_n": fill_acc[2] if fill_acc else None,
                "worst_neighbor_pct": wn, "status": cliff,
                "addon_n": addon[0] if addon else None,
                "addon_compounded_pct": addon[1] if addon else None,
                "addon_win_rate_pct": addon[2] if addon else None,
                "drought_n": drought[0] if drought else None,
                "drought_compounded_pct": drought[1] if drought else None,
                "drought_win_rate_pct": drought[2] if drought else None,
                "x_addon_pct": addon_mult, "x_drought_pct": drought_mult,
            })
    print(f"Wrote {out_path} ({len(rows)} rows)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--version", default="v5")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--skip-5min", action="store_true",
                     help="skip the 5-min fill-accuracy replay (saves a yfinance call per ticker)")
    ap.add_argument("--csv", default=None, help="write output/<name>.csv instead of the wide terminal table")
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
         ralpha, sret, trades, wr, spy_bh) = node
        wn = worst_neighbor(conn, ticker, args.version, strategy, window, z,
                             entry_timing, sl, tp, hold, tb, ts)
        addon = overlay_summary(conn, ticker, "addon")
        drought = overlay_summary(conn, ticker, "drought")
        cliff = "CLIFF" if (wn is not None and wn < 0) else ("SAFE" if wn is not None else "?")
        days, ann_excess = annualized_excess(ticker, sret, spy_bh)
        years = round(days / 365.25, 2) if days else None
        fill_acc = None if args.skip_5min else fill_accuracy_summary(ticker, strategy, window, z, tb, hold)
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
        rows.append((ticker, ralpha, sret, years, trades, ann_excess, fill_acc, wn, cliff,
                     addon, drought, addon_mult, drought_mult))

    conn.close()

    if args.csv:
        _write_csv(args.csv, rows)
        return

    hdr = "%-8s %9s %9s %6s %6s %10s %14s %9s %6s %20s %20s %12s %12s" % (
        "Ticker", "CoreA%", "AbsRet%", "Years", "Trades", "AnnExcess%", "FillAcc(win%,err%)",
        "WorstNb%", "Status", "Addon(n,comp%,WR%)", "Drought(n,comp%,WR%)", "x Addon%", "x Drought%")
    print(hdr)
    print("(x columns are a NAIVE multiplicative estimate, not real stacked-model math -- see docstring)")
    print("(AnnExcess% = CAGR-based excess over SPY, comparable across tickers with different cached-history "
          "lengths -- see annualized_alpha_report.py. Years/Trades sit right next to it: a big AnnExcess% "
          "backed by a short history / few trades is weaker evidence than the same number over a long one.)")
    for row in sorted(rows, key=lambda r: -(r[1] if r[1] is not None else -1e9)):
        if row[1] is None:
            print(f"{row[0]:8} NO_DATA")
            continue
        (ticker, ralpha, sret, years, trades, ann_excess, fill_acc, wn, cliff,
         addon, drought, addon_mult, drought_mult) = row
        wn_str = f"{wn:>9.1f}" if wn is not None else "      n/a"
        ae_str = f"{ann_excess:+.1f}" if ann_excess is not None else "-"
        fa_str = f"{fill_acc[0]:.0f}%,{fill_acc[1]:.2f}%,n={fill_acc[2]}" if fill_acc else "-"
        ao_str = f"{addon[0]},{addon[1]:+.2f}%,{addon[2]:.0f}%" if addon else "-"
        dr_str = f"{drought[0]},{drought[1]:+.2f}%,{drought[2]:.0f}%" if drought else "-"
        am_str = f"{addon_mult:+.1f}" if addon_mult is not None else "-"
        dm_str = f"{drought_mult:+.1f}" if drought_mult is not None else "-"
        print(f"{ticker:8} {ralpha:>9.1f} {sret:>9.1f} {years!s:>6} {trades:>6} {ae_str:>10} {fa_str:>14} "
              f"{wn_str} {cliff:>6} {ao_str:>20} {dr_str:>20} {am_str:>12} {dm_str:>12}")


if __name__ == "__main__":
    main()
