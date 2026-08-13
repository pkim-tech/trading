"""EOD/nightly-routine live-node table: account | ticker | trigger | price | state/action |
validation-vs-backtest, split into (brokerage/roth/ira) vs soxl_ira, per the user's requested
nightly-routine format (2026-08-12). Real state only (open_positions/pending_buys), current
price + trigger recomputed from cached hourly data via the real strategy indicator code,
sliced to strictly-before-today before computing indicators -- matches
signals_compute.compute_buy_signal's own prior-day SMA/Std method exactly (fixed 2026-08-13:
the original version included today's own in-progress bar, diverging from production by
~1.5% on real SOXL data, found by paired review).

Validation column is a REAL replay of each node's exact live config over 4 fixed rolling
windows (4wk/3mo/YTD/1yr) via the same flat-start replay machinery
scripts/paper_vs_backtest_reconcile.py uses (get_trades_and_bars_since) -- window return %
and trade count per window, not an annualized CAGR figure (the user asked for CAGR initially
but the raw window return + trade count is what's actually printed, since annualizing a
1-4-trade sample is noisy to the point of being misleading -- 2026-08-13: "i don't need
alpha - i need cagr ... i mean like in the last few weeks")."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
from datetime import timedelta

import pandas as pd
import strategies

from scripts.paper_vs_backtest_reconcile import get_trades_and_bars_since
from scripts.export_trades import load_hourly

DB_PATH = Path(__file__).resolve().parent.parent / "cache" / "live" / "trading_live.db"
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "research"

SOXL_IRA_ACCOUNTS = {"soxl_ira"}
OTHER_ACCOUNTS = {"brokerage", "roth", "ira"}


def load_nodes(conn, states):
    """states: iterable of watch_list.state values to include. The 'Brokerage/Roth/IRA' table
    is real-capital-only (state='live'); the soxl_ira table also needs 'dry_run' (2026-08-13
    fix: the new FAS/FAZ canary nodes built this session are all state='dry_run' -- the
    original state='live'-only filter meant the very nodes this table exists to track for
    soxl_ira never appeared in it, found by paired review)."""
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    placeholders = ",".join("?" * len(states))
    c.execute(f"""SELECT id, ticker, account, strategy, window, z_score_threshold, fixed_sl,
                         arm_sell_pct, take_profit, trail_buy_pct, trail_sell_pct, entry_timing,
                         max_hold_hours, added_at, drought_overlay_enabled, drought_vol_gate,
                         drought_confirm_days, addon_enabled, force_same_day_block
                  FROM watch_list WHERE state IN ({placeholders})""", states)
    nodes = [dict(r) for r in c.fetchall()]
    for n in nodes:
        n["z"] = n["z_score_threshold"]
        # arm_pct is an overloaded column across strategies -- TrailingBoth stores it in
        # arm_sell_pct, TrailingExit stores it in take_profit (same pattern documented
        # repeatedly in CLAUDE.md's backtest_cache overloaded-column notes).
        n["arm_pct"] = n["arm_sell_pct"] if n["strategy"] == "TrailingBothZScoreBreakout" else n["take_profit"]
        if n["arm_pct"] is None:
            n["arm_pct"] = 0.0
    return nodes


def trigger_and_price(node):
    """current price + real entry trigger, computed the SAME way signals_compute.compute_buy_signal
    does it -- df_daily sliced to STRICTLY BEFORE today before computing indicators (2026-08-13
    fix: the original version resampled the full frame including today's own in-progress bar,
    which inflates/deflates SMA/Std with a partial day's data -- confirmed on real SOXL data to
    diverge from production by ~1.5%, found by paired review)."""
    t = node["ticker"]
    df = pd.read_csv(CACHE_DIR / f"{t}_1h.csv", index_col=0, parse_dates=True)
    close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    if close_col != "Close":
        df["Close"] = df[close_col]
    current = df["Close"].iloc[-1]
    df_daily = df.resample("D").last().dropna(subset=["Close"])
    today = pd.Timestamp.now().normalize()
    df_daily_prior = df_daily[df_daily.index < today]
    strat_cls = getattr(strategies, node["strategy"])
    strat = strat_cls(window=node["window"], z_score_threshold=node["z_score_threshold"])
    ind = strat.generate_daily_indicators(df_daily_prior)
    ind = ind.dropna()
    if ind.empty:
        return current, None, None
    last_day = ind.index[-1]
    sma, std = ind.loc[last_day, "SMA"], ind.loc[last_day, "Std"]
    lower = sma - std * node["z_score_threshold"]
    pct_away = (current - lower) / lower * 100
    return current, lower, pct_away


def state_and_action(conn, node):
    c = conn.cursor()
    c.execute("SELECT entry_price FROM open_positions WHERE wl_id=?", (node["id"],))
    pos = c.fetchone()
    if pos:
        return f"OPEN {pos[0]:.4f} entry"
    c.execute("SELECT signal_price, order_placed, order_id FROM pending_buys WHERE wl_id=?", (node["id"],))
    pb = c.fetchone()
    if pb:
        signal_price, order_placed, order_id = pb
        if order_id:
            return f"PENDING order_id={order_id} (resting, unfilled)"
        return f"PENDING signal={signal_price:.4f} (no broker order id on file)"
    return "flat"


def window_replay(node, df_h, sim_start):
    """Real replay of this node's exact live config from sim_start to the latest cached
    bar, flat-start (see get_trades_and_bars_since docstring) -- returns (window_return_pct,
    trade_count), or None if the strategy isn't supported by the replay mirror."""
    try:
        trades, _ = get_trades_and_bars_since(node, sim_start)
    except ValueError:
        return None  # unhandled strategy in the replay mirror
    compounded = 1.0
    for t in trades:
        compounded *= (1.0 + t["ret"])  # fraction, not percent (see export_trades.py trade dicts)
    return (compounded - 1.0) * 100.0, len(trades)


