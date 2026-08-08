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

# Single source of truth for what each output column means -- used for both
# the xlsx glossary sheet and (imported directly) the Streamlit Candidates
# page's glossary expander, so the two never drift apart.
COLUMN_DEFS = {
    "ticker": "The symbol.",
    "strategy": "Which strategy class the best node uses (TrailingBothZScoreBreakout = trailing-buy entry, "
                "TrailingExitZScoreBreakout = market-buy entry).",
    "core_alpha_pct": "robust_alpha for the best node: MIN(possible, pessimistic, certain) fill-resolution "
                       "alpha vs SPY, over the ticker's full cached-data window. This project's standard "
                       "selection metric -- see CLAUDE.md's kernel versioning notes.",
    "abs_return_pct": "The strategy's own raw compounded return (not vs SPY) over the same window.",
    "years": "Real calendar span of this ticker's cached hourly data (days between the earliest and latest "
             "cached bar, /365.25). Tickers vary a lot here (e.g. SOXL ~3.0y vs SPCL ~0.31y) -- this is why "
             "core_alpha_pct/abs_return_pct alone aren't fairly comparable across tickers.",
    "trades": "Number of completed trades in the best node's backtest.",
    "ann_excess_pct": "CAGR-based excess return over SPY, annualized over the full calendar window (years "
                       "column) -- fixes the cross-ticker horizon-mismatch problem above. A large value backed "
                       "by a short 'years'/low 'trades' is weak evidence (e.g. SPCL's ~3000%+ off 0.31y/3 "
                       "trades is an annualization artifact, not a real signal) -- always read this next to "
                       "years/trades, never alone. See docs/research_log.md's 2026-08-08 'annualized excess' "
                       "entries for the full derivation.",
    "fillacc_possible_win_pct": "Of this node's real trailing-buy entry signals in the last ~58 days (yfinance's "
                                 "5-min history cap), the % where the 'possible' fill resolution (the kernel's "
                                 "default, optimistic-but-unmodified bounce-fill assumption) was closest to the "
                                 "REAL 5-minute-bar fill price, vs the 'pessimistic'/'certain' alternatives. "
                                 "Blank for TrailingExitZScoreBreakout nodes (market-buy entry has no bounce-fill "
                                 "resolution to check) or if trail_buy_pct=0.",
    "fillacc_possible_mean_err_pct": "Mean absolute price error (%) of the 'possible' resolution vs the real "
                                      "5-min fill, across those same signals. Lower is better/more trustworthy. "
                                      "Same blank-condition as fillacc_possible_win_pct.",
    "fillacc_n": "How many real signals the fill-accuracy check above is actually based on -- often single "
                 "digits in a 58-day window, so treat a 100% win rate on n=1-2 with real caution.",
    "worst_neighbor_pct": "Cliff-safety check: the worst robust_alpha found among nearby take_profit/stop_loss "
                           "grid values (CLIFF_RADIUS=3 steps) around the best node's own params, holding every "
                           "other axis fixed. Negative means a small parameter nudge would have lost money -- "
                           "see docs/cliff_safety_query_checklist.md.",
    "status": "CLIFF (worst_neighbor_pct < 0, the pick is fragile to small parameter changes) or SAFE "
              "(worst_neighbor_pct >= 0). Unlike top_safe_nodes.py, this is computed for whatever node actually "
              "won on core_alpha_pct, even if that node isn't cliff-safe -- so you can see HOW unsafe it is, "
              "not just a pass/fail.",
    "addon_n": "Number of backtested add-on-leg trades found for this ticker's registered candidate node(s) "
               "(candidate_overlay_results, mechanism='addon'). Blank if none registered/run yet.",
    "addon_compounded_pct": "Compounded return of just the add-on overlay's own trades (not combined with core).",
    "addon_win_rate_pct": "% of add-on trades that were profitable.",
    "drought_n": "Number of backtested drought-overlay trades found (mechanism='drought'). Blank if none "
                 "registered/run yet.",
    "drought_compounded_pct": "Compounded return of just the drought overlay's own trades (not combined with core).",
    "drought_win_rate_pct": "% of drought trades that were profitable.",
    "x_addon_pct": "NAIVE estimate of core+add-on combined: (1+core_return)*(1+addon_return)-1. Add-on capital "
                   "runs CONCURRENTLY with an open core position (not sequentially), so this OVERSTATES the "
                   "real combined effect -- v5_stacked_backtest.py's parallel-return model is the rigorous "
                   "version. Treat this column as a rough upper bound, not a real number.",
    "x_drought_pct": "NAIVE estimate of core+drought combined, same (1+core)*(1+overlay)-1 formula. Drought "
                      "fills core's own idle-time gaps SEQUENTIALLY, so this approximation is more defensible "
                      "than x_addon_pct, but still not the rigorous stacked model.",
    "liquidity_dollars_per_day": "avg_vol_10d * last_price * 0.01 from the tickers table -- this project's "
                                  "standard real dollar-liquidity estimate (confirmed against CLAUDE.md's cited "
                                  "figures, e.g. AGQ ~$2.02M, HIBL ~$89.9K). This is supposed to be the FIRST-pass "
                                  "filter before spending validation effort on a candidate (see the 2026-08-07 "
                                  "'liquidity was never the limiting filter' finding) -- a great alpha number on "
                                  "an illiquid name is untradeable regardless of the rest of this row.",
}


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


