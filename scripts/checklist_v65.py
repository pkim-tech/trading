"""Full watchlist-candidate checklist (docs/watchlist_candidate_checklist.md,
checks 1/4/8/9/10/11/12/13) run against watchlist 65's actual 10 nodes, both
strategies, real live params straight from watch_list -- not a re-derived
"best v4 node" like the older per-check scripts (candidate_checklist_report.py/
walk_forward_check.py/v4_max_drawdown.py), which are TB-only and v4-specific.
Checks 2/3/6/7 are run separately (existing scripts already default to the
active watchlist / all cached tickers, no changes needed):
    .venv/bin/python scripts/verify_trailing_buy_resolution.py   # check 2, TB only
    .venv/bin/python scripts/verify_trailing_sell_resolution.py  # check 3, TB only
    .venv/bin/python scripts/check_stock_splits.py               # check 6, all tickers
Check 7 (fill-logic optimism) is folded in below for the TB tickers via
export_trades.simulate_trail_both_ohlc_aware. Check 5 (open positions) is n/a
-- no watchlist-65 node has ever held a real or paper position.
Check 9/10 (same-day-block sensitivity) only apply to TB -- backtester._simulate_trail
(TE's kernel) has no same_day_block parameter at all.

Usage: .venv/bin/python scripts/checklist_v65.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
import pandas as pd

import strategies
from backtester import run_backtest_v110, run_backtest_v18, prep_inputs
from run_optimization_sweep import _summarize_trades
from train_test_split_check import period_spy_bh
from v4_max_drawdown import max_drawdown
from export_trades import simulate_trail_both_ohlc_aware
from backtester import WIN, LOSS, TWIN, TLOSS

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "cache" / "research"
LIVE_DIR = REPO_ROOT / "cache" / "live"
WATCHLIST_ID = 65
FOLDS = 5
CLOSED = ["WIN", "LOSS", "TWIN", "TLOSS"]


def get_nodes():
    conn = sqlite3.connect(LIVE_DIR / "trading_live.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ticker, strategy, window, z_score_threshold, trail_buy_pct, arm_sell_pct, "
        "take_profit, trail_sell_pct, fixed_sl, max_hold_hours, entry_timing "
        "FROM watch_list WHERE watchlist_id=? ORDER BY ticker", (WATCHLIST_ID,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def load(ticker):
    df_hourly = pd.read_csv(CACHE_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    df_hourly.index = pd.to_datetime(df_hourly.index).tz_localize(None)
    df_hourly = df_hourly.sort_index()
    close_col = 'Adj Close' if 'Adj Close' in df_hourly.columns else 'Close'
    df_daily = df_hourly.resample('D').last().dropna(subset=[close_col])
    return df_hourly, df_daily


def replay(node, df_hourly, df_daily_ind, same_day_block=False, return_bounds=False):
    is_tb = node["strategy"] == "TrailingBothZScoreBreakout"
    take_profit = (node["arm_sell_pct"] if is_tb else node["take_profit"]) / 100
    stop_loss = node["fixed_sl"] / 100
    trail_pct = node["trail_sell_pct"] / 100
    if is_tb:
        return run_backtest_v110(
            df_hourly, df_daily_ind, node["ticker"], take_profit=take_profit, stop_loss=stop_loss,
            max_hours_to_hold=node["max_hold_hours"], z_score_threshold=node["z_score_threshold"],
            trail_buy_pct=node["trail_buy_pct"] / 100, trail_pct=trail_pct,
            entry_timing=node["entry_timing"], same_day_block=same_day_block, return_bounds=return_bounds)
    return run_backtest_v18(
        df_hourly, df_daily_ind, node["ticker"], take_profit=take_profit, stop_loss=stop_loss,
        max_hours_to_hold=node["max_hold_hours"], z_score_threshold=node["z_score_threshold"],
        trail_pct=trail_pct, entry_timing=node["entry_timing"])


def robust_alpha_of(closed, spy_bh, closed_p=None, closed_c=None):
    alpha, n, *_ = _summarize_trades(closed, spy_bh) if closed else (None, 0, None, None, None)
    if alpha is None:
        return None, 0
    if closed_p is not None and closed_c is not None and closed_p and closed_c:
        alpha_p, _, *_ = _summarize_trades(closed_p, spy_bh)
        alpha_c, _, *_ = _summarize_trades(closed_c, spy_bh)
        return min(alpha, alpha_p, alpha_c), n
    return alpha, n


def check1_macro(df_daily):
    close = df_daily['Close'] if 'Close' in df_daily.columns else df_daily['Adj Close']
    r30 = (close.iloc[-1] / close.iloc[-21] - 1) * 100 if len(close) > 21 else None
    r90 = (close.iloc[-1] / close.iloc[-63] - 1) * 100 if len(close) > 63 else None
    return r30, r90


def check4_stability(closed):
    if len(closed) < 10:
        return None, None
    df = pd.DataFrame(closed).sort_values("Entry Time")
    n = len(df); cut = int(n * 0.7)
    early, late = df.iloc[:cut], df.iloc[cut:]
    early_wr = early["Result"].isin(["WIN", "TWIN"]).mean() * 100
    late_wr = late["Result"].isin(["WIN", "TWIN"]).mean() * 100
    return early_wr, late_wr


def check8_fluke(closed):
    if not closed:
        return None
    df = pd.DataFrame(closed)
    compounded_total = float(((df["Return"] + 1).prod() - 1) * 100)
    best_i = df["Return"].idxmax()
    without_best = df.drop(best_i)
    compounded_wo = float(((without_best["Return"] + 1).prod() - 1) * 100) if len(without_best) else 0.0
    return len(df), compounded_total, compounded_wo


def check13_walk_forward(closed, closed_p, closed_c, dates_min, dates_max, is_tb):
    if len(closed) < FOLDS:
        return []
    span = dates_max - dates_min
    edges = [dates_min + span * i / FOLDS for i in range(FOLDS + 1)]
    rows = []
    for i in range(FOLDS):
        start, end = edges[i], edges[i + 1]
        spy_bh = period_spy_bh(start, end)
        df = pd.DataFrame(closed)
        sub = df[(df["Entry Time"] >= start) & (df["Entry Time"] < end)]
        if sub.empty:
            rows.append(dict(fold=i + 1, n=0, alpha=None))
            continue
        sub_p = sub_c = None
        if is_tb:
            dfp, dfc = pd.DataFrame(closed_p), pd.DataFrame(closed_c)
            sub_p = dfp[(dfp["Entry Time"] >= start) & (dfp["Entry Time"] < end)].to_dict("records")
            sub_c = dfc[(dfc["Entry Time"] >= start) & (dfc["Entry Time"] < end)].to_dict("records")
        alpha, n = robust_alpha_of(sub.to_dict("records"), spy_bh, sub_p, sub_c)
        rows.append(dict(fold=i + 1, n=n, alpha=alpha))
    return rows


def main():
    nodes = get_nodes()
    all_rows = []
    for node in nodes:
        ticker = node["ticker"]
        is_tb = node["strategy"] == "TrailingBothZScoreBreakout"
        try:
            df_hourly, df_daily = load(ticker)
        except FileNotFoundError:
            print(f"[skip] {ticker}: no cached data")
            continue
        strat_cls = strategies.TrailingBothZScoreBreakout if is_tb else strategies.TrailingExitZScoreBreakout
        strat = strat_cls(window=node["window"], z_score_threshold=node["z_score_threshold"])
        df_daily_ind = strat.generate_daily_indicators(df_daily)

        # --- possible/pessimistic/certain (TB) or single-resolution (TE) ---
        if is_tb:
            trades, trades_p, trades_c = replay(node, df_hourly, df_daily_ind, return_bounds=True)
        else:
            trades = replay(node, df_hourly, df_daily_ind)
            trades_p = trades_c = None
        closed = [t for t in trades if t["Result"] in CLOSED]
        closed_p = [t for t in trades_p if t["Result"] in CLOSED] if trades_p else None
        closed_c = [t for t in trades_c if t["Result"] in CLOSED] if trades_c else None
        if not closed:
            print(f"[skip] {ticker}: no closed trades")
            continue

        entry_min, entry_max = min(t["Entry Time"] for t in closed), max(t["Exit Time"] for t in closed)
        spy_bh_full = period_spy_bh(entry_min, entry_max)
        robust_alpha, n_trades = robust_alpha_of(closed, spy_bh_full, closed_p, closed_c)

        r30, r90 = check1_macro(df_daily)
        early_wr, late_wr = check4_stability(closed)
        n8, comp_total, comp_wo_best = check8_fluke(closed)
        dd_pct, dd_start, dd_end = max_drawdown(trades)

        # check 12: current drawdown (equity at the very last closed trade vs running peak)
        equity, peak, cur_dd = 100.0, 100.0, 0.0
        for t in closed:
            equity *= (1.0 + t["Return"])
            peak = max(peak, equity)
            cur_dd = (equity - peak) / peak * 100.0

        # check 9/10: same-day-block sensitivity (TB only)
        sdb9, sdb10 = None, None
        if is_tb:
            trades_sdb, trades_sdb_p, trades_sdb_c = replay(node, df_hourly, df_daily_ind,
                                                              same_day_block=True, return_bounds=True)
            closed_sdb = [t for t in trades_sdb if t["Result"] in CLOSED]
            closed_sdb_p = [t for t in trades_sdb_p if t["Result"] in CLOSED]
            closed_sdb_c = [t for t in trades_sdb_c if t["Result"] in CLOSED]
            sdb_alpha, sdb_n = robust_alpha_of(closed_sdb, spy_bh_full, closed_sdb_p, closed_sdb_c) if closed_sdb else (None, 0)
            trade_retention = (sdb_n / n_trades * 100) if n_trades else None
            alpha_retention = (sdb_alpha / robust_alpha * 100) if (sdb_alpha is not None and robust_alpha not in (None, 0)) else None
            sdb9 = dict(trade_retention_pct=trade_retention, alpha_retention_pct=alpha_retention)

            # check 10: 70/30 split of the same-day-block retention itself
            df_all = pd.DataFrame(closed).sort_values("Entry Time")
            n = len(df_all); cut = int(n * 0.7)
            cut_time = df_all.iloc[cut]["Entry Time"] if cut < n else df_all.iloc[-1]["Entry Time"]
            df_sdb = pd.DataFrame(closed_sdb) if closed_sdb else pd.DataFrame(columns=df_all.columns)
            half_retentions = {}
            for label, before in (("early", True), ("late", False)):
                sub_all = df_all[df_all["Entry Time"] < cut_time] if before else df_all[df_all["Entry Time"] >= cut_time]
                sub_sdb = df_sdb[df_sdb["Entry Time"] < cut_time] if before else df_sdb[df_sdb["Entry Time"] >= cut_time]
                half_retentions[label] = (len(sub_sdb) / len(sub_all) * 100) if len(sub_all) else None
            sdb10 = half_retentions

        # check 7 (TB only): fill-logic optimism via OHLC-aware entry re-simulation
        fill_optimism_pct = None
        certain_frac = None
        if is_tb:
            opens = df_hourly["Open"].to_numpy(dtype=float)
            p = prep_inputs(df_hourly, df_daily_ind)
            ohlc_trades = simulate_trail_both_ohlc_aware(
                p, opens, take_profit=(node["arm_sell_pct"] / 100), stop_loss=node["fixed_sl"] / 100,
                max_hours_to_hold=node["max_hold_hours"], trail_buy_pct=node["trail_buy_pct"] / 100,
                trail_pct=node["trail_sell_pct"] / 100, target_h0=9, target_h1=14,
                z_thresh=node["z_score_threshold"])
            ohlc_closed = [t for t in ohlc_trades if t["result"] in (WIN, LOSS, TWIN, TLOSS)]
            if ohlc_closed:
                comp_ohlc = 1.0
                for t in ohlc_closed:
                    comp_ohlc *= (1.0 + t["ret"])
                comp_ohlc = (comp_ohlc - 1.0) * 100.0
                fill_optimism_pct = comp_total - comp_ohlc
                certain_frac = sum(1 for t in ohlc_closed if t.get("entry_certain", True)) / len(ohlc_closed) * 100

        wf = check13_walk_forward(closed, closed_p, closed_c, entry_min, entry_max, is_tb)
        neg_folds = sum(1 for f in wf if f["alpha"] is not None and f["alpha"] < 0)

        all_rows.append(dict(
            ticker=ticker, strategy=node["strategy"],
            n_trades=n_trades, robust_alpha_pct=round(robust_alpha, 1) if robust_alpha is not None else None,
            c1_r30_pct=round(r30, 1) if r30 is not None else None,
            c1_r90_pct=round(r90, 1) if r90 is not None else None,
            c4_early_wr=round(early_wr, 1) if early_wr is not None else None,
            c4_late_wr=round(late_wr, 1) if late_wr is not None else None,
            c7_fill_optimism_pct=round(fill_optimism_pct, 1) if fill_optimism_pct is not None else None,
            c7_certain_frac_pct=round(certain_frac, 1) if certain_frac is not None else None,
            c8_compounded_pct=round(comp_total, 1),
            c8_compounded_wo_best_pct=round(comp_wo_best, 1),
            c8_best_trade_share_pct=round(comp_total - comp_wo_best, 1),
            c9_trade_retention_pct=round(sdb9["trade_retention_pct"], 1) if sdb9 and sdb9["trade_retention_pct"] is not None else None,
            c9_alpha_retention_pct=round(sdb9["alpha_retention_pct"], 1) if sdb9 and sdb9["alpha_retention_pct"] is not None else None,
            c10_early_retention_pct=round(sdb10["early"], 1) if sdb10 and sdb10["early"] is not None else None,
            c10_late_retention_pct=round(sdb10["late"], 1) if sdb10 and sdb10["late"] is not None else None,
            c11_max_drawdown_pct=round(dd_pct, 1),
            c12_current_drawdown_pct=round(cur_dd, 1),
            c13_neg_folds=f"{neg_folds}/{len(wf)}",
            c13_fold_alphas=[round(f["alpha"], 0) if f["alpha"] is not None else None for f in wf],
        ))
        print(f"{ticker} done")

    df = pd.DataFrame(all_rows)
    out_path = REPO_ROOT / "output" / "checklist_v65_summary.csv"
    df.to_csv(out_path, index=False)
    print(f"\nWrote {out_path}\n")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