# (label, sim_start-computation) -- computed relative to the latest cached bar's date, per
# the user's request (2026-08-13): "4 week, 3 month, ytd, 1 year rolling".
def _windows(latest_bar_date):
    return [
        ("4wk", latest_bar_date - timedelta(weeks=4)),
        ("3mo", latest_bar_date - timedelta(weeks=13)),
        ("YTD", pd.Timestamp(year=latest_bar_date.year, month=1, day=1)),
        ("1yr", latest_bar_date - timedelta(days=365)),
    ]


def multi_window_validation(node):
    try:
        df_h = load_hourly(node["ticker"])
    except FileNotFoundError:
        return "no cached data"
    latest = df_h.index.max()
    parts = []
    for label, sim_start in _windows(latest):
        result = window_replay(node, df_h, sim_start)
        if result is None:
            parts.append(f"{label}=unsupported")
            continue
        ret_pct, n_trades = result
        parts.append(f"{label}={ret_pct:+.1f}%({n_trades}tr)")
    return "  ".join(parts)


def candidate_role(conn, node):
    c = conn.cursor()
    c.execute("SELECT role FROM watch_list_candidate_link WHERE wl_id=?", (node["id"],))
    row = c.fetchone()
    return row[0] if row else "core"  # no link = plain core config, not a labeled overlay role


def exec_params(conn, node):
    """Drought/add-on/overlay-role/low-vol-gate/same-day-block flags -- the execution-side
    knobs layered on top of the base strategy, requested separately from trigger/state/
    validation (2026-08-13: 'i want to see the execution params - drought, add on, overlay,
    core, low vol, block trade')."""
    role = candidate_role(conn, node)
    parts = [f"role={role}"]
    if node["drought_overlay_enabled"]:
        gate = node["drought_vol_gate"]
        days = node["drought_confirm_days"]
        gate_s = f"gate={gate}" if gate is not None else "gate=?"
        days_s = f"confirm={days}d" if days is not None else ""
        parts.append(f"drought({gate_s},{days_s})".replace(",)", ")"))
    if node["addon_enabled"]:
        parts.append("addon=on")
    if node["force_same_day_block"]:
        parts.append("SDB=forced")
    return " ".join(parts)


def print_table(conn, nodes, title):
    print(f"\n=== {title} ===")
    header = (f"{'Account':10} {'Ticker':6} {'Trigger':>10} {'Price':>10} {'%Away':>8}  {'State/Action':30}")
    print(header)
    print("-" * len(header))
    for node in sorted(nodes, key=lambda n: (n["account"], n["ticker"])):
        current, lower, pct_away = trigger_and_price(node)
        trig_s = f"{lower:.2f}" if lower is not None else "n/a"
        pct_s = f"{pct_away:+.1f}%" if pct_away is not None else "n/a"
        state = state_and_action(conn, node)
        params = exec_params(conn, node)
        val = multi_window_validation(node)
        print(f"{node['account']:10} {node['ticker']:6} {trig_s:>10} {current:>10.2f} {pct_s:>8}  {state:30}")
        print(f"           {'params:':>0} {params}")
        print(f"           {'validation:':>0} {val}")


def main():
    conn = sqlite3.connect(DB_PATH)
    other = [n for n in load_nodes(conn, ["live"]) if n["account"] in OTHER_ACCOUNTS]
    soxl_ira = [n for n in load_nodes(conn, ["live", "dry_run"]) if n["account"] in SOXL_IRA_ACCOUNTS]
    print_table(conn, other, "Brokerage / Roth / IRA (real capital)")
    print_table(conn, soxl_ira, "soxl_ira (staged tests + proving-ground)")
    conn.close()


if __name__ == "__main__":
    main()