def liquidity_dollars_per_day(conn, ticker):
    """Real dollar liquidity, this project's standard formula (see
    campaign_comparison_table.py) -- avg_vol_10d*last_price*0.01, confirmed
    against CLAUDE.md's cited figures (AGQ ~$2.02M/day, HIBL ~$89.9K/day)."""
    c = conn.cursor()
    c.execute("SELECT avg_vol_10d * last_price * 0.01 FROM tickers WHERE symbol=?", (ticker,))
    row = c.fetchone()
    return row[0] if row else None


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


def _row_to_record(row):
    """Converts one internal row tuple to a {column_name: value} dict keyed
    exactly like COLUMN_DEFS, so CSV/xlsx/terminal output and the glossary
    all agree on column names in one place."""
    ticker = row[0]
    if row[1] is None:
        return {"ticker": ticker}
    (ticker, strategy, ralpha, sret, years, trades, ann_excess, fill_acc, wn, cliff,
     addon, drought, addon_mult, drought_mult, liquidity) = row
    return {
        "ticker": ticker, "strategy": strategy, "core_alpha_pct": ralpha, "abs_return_pct": sret,
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
        "liquidity_dollars_per_day": liquidity,
    }


def _write_csv(name, rows):
    out_path = Path("output") / (name if name.endswith(".csv") else f"{name}.csv")
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(COLUMN_DEFS.keys()))
        w.writeheader()
        for row in rows:
            w.writerow(_row_to_record(row))
    print(f"Wrote {out_path} ({len(rows)} rows)")


