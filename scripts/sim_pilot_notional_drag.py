"""Quantifies the real-world sizing drag (integer share floor + trailing-buy/market
padding) for a small-notional live node, against the idealized full-fractional-share
compounding the backtest kernel assumes (run_optimization_sweep._summarize_trades:
compounded = ((Return + 1).prod() - 1)).

Built 2026-08-01: the HIBL/USD/YANG "real-world coverage pilot" nodes added the same
session run on ~$60-200 starting_notional, small enough that the 1% sizing pad plus
`int(...)` share-count floor (signals_helpers.buy_order_sizing) waste a nontrivial
fraction of notional every cycle -- and because these nodes compound off their own
realized proceeds (signals_helpers._last_sale_recovery), that waste is multiplicative,
not a one-time hit. Simulates the exact production sizing formula bar-by-bar against
each node's real historical trade sequence and compares final compounded return to the
idealized backtest number for the same trades.

Usage: .venv/bin/python scripts/sim_pilot_notional_drag.py [--watch-id ID ...]
Defaults to the 3 pilot nodes (154 HIBL, 155 USD, 156 YANG).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
import signals_db as db
from backtester import run_backtest_dispatch
from run_optimization_sweep import _load_node_inputs, _sl_axis_real_column

DEFAULT_WATCH_IDS = [154, 155, 156]

PAD_PCT = 1.0  # signals_helpers.DEFAULT_MARKET_ENTRY_PAD_PCT / trailing-buy pad_pct default


def _load_node(watch_id):
    with db._conn() as c:
        import sqlite3
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM watch_list WHERE id=?", (watch_id,)).fetchone()
    if row is None:
        raise ValueError(f"no watch_list row for id={watch_id}")
    return dict(row)


def _real_trades(node):
    strategy_class = getattr(strategies, node['strategy'])
    take_profit = node['arm_sell_pct'] if node['strategy'] == 'TrailingBothZScoreBreakout' else node['take_profit']
    # sl_raw's real meaning is strategy-dependent (docs/design.md "Grid axis meaning
    # by strategy") -- trail_buy_pct for TrailingBoth, but trail_sell_pct for
    # TrailingExit (which has no trailing-buy axis at all). Passing trail_buy_pct
    # unconditionally silently zeroed out YANG's real 17% trailing-exit width.
    sl_axis_col, _ = strategies.resolve_axis_columns(node['strategy'])
    sl_raw = node[_sl_axis_real_column(sl_axis_col)]
    z_thresh = node['z_score_threshold']
    inputs = _load_node_inputs(node['ticker'], strategy_class, node['strategy'], node['window'], z_thresh)
    if inputs is None:
        raise ValueError(f"no cached data for {node['ticker']}")
    df_hourly, df_daily, prep = inputs
    trades = run_backtest_dispatch(
        strategy_class, df_hourly, df_daily, node['ticker'],
        take_profit=take_profit, sl_raw=sl_raw,
        max_hours_to_hold=node['max_hold_hours'], z_score_threshold=z_thresh,
        fixed_sl=node['fixed_sl'], trail_pct_pct=node['trail_sell_pct'],
        entry_timing=node['entry_timing'], prep=prep,
    )
    closed_codes = ('WIN', 'LOSS', 'TWIN', 'TLOSS')
    return [t for t in trades if t['Result'] in closed_codes]


def _idealized_compounded(trades):
    total = 1.0
    for t in trades:
        total *= (1.0 + t['Return'])
    return (total - 1.0) * 100.0


def _realistic_compounded(node, trades, top_up=False):
    """Mirrors signals_helpers.buy_order_sizing + _last_sale_recovery exactly:
    shares = floor(notional / (price * (1 + pad/100))), next notional = shares * exit_price.
    top_up=True additionally mirrors signals_notify._reconcile_fill -- the real
    production post-fill top-up that buys the shortfall (target_notional minus
    actual fill notional) immediately at the real fill price, same as every real
    BUY currently does. Without this, the sizing pad's cost was being modeled as
    a permanent per-trade loss; in reality it's recovered same-day, down to less
    than one share's worth of dollars."""
    trailing_buy = db._is_trailing_buy(node)
    trail_buy_pct = node.get('trail_buy_pct') or 0.0
    notional = node['starting_notional']
    skipped = 0
    for t in trades:
        entry_price = t['Entry Price']
        exit_price = t['Exit Price']
        if trailing_buy:
            divisor = entry_price * (1 + (trail_buy_pct + PAD_PCT) / 100)
        else:
            divisor = entry_price * (1 + PAD_PCT / 100)
        shares = int(notional // divisor)
        if shares < 1:
            skipped += 1
            continue  # mirrors the real "shares >= 1" guard -- trade not taken, notional carries forward unchanged
        if top_up:
            delta = notional - shares * entry_price
            if delta > entry_price:
                shares += int(delta // entry_price)
        notional = shares * exit_price
    return notional, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--watch-id', type=int, action='append', dest='watch_ids', default=None)
    ap.add_argument('--top-up', action='store_true', help='mirror the real post-fill top-up (signals_notify._reconcile_fill)')
    args = ap.parse_args()
    watch_ids = args.watch_ids or DEFAULT_WATCH_IDS

    print(f"{'Ticker':<8}{'Start':>10}{'Trades':>8}{'Skipped':>9}{'Idealized':>14}{'Realistic':>14}{'Final $':>12}{'Drag':>10}")
    for wid in watch_ids:
        node = _load_node(wid)
        trades = _real_trades(node)
        if not trades:
            print(f"{node['ticker']:<8} -- no closed trades in cached data --")
            continue
        idealized_pct = _idealized_compounded(trades)
        final_notional, skipped = _realistic_compounded(node, trades, top_up=args.top_up)
        start = node['starting_notional']
        realistic_pct = (final_notional / start - 1.0) * 100.0
        drag_pp = idealized_pct - realistic_pct
        print(f"{node['ticker']:<8}{start:>10.0f}{len(trades):>8}{skipped:>9}"
              f"{idealized_pct:>13.1f}%{realistic_pct:>13.1f}%{final_notional:>12.0f}{drag_pp:>9.1f}pp")


if __name__ == '__main__':
    main()
