"""
Real out-of-sample walk-forward chain built from the already-run quarterly rolling-window
SOXL campaign (scripts/run_quarterly_soxl_sweep.sh, 2026-08-15): each window's own winning
("best safe node") config is re-run directly (single-node kernel call, not a grid sweep -- no
new sweep campaign needed) against the ONE calendar quarter immediately following that window's
end date -- a quarter its own fit never saw. Chaining those quarterly returns together answers
"what would have actually happened if I re-swept and rebalanced every quarter," using genuinely
out-of-sample data at every step (distinct from scripts/quarterly_config_time_stability.py,
which cross-applies a config against OTHER already-swept windows that may overlap its own fit
period).

Each rolling window is 1 year, spaced 1 quarter apart (2023-10-01_2024-09-30,
2024-01-01_2024-12-31, ...), so window N's end date is always exactly one quarter before window
N+1's start date -- meaning the quarter right after window N ends is the same quarter window N+1
starts fitting on. Only 7 of the 8 windows have a real "next quarter" already inside cached data
(the 8th window's forward quarter, 2026-07 on, is beyond what's been swept/cached).

Usage: .venv/bin/python scripts/quarterly_rebalance_walkforward.py --ticker SOXL
       .venv/bin/python scripts/quarterly_rebalance_walkforward.py --ticker SOXL --node-ids 360 363 358 357 362 355 369 365
       .venv/bin/python scripts/quarterly_rebalance_walkforward.py --ticker SOXL --hop

--hop (added 2026-08-16, user's idea): instead of jumping straight to each window's fresh
"best safe node" every quarter, move the currently-applied node ONE real grid-step toward that
quarter's target on every axis that differs, and apply THAT (not the raw target) to the forward
quarter. Rationale: the currently-applied node is proven on real prior out-of-sample data
(it's been trading, not just fit); the fresh target is a single noisy in-sample re-optimization
that could jump to an unproven, overfit region of param space. Hopping only ever trades
one-grid-step away from something already validated, damping how fast the config can move.
Step sizes per axis mirror what run_optimization_sweep.py's own island/cliff-box refinement
treats as "one neighbor" (see run_phase2_island/run_phase25_cliff_box): arm_pct/trail_buy_pct/
trail_sell_pct step by 1, max_hold_hours by 7 (one trading day = 7 hourly bars), z by 0.5.
window/fixed_sl/entry_timing aren't swept within a campaign here so they don't need a step size.
A strategy-class change (TrailingBoth <-> TrailingExit) has no continuous "hop" between two
different kernels -- treated as a full snap to the target on that one transition.
"""
import argparse
import itertools
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import strategies
from backtester import run_backtest_dispatch
from run_optimization_sweep import _load_node_inputs, compute_bh_returns

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


def dispatch_kwargs(node):
    """Same real per-strategy grid-axis mapping as cross_apply_config_test.py's
    dispatch_kwargs -- mirrors run_optimization_sweep.py's grid-axis meaning."""
    strat_cls = getattr(strategies, node["strategy"])
    if node["strategy"] == "TrailingBothZScoreBreakout":
        return dict(
            strategy_class=strat_cls, take_profit=node["arm_pct"], sl_raw=node["trail_buy_pct"],
            fixed_sl=node["fixed_sl"], trail_pct_pct=node["trail_sell_pct"],
            max_hours_to_hold=node["max_hold_hours"], z_score_threshold=node["z"],
            entry_timing=node["entry_timing"],
        )
    if node["strategy"] == "TrailingExitZScoreBreakout":
        return dict(
            strategy_class=strat_cls, take_profit=node["arm_pct"], sl_raw=node["trail_sell_pct"],
            fixed_sl=node["fixed_sl"], trail_pct_pct=0.0,
            max_hours_to_hold=node["max_hold_hours"], z_score_threshold=node["z"],
            entry_timing=node["entry_timing"],
        )
    raise ValueError(f"unhandled strategy {node['strategy']}")


