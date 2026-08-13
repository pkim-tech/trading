"""Standard post-campaign comparison table: per ticker, per strategy, the
best node (preferring cliff-safe, falling back to best-any) with alpha,
worst_neighbor, full node params, trades, win_rate, and liquidity -- plus a
winner call across strategies. Built after repeatedly hand-assembling this
same table piece by piece during the 2026-07-20 v5 campaign review; see
.claude/skills/backtest-change-rollout/SKILL.md Stage 4.

Usage:
    .venv/bin/python scripts/campaign_comparison_table.py --version v5 \
        --strategies TrailingBothZScoreBreakout TrailingExitZScoreBreakout \
        --fixed-sls 1 2 3 --entry-timing open_check \
        --tickers AGQ DPST DUST GDXD GDXU HIBL KORU LABU NAIL NUGT RETL SOXL TQQQ UDOW USD UVIX YANG ZSL
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import strategies as strategies_module
from drought_overlay_test import get_trades_and_bars
from calendar_year_returns import calendar_year_breakdown, format_calendar_years

DB_PATH = "cache/research/trading_universe.db"
CLIFF_RADIUS = 2
ROBUST_ALPHA_SQL = (
    "MIN(alpha_vs_spy, COALESCE(alpha_vs_spy_pessimistic, alpha_vs_spy), "
    "COALESCE(alpha_vs_spy_certain, alpha_vs_spy))"
)


def sl_real_col(sl_axis_col):
    return "trail_sell_pct" if sl_axis_col == "trail_pct" else sl_axis_col


def best_node(con, version, ticker, strategy, fixed_sl, entry_timing):
    sl_axis_col, fourth_axis_col = strategies_module.resolve_axis_columns(strategy)
    real_col = sl_real_col(sl_axis_col)
    row = con.execute(f"""
        SELECT axis_tp, {real_col} AS slc, max_hold_hours, window, z_score_threshold,
               {ROBUST_ALPHA_SQL} AS robust_alpha, trades, win_rate,
               {'trail_sell_pct' if fourth_axis_col == 'trail_pct' else '0'} AS tpct
        FROM backtest_cache
        WHERE version=? AND ticker=? AND strategy=? AND trades>0 AND stop_loss=? AND entry_timing=?
        ORDER BY robust_alpha DESC LIMIT 1
    """, (version, ticker, strategy, fixed_sl, entry_timing)).fetchone()
    if not row:
        return None
    tp_c, sl_c, hold_c, win_c, z_c, best_alpha, trades, winrate, tpct_c = (
        int(row[0]), float(row[1]), int(row[2]), int(row[3]), float(row[4]),
        float(row[5]), int(row[6]), float(row[7]), float(row[8]))
    tpct_filter, tpct_params = "", []
    if fourth_axis_col == "trail_pct":
        tpct_filter = "AND trail_sell_pct BETWEEN ? AND ?"
        tpct_params = [tpct_c - 1, tpct_c + 1]
    worst = con.execute(f"""
        SELECT MIN({ROBUST_ALPHA_SQL}) FROM backtest_cache
        WHERE version=? AND ticker=? AND strategy=?
          AND window=? AND z_score_threshold=?
          AND axis_tp BETWEEN ? AND ?
          AND {real_col} BETWEEN ? AND ?
          AND max_hold_hours BETWEEN ? AND ?
          AND stop_loss=? AND entry_timing=? {tpct_filter}
          AND trades>0
    """, (version, ticker, strategy, win_c, z_c, tp_c - CLIFF_RADIUS, tp_c + CLIFF_RADIUS,
          sl_c - CLIFF_RADIUS, sl_c + CLIFF_RADIUS, hold_c - 7, hold_c + 7,
          fixed_sl, entry_timing, *tpct_params)).fetchone()[0]
    worst_neighbor = float(worst) if worst is not None else 0.0
    return {
        "fixed_sl": fixed_sl, "tp": tp_c, "axis": sl_c, "hold": hold_c, "window": win_c,
        "z": z_c, "best": best_alpha, "worst": worst_neighbor, "safe": worst_neighbor >= 0,
        "trades": trades, "winrate": winrate,
        # tpct (2026-08-13): already computed above for the cliff-check query's
        # trail_pct filter, just not previously exposed -- needed to reconstruct
        # a full node (trail_sell_pct for TrailingBoth) for --show-calendar-years.
        "tpct": tpct_c,
    }


def collect(con, version, ticker, strategy, fixed_sls, entry_timing):
    nodes = [best_node(con, version, ticker, strategy, fsl, entry_timing) for fsl in fixed_sls]
    return [n for n in nodes if n]


def safe_best(nodes):
    safe = [n for n in nodes if n["safe"]]
    return max(safe, key=lambda n: n["best"]) if safe else None


def best_any(nodes):
    return max(nodes, key=lambda n: n["best"]) if nodes else None


def fmt_node(n):
    return f"sl{n['fixed_sl']:.0f} tp{n['tp']} ax{n['axis']:.1f} h{n['hold']} w{n['window']} z{n['z']}"


_KNOWN_ABBREVIATIONS = {
    "TrailingBothZScoreBreakout": "TB",
    "TrailingExitZScoreBreakout": "TE",
    "TrailingBuyZScoreBreakout": "TBu",
    "LimitOrderTrailingExit": "LOTE",
    "LimitOrderZScoreBreakout": "LOZ",
    "LimitExitZScoreBreakout": "LEZ",
}


def _gt_node(ticker, strategy, node, entry_timing):
    """Reconstructs a get_trades_and_bars-compatible dict from a best_node()
    result -- 'axis'/'tp'/'tpct' are strategy-normalized column names (see
    best_node()'s real_col/axis_tp resolution), not literal field names, so
    which one means trail_buy_pct vs trail_sell_pct vs arm_pct depends on the
    strategy's real sl_axis (same overloaded-column mapping best_node() itself
    already resolves via strategies_module.resolve_axis_columns -- reused here
    rather than re-guessed, since getting this wrong is exactly the recurring
    bug shape this project has hit before with trail_buy_pct/take_profit)."""
    sl_axis, _ = strategies_module.resolve_axis_columns(strategy)
    if sl_axis == "trail_buy_pct":  # TrailingBoth: axis=trail_buy_pct, tpct=trail_sell_pct
        trail_buy_pct, trail_sell_pct = node["axis"], node["tpct"]
    else:  # TrailingExit (sl_axis == 'trail_pct'): axis IS trail_sell_pct, no trail_buy_pct
        trail_buy_pct, trail_sell_pct = 0.0, node["axis"]
    return {
        "ticker": ticker, "strategy": strategy, "window": node["window"], "z": node["z"],
        "fixed_sl": node["fixed_sl"], "arm_pct": node["tp"],
        "trail_buy_pct": trail_buy_pct, "trail_sell_pct": trail_sell_pct,
        "max_hold_hours": node["hold"], "entry_timing": entry_timing,
    }


def short_label(strategy_name):
    """Distinguishing abbreviation for a strategy class name, e.g.
    'TrailingBothZScoreBreakout' -> 'TB', 'TrailingExitZScoreBreakout' -> 'TE'
    (matches the TB/TE convention used throughout this session's discussion).
    Falls back to first-letter-of-each-word for anything not in the table --
    plain [:2] collapses every Trailing* strategy to the same 'TR' prefix."""
    if strategy_name in _KNOWN_ABBREVIATIONS:
        return _KNOWN_ABBREVIATIONS[strategy_name]
    core = strategy_name.replace("ZScoreBreakout", "").replace("Trailing", "")
    return (core[:2] or strategy_name[:2]).upper()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--version", required=True)
    p.add_argument("--strategies", nargs="+", required=True)
    p.add_argument("--fixed-sls", nargs="+", type=float, required=True)
    p.add_argument("--entry-timing", default="open_check")
    p.add_argument("--tickers", nargs="+", required=True)
    p.add_argument("--min-best", type=float, default=None,
                    help="Drop tickers whose winning (safe, or best-any if none safe) alpha is below this threshold.")
    p.add_argument("--show-calendar-years", action="store_true",
                    help="compute per-calendar-year return breakdown (tax-relevant) for each ticker's "
                         "printed winner node (needs a trade replay per ticker, opt-in)")
    args = p.parse_args()

    if len(args.strategies) != 2:
        print("This script's table format assumes exactly 2 strategies to compare "
              "side by side. Pass --strategies A B.", file=sys.stderr)
        sys.exit(1)
    strat_a, strat_b = args.strategies

    con = sqlite3.connect(DB_PATH)
    liq = dict(con.execute(
        f"SELECT symbol, avg_vol_10d*last_price*0.01 FROM tickers WHERE symbol IN "
        f"({','.join('?' * len(args.tickers))})", args.tickers))

    rows = {}
    for strategy in (strat_a, strat_b):
        for t in args.tickers:
            rows[(strategy, t)] = collect(con, args.version, t, strategy, args.fixed_sls, args.entry_timing)

    header = (f"| Ticker | Winner | {short_label(strat_a)} best | {short_label(strat_a)} wnbr | "
              f"{short_label(strat_a)} node | trades | wr | "
              f"{short_label(strat_b)} best | {short_label(strat_b)} wnbr | "
              f"{short_label(strat_b)} node | trades | wr | Liquidity |")
    sep = "|---" * 13 + "|"
    print(header)
    print(sep)

    for t in args.tickers:
        a_nodes, b_nodes = rows[(strat_a, t)], rows[(strat_b, t)]
        a_safe, b_safe = safe_best(a_nodes), safe_best(b_nodes)
        a_show = a_safe or best_any(a_nodes)
        b_show = b_safe or best_any(b_nodes)
        if a_safe and b_safe:
            winner = short_label(strat_a) if a_safe["best"] > b_safe["best"] else short_label(strat_b)
        elif b_safe:
            winner = f"{short_label(strat_b)} only"
        elif a_safe:
            winner = f"{short_label(strat_a)} only"
        else:
            winner = "neither"

        def cell(n):
            if n is None:
                return ("-", "-", "-", "-", "-")
            return (f"{n['best']:.1f}", f"{n['worst']:.1f}", fmt_node(n), str(n["trades"]), f"{n['winrate']:.1f}%")

        if args.min_best is not None:
            # Filter on the actual winner's alpha, not whichever unsafe best-any
            # happens to be numerically larger -- an unsafe node's inflated
            # "best" shouldn't rescue a row whose real (safe) winner is weak.
            if a_safe and b_safe:
                winning_best = max(a_safe["best"], b_safe["best"])
            elif b_safe:
                winning_best = b_safe["best"]
            elif a_safe:
                winning_best = a_safe["best"]
            else:
                winning_best = max((n["best"] for n in (a_show, b_show) if n is not None), default=None)
            if winning_best is None or winning_best < args.min_best:
                continue

        a_best, a_wnbr, a_node, a_trd, a_wr = cell(a_show)
        b_best, b_wnbr, b_node, b_trd, b_wr = cell(b_show)
        liq_str = f"${liq.get(t, 0):,.0f}"
        print(f"| {t} | {winner} | {a_best} | {a_wnbr} | {a_node} | {a_trd} | {a_wr} | "
              f"{b_best} | {b_wnbr} | {b_node} | {b_trd} | {b_wr} | {liq_str} |")

        if args.show_calendar_years:
            for strategy, shown in ((strat_a, a_show), (strat_b, b_show)):
                if shown is None:
                    continue
                try:
                    gt_node = _gt_node(t, strategy, shown, args.entry_timing)
                    trades, df_h = get_trades_and_bars(gt_node)
                    cy_str = format_calendar_years(calendar_year_breakdown(trades, df_h))
                except Exception as e:
                    cy_str = f"(failed: {e})"
                print(f"    {t} {short_label(strategy)} calendar years: {cy_str}")


if __name__ == "__main__":
    main()
