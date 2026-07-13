"""Standalone export of pages/0_Top_Pivot.py's Cliff Safety (best alpha vs worst neighbor)
table to CSV, without needing the Streamlit UI running. Same math as load_cliff_safety."""
import sqlite3
import sys
import pandas as pd

import strategies

DB_PATH = "./cache/trading_universe.db"
CLIFF_SAFETY_RADIUS = 3

_LEGACY_TRAILING_BOTH_TRAIL_PCT = {'v2.13': 1.0, 'v2.14': 2.0, 'v2.15': 3.0, 'v2.16': 4.0, 'v2.17': 5.0}


def _resolve_sl_display(version, strategy, stop_loss, trail_buy_pct, trail_pct):
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy)
    if sl_axis_col == 'stop_loss':
        return 'SL %', f"{stop_loss:g}", stop_loss
    is_v3 = version.startswith('v3.')
    real_sl_axis_val = (trail_buy_pct if sl_axis_col == 'trail_buy_pct' else trail_pct) if is_v3 else stop_loss
    if sl_axis_col == 'trail_buy_pct' and fourth_axis_col == 'trail_pct':
        real_trail_pct = trail_pct if is_v3 else _LEGACY_TRAILING_BOTH_TRAIL_PCT.get(version, 3.0)
        return 'Bounce % / Trail %', f"{real_sl_axis_val:g} / {real_trail_pct:g}", real_sl_axis_val
    label = 'Bounce %' if sl_axis_col == 'trail_buy_pct' else 'Trail %'
    return label, f"{real_sl_axis_val:g}", real_sl_axis_val


def load_cliff_safety(conn, version_strategy_pairs):
    results = []
    for version, strategy in version_strategy_pairs:
        sl_axis_col, _ = strategies.resolve_axis_columns(strategy)
        neighbor_axis_col = sl_axis_col if version.startswith('v3.') else 'stop_loss'
        tickers = [r[0] for r in conn.execute(
            "SELECT DISTINCT ticker FROM backtest_cache WHERE version=? AND strategy=? AND trades > 0",
            (version, strategy)
        ).fetchall()]
        for ticker in tickers:
            row = conn.execute("""
                SELECT axis_tp, stop_loss, max_hold_hours, window, z_score_threshold,
                       alpha_vs_spy, strategy_return, trades, win_rate, trail_buy_pct, trail_sell_pct,
                       win_twin_rate
                FROM backtest_cache
                WHERE version=? AND ticker=? AND strategy=? AND trades > 0
                ORDER BY alpha_vs_spy DESC LIMIT 1
            """, (version, ticker, strategy)).fetchone()
            if not row:
                continue
            (tp, sl, hold, window, z, alpha, ret, trades, win_rate,
             trail_buy_pct, trail_pct, win_twin_rate) = row
            tp, sl, hold, window = int(tp), int(sl), int(hold), int(window)
            trail_buy_pct, trail_pct = float(trail_buy_pct or 0), float(trail_pct or 0)
            sl_label, sl_display, neighbor_center = _resolve_sl_display(
                version, strategy, sl, trail_buy_pct, trail_pct)
            worst = conn.execute(f"""
                SELECT MIN(alpha_vs_spy) FROM backtest_cache
                WHERE version=? AND ticker=? AND strategy=?
                  AND window=? AND z_score_threshold=?
                  AND axis_tp BETWEEN ? AND ?
                  AND {neighbor_axis_col} BETWEEN ? AND ?
                  AND max_hold_hours BETWEEN ? AND ?
                  AND trades > 0
            """, (version, ticker, strategy, window, z,
                  tp - CLIFF_SAFETY_RADIUS, tp + CLIFF_SAFETY_RADIUS,
                  neighbor_center - CLIFF_SAFETY_RADIUS, neighbor_center + CLIFF_SAFETY_RADIUS,
                  hold - 7, hold + 7)).fetchone()[0]
            worst_neighbor = float(worst) if worst is not None else float(alpha)
            results.append({
                'ticker': ticker, 'version': version, 'strategy': strategy,
                'best_alpha': float(alpha), 'worst_neighbor': worst_neighbor,
                'safe': worst_neighbor >= 0,
                'take_profit': tp, 'sl_label': sl_label, 'sl_display': f"'{sl_display}",
                'max_hold_hours': hold,
                'window': window, 'z': float(z),
                'strategy_return': float(ret), 'trades': int(trades), 'win_rate': float(win_rate),
                'win_twin_rate': float(win_twin_rate or 0),
            })
    return pd.DataFrame(results)


if __name__ == '__main__':
    out_path = sys.argv[1] if len(sys.argv) > 1 else 'logs/cliff_safety_v3x.csv'
    with sqlite3.connect(DB_PATH, timeout=60) as conn:
        pairs = [(v, s) for v, s in conn.execute(
            "SELECT DISTINCT version, strategy FROM backtest_cache ORDER BY version DESC"
        ).fetchall() if v.startswith('v3.')]
        print(f"{len(pairs)} v3.x version/strategy pairs")
        df = load_cliff_safety(conn, pairs)
    df = df.sort_values('worst_neighbor')
    df.to_csv(out_path, index=False)
    print(f"Wrote {len(df)} rows to {out_path}")