def run_windowed(node, start_date, end_date):
    """Runs node's exact config against [start_date, end_date] only, via the same
    windowed-prep path the real Stage 2 date-windowed sweep uses (_load_node_inputs) --
    not a grid sweep, one direct kernel call."""
    strat_cls = getattr(strategies, node["strategy"])
    _, _, prep = _load_node_inputs(node["ticker"], strat_cls, node["strategy"], node["window"],
                                    node["z"], start_date=start_date, end_date=end_date)
    kwargs = dispatch_kwargs(node)
    trades = run_backtest_dispatch(df_hourly=None, df_daily_indicators=None,
                                    ticker=node["ticker"], prep=prep, **kwargs)
    rets = [t["Return"] for t in trades if t["Result"] != "OPEN"]
    compounded = (float(np.prod([1 + r for r in rets]) - 1) * 100.0) if rets else 0.0
    return compounded, len(rets)


AXIS_STEPS = {
    "arm_pct": 1,
    "trail_buy_pct": 1,
    "trail_sell_pct": 1,
    "max_hold_hours": 7,
    "z": 0.5,
}


def hop_toward(current, target):
    """Moves `current` one real grid-step toward `target` on every AXIS_STEPS axis that
    differs; a strategy-class change snaps fully (no continuous hop between kernels)."""
    if current["strategy"] != target["strategy"]:
        return dict(target)
    hopped = dict(current)
    hopped["ticker"] = target["ticker"]
    hopped["version"] = target["version"]  # for display/labeling only
    for axis, step in AXIS_STEPS.items():
        cur_v, tgt_v = current[axis], target[axis]
        if cur_v == tgt_v:
            continue
        hopped[axis] = min(tgt_v, cur_v + step) if cur_v < tgt_v else max(tgt_v, cur_v - step)
    return hopped


def local_search(current, fit_start, fit_end, radius):
    """Real local re-optimization (2026-08-16, user's idea): instead of hopping toward a
    global re-swept target, directly search a neighborhood of `radius` grid-steps around
    `current` on that strategy's real axes, scored on THIS window's own fit data (direct
    kernel calls, not a grid sweep -- window/z stay fixed at `current`'s values so prep/
    indicators are computed once and reused across every candidate in the neighborhood),
    and take whichever candidate scores best. Never considers a strategy-class change --
    only the very first window's seed picks the strategy; every subsequent window locally
    refines within that same strategy's axes. Falls back to keeping `current` unchanged
    if every candidate in the neighborhood (including `current` itself) produced zero
    trades over this window (degenerate case, e.g. axis pinned to grid boundary)."""
    strat_cls = getattr(strategies, current["strategy"])
    _, _, prep = _load_node_inputs(current["ticker"], strat_cls, current["strategy"],
                                    current["window"], current["z"],
                                    start_date=fit_start, end_date=fit_end)
    axes = ["arm_pct", "trail_sell_pct", "max_hold_hours"]
    if current["strategy"] == "TrailingBothZScoreBreakout":
        axes.append("trail_buy_pct")

    ranges = {}
    for ax in axes:
        step = AXIS_STEPS[ax]
        lo_bound = 7 if ax == "max_hold_hours" else (0 if ax == "trail_buy_pct" else 1)
        c = current[ax]
        vals = sorted({max(lo_bound, c + k * step) for k in range(-radius, radius + 1)})
        ranges[ax] = vals

    _, spy_bh = compute_bh_returns(current["ticker"], fit_start, fit_end)

    best, best_score = None, None
    for combo in itertools.product(*(ranges[a] for a in axes)):
        cand = dict(current)
        for a, v in zip(axes, combo):
            cand[a] = v
        kwargs = dispatch_kwargs(cand)
        trades = run_backtest_dispatch(df_hourly=None, df_daily_indicators=None,
                                        ticker=cand["ticker"], prep=prep, **kwargs)
        rets = [t["Return"] for t in trades if t["Result"] != "OPEN"]
        if not rets:
            continue
        compounded = float(np.prod([1 + r for r in rets]) - 1) * 100.0
        alpha = compounded - spy_bh
        if best_score is None or alpha > best_score:
            best_score, best = alpha, cand
    return best if best is not None else dict(current)


def next_quarter(end_date_str):
    end = pd.Timestamp(end_date_str)
    q_start = end + pd.Timedelta(days=1)
    q_end = q_start + pd.DateOffset(months=3) - pd.Timedelta(days=1)
    return q_start.strftime("%Y-%m-%d"), q_end.strftime("%Y-%m-%d")


