"""Standalone v4 SL-sweep summary export: for every (ticker, stop_loss, entry_timing)
campaign, the best island node (by robust_alpha = MIN(possible,pessimistic,certain))
and the worst robust_alpha found in its cliff-safety neighborhood box, plus the
ticker's overall best campaign for cross-campaign comparison. Same best/worst-neighbor
math as scripts/export_cliff_safety.py (the v3.x version of this report), extended
for v4's per-resolution columns and per-campaign (stop_loss, entry_timing) scoping.

Usage: .venv/bin/python scripts/export_v4_sweep_summary.py [out.csv]
"""
import sqlite3
import sys
import pandas as pd

RESEARCH_DB_PATH = "./cache/research/trading_universe.db"
LIVE_DB_PATH = "./cache/live/trading_live.db"
CLIFF_RADIUS = 2  # mirrors run_optimization_sweep.CLIFF_RADIUS

ROBUST_ALPHA_SQL = ("MIN(alpha_vs_spy, COALESCE(alpha_vs_spy_pessimistic, alpha_vs_spy), "
                     "COALESCE(alpha_vs_spy_certain, alpha_vs_spy))")


def load_v4_summary(conn):
    campaigns = conn.execute("""
        SELECT DISTINCT ticker, strategy, stop_loss, entry_timing
        FROM backtest_cache
        WHERE version='v4' AND trades > 0
    """).fetchall()

    results = []
    for ticker, strategy, stop_loss, entry_timing in campaigns:
        best = conn.execute(f"""
            SELECT window, z_score_threshold, trail_buy_pct, arm_sell_pct, trail_sell_pct,
                   max_hold_hours, trades, win_rate, win_twin_rate,
                   alpha_vs_spy, alpha_vs_spy_pessimistic, alpha_vs_spy_certain,
                   {ROBUST_ALPHA_SQL} AS robust_alpha
            FROM backtest_cache
            WHERE version='v4' AND ticker=? AND strategy=? AND stop_loss=? AND entry_timing=?
              AND trades > 0
            ORDER BY robust_alpha DESC LIMIT 1
        """, (ticker, strategy, stop_loss, entry_timing)).fetchone()
        if not best:
            continue
        (window, z, tb, arm, ts, max_hours, trades, win_rate, win_twin_rate,
         best_possible, best_pessimistic, best_certain, best_optimal) = best

        worst = conn.execute(f"""
            SELECT alpha_vs_spy, alpha_vs_spy_certain, {ROBUST_ALPHA_SQL} AS robust_alpha
            FROM backtest_cache
            WHERE version='v4' AND ticker=? AND strategy=? AND stop_loss=? AND entry_timing=?
              AND window=? AND z_score_threshold=?
              AND arm_sell_pct BETWEEN ? AND ?
              AND trail_buy_pct BETWEEN ? AND ?
              AND trail_sell_pct BETWEEN ? AND ?
              AND max_hold_hours BETWEEN ? AND ?
              AND trades > 0
            ORDER BY robust_alpha ASC LIMIT 1
        """, (ticker, strategy, stop_loss, entry_timing, window, z,
              arm - CLIFF_RADIUS, arm + CLIFF_RADIUS,
              tb - CLIFF_RADIUS, tb + CLIFF_RADIUS,
              ts - 1, ts + 1,
              max_hours - 7, max_hours + 7)).fetchone()
        worst_possible, worst_certain, worst_optimal = worst if worst else (best_possible, best_certain, best_optimal)

        results.append({
            'ticker': ticker, 'version': 'v4', 'strategy': strategy,
            'best_optimal': best_optimal, 'best_possible': best_possible, 'best_certain': best_certain,
            'worst_optimal': worst_optimal, 'worst_possible': worst_possible, 'worst_certain': worst_certain,
            'safe': worst_optimal >= 0,
            'z': float(z), 'tb': float(tb), 'arm': float(arm), 'sl': float(stop_loss),
            'ts': float(ts), 'entry_timing': entry_timing,
            'max_hours': int(max_hours), 'window': int(window),
            'trades': int(trades), 'win_rate': float(win_rate), 'win_twin_rate': float(win_twin_rate or 0),
        })

    df = pd.DataFrame(results)
    if df.empty:
        return df
    ticker_max = df.groupby('ticker')['best_optimal'].transform('max')
    df['max'] = ticker_max

    with sqlite3.connect(LIVE_DB_PATH) as lconn:
        wl = pd.read_sql("""
            SELECT wl.ticker, wl.account FROM watch_list wl
            JOIN watchlists w ON w.id = wl.watchlist_id
            WHERE w.is_active = 1
        """, lconn)
    df = df.merge(wl, on='ticker', how='left')

    cols = ['ticker', 'version', 'best_optimal', 'best_possible', 'best_certain',
            'worst_optimal', 'worst_possible', 'worst_certain', 'safe',
            'z', 'tb', 'arm', 'sl', 'ts', 'entry_timing', 'max_hours', 'window', 'strategy',
            'trades', 'win_rate', 'max', 'win_twin_rate', 'account']
    return df[cols]


if __name__ == '__main__':
    out_path = sys.argv[1] if len(sys.argv) > 1 else 'logs/v4_sweep_summary.csv'
    with sqlite3.connect(RESEARCH_DB_PATH, timeout=60) as conn:
        df = load_v4_summary(conn)
    df = df.sort_values(['ticker', 'sl', 'entry_timing'])
    df.to_csv(out_path, index=False)
    print(f"Wrote {len(df)} rows to {out_path}")