def _write_xlsx(name, rows):
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter

    out_path = Path("output") / (name if name.endswith(".xlsx") else f"{name}.xlsx")
    out_path.parent.mkdir(exist_ok=True)

    wb = Workbook()
    data_ws = wb.active
    data_ws.title = "Candidates"

    cols = list(COLUMN_DEFS.keys())
    data_ws.append(cols)
    for cell in data_ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        rec = _row_to_record(row)
        data_ws.append([rec.get(c) for c in cols])
    data_ws.freeze_panes = "A2"
    for i, col in enumerate(cols, start=1):
        data_ws.column_dimensions[get_column_letter(i)].width = max(12, min(len(col) + 2, 28))

    def_ws = wb.create_sheet("Column Definitions")
    def_ws.append(["Column", "Definition"])
    for cell in def_ws[1]:
        cell.font = Font(bold=True)
    for col, definition in COLUMN_DEFS.items():
        def_ws.append([col, definition])
        def_ws.cell(row=def_ws.max_row, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    def_ws.column_dimensions["A"].width = 32
    def_ws.column_dimensions["B"].width = 110

    wb.save(out_path)
    print(f"Wrote {out_path} ({len(rows)} rows, 2 sheets)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--version", default="v5")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--skip-5min", action="store_true",
                     help="skip the 5-min fill-accuracy replay (saves a yfinance call per ticker)")
    ap.add_argument("--csv", default=None, help="write output/<name>.csv instead of the wide terminal table")
    ap.add_argument("--xlsx", default=None,
                     help="write output/<name>.xlsx (Candidates sheet + Column Definitions glossary sheet) "
                          "instead of the wide terminal table")
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
        liquidity = liquidity_dollars_per_day(conn, ticker)
        rows.append((ticker, strategy, ralpha, sret, years, trades, ann_excess, fill_acc, wn, cliff,
                     addon, drought, addon_mult, drought_mult, liquidity))

    conn.close()

    if args.xlsx:
        _write_xlsx(args.xlsx, rows)
        return
    if args.csv:
        _write_csv(args.csv, rows)
        return

    hdr = "%-8s %12s %9s %9s %6s %6s %10s %14s %9s %6s %20s %20s %12s %12s" % (
        "Ticker", "Liquidity$/d", "CoreA%", "AbsRet%", "Years", "Trades", "AnnExcess%", "FillAcc(win%,err%)",
        "WorstNb%", "Status", "Addon(n,comp%,WR%)", "Drought(n,comp%,WR%)", "x Addon%", "x Drought%")
    print(hdr)
    print("(x columns are a NAIVE multiplicative estimate, not real stacked-model math -- see docstring)")
    print("(AnnExcess% = CAGR-based excess over SPY, comparable across tickers with different cached-history "
          "lengths -- see annualized_alpha_report.py. Years/Trades sit right next to it: a big AnnExcess% "
          "backed by a short history / few trades is weaker evidence than the same number over a long one.)")
    for row in sorted(rows, key=lambda r: -(r[2] if r[1] is not None else -1e9)):
        rec = _row_to_record(row)
        if rec.get("core_alpha_pct") is None:
            print(f"{rec['ticker']:8} NO_DATA")
            continue
        wn_str = f"{rec['worst_neighbor_pct']:>9.1f}" if rec['worst_neighbor_pct'] is not None else "      n/a"
        ae_str = f"{rec['ann_excess_pct']:+.1f}" if rec['ann_excess_pct'] is not None else "-"
        fa_str = (f"{rec['fillacc_possible_win_pct']:.0f}%,{rec['fillacc_possible_mean_err_pct']:.2f}%,"
                  f"n={rec['fillacc_n']}") if rec['fillacc_possible_win_pct'] is not None else "-"
        ao_str = (f"{rec['addon_n']},{rec['addon_compounded_pct']:+.2f}%,{rec['addon_win_rate_pct']:.0f}%"
                  if rec['addon_n'] is not None else "-")
        dr_str = (f"{rec['drought_n']},{rec['drought_compounded_pct']:+.2f}%,{rec['drought_win_rate_pct']:.0f}%"
                  if rec['drought_n'] is not None else "-")
        am_str = f"{rec['x_addon_pct']:+.1f}" if rec['x_addon_pct'] is not None else "-"
        dm_str = f"{rec['x_drought_pct']:+.1f}" if rec['x_drought_pct'] is not None else "-"
        liq = rec['liquidity_dollars_per_day']
        liq_str = f"${liq:,.0f}" if liq is not None else "n/a"
        print(f"{rec['ticker']:8} {liq_str:>12} {rec['core_alpha_pct']:>9.1f} {rec['abs_return_pct']:>9.1f} "
              f"{rec['years']!s:>6} {rec['trades']:>6} {ae_str:>10} {fa_str:>14} "
              f"{wn_str} {rec['status']:>6} {ao_str:>20} {dr_str:>20} {am_str:>12} {dm_str:>12}")


if __name__ == "__main__":
    main()