def data_available_through(ticker):
    path = Path("cache/research") / f"{ticker}_1h.csv"
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    return df.index.max()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="SOXL")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--node-ids", nargs="*", type=int,
                     default=[360, 363, 358, 357, 362, 355, 369, 365],
                     help="candidate_nodes ids, one per rolling window, in chronological "
                          "window-start order (default: the real 2026-08-15 SOXL campaign's "
                          "8 'best safe node' picks)")
    ap.add_argument("--mode", choices=["direct", "hop", "neighbor"], default="direct",
                     help="direct: jump straight to each quarter's fresh target. "
                          "hop: move 1 grid-step toward the target per axis. "
                          "neighbor: locally re-search a neighborhood around the current "
                          "node using that window's own fit data (ignores the target "
                          "entirely past the first window's seed).")
    ap.add_argument("--neighbor-radius", type=int, default=4,
                     help="grid-steps searched per axis in --mode neighbor (default 4)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    targets = [get_node(conn, nid) for nid in args.node_ids]
    targets.sort(key=lambda n: n["version"])

    data_end = data_available_through(args.ticker)
    print(f"{args.ticker}: {len(targets)} rolling windows, cached data through {data_end.date()}, "
          f"mode={args.mode}"
          + (f", radius={args.neighbor_radius}" if args.mode == "neighbor" else "") + "\n")
    print(f"{'window (fit on)':30s}{'applied config':45s}{'forward quarter':22s}{'q_return':>10s}"
          f"{'q_spy_bh':>10s}{'trades':>8s}{'chained':>12s}")

    chained = 1.0
    applied = None
    for target in targets:
        fit_start = target["version"].split("-w")[1].split("_")[0]
        fit_end = target["version"].split("-w")[1].split("_")[1]

        if applied is None:
            applied = dict(target)
        elif args.mode == "hop":
            applied = hop_toward(applied, target)
        elif args.mode == "neighbor":
            applied = local_search(applied, fit_start, fit_end, args.neighbor_radius)
        else:
            applied = dict(target)

        cfg_label = (f"{applied['strategy'][:12]} z={applied['z']} arm={applied['arm_pct']} "
                     f"tb={applied['trail_buy_pct']} ts={applied['trail_sell_pct']} "
                     f"hold={applied['max_hold_hours']}")

        q_start, q_end = next_quarter(fit_end)
        if pd.Timestamp(q_end) > data_end:
            print(f"{fit_start}_{fit_end:12s}{cfg_label:45s}"
                  f"{q_start}_{q_end:10s}{'(no data yet -- forward quarter not cached)':>10s}")
            continue

        q_return, n_trades = run_windowed(applied, q_start, q_end)
        asset_bh, spy_bh = compute_bh_returns(args.ticker, q_start, q_end)
        chained *= (1 + q_return / 100.0)
        print(f"{fit_start}_{fit_end:12s}{cfg_label:45s}"
              f"{q_start}_{q_end:10s}{q_return:>9.1f}%{spy_bh:>9.1f}%{n_trades:>8d}"
              f"{(chained - 1) * 100:>11.1f}%")

    if args.mode == "hop":
        print(f"\n'applied config' is the running node after hopping 1 grid-step toward that "
              f"window's fresh target on every differing axis (full snap on a strategy-class "
              f"change) -- applied to the SINGLE quarter immediately after that window's fit "
              f"period ended. 'chained' compounds quarter-over-quarter.")
    elif args.mode == "neighbor":
        print(f"\n'applied config' is the running node after a local re-search (radius="
              f"{args.neighbor_radius}) around the prior applied node, scored on that "
              f"window's own fit data -- the global sweep target is only used to seed the "
              f"very first window. 'chained' compounds quarter-over-quarter.")
    else:
        print(f"\nEach row's 'applied config' is that window's own real winning node, applied "
              f"to the SINGLE quarter immediately after its fit period ended -- genuinely "
              f"out-of-sample for that config. 'chained' compounds quarter-over-quarter as if "
              f"you rebalanced into the freshly-swept config at the start of every quarter.")


if __name__ == "__main__":
    main()
