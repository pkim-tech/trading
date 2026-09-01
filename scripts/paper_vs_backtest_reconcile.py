"""
Reconciles real paper-trading activity against the corrected backtest kernel, for both
paper roles on a v5 watchlist ticker:

- live-track (paper_role IS NULL, free-running, live-tick pricing): no other tool computes
  this comparison, so this script still does it directly -- matches paper_trade_log rows to
  a fresh backtest replay by entry date (not exact timestamp, since paper's real fill can
  drift slightly from the idealized backtest fill) and reports win-rate/compounded-return
  side by side, flagging direction mismatches and trade-count gaps.
- daily-track (paper_role='daily_sync', hourly-close pricing): reconciliation already runs
  nightly (paper_trading.reconcile_daily_track_nodes) and writes one full diagnostic row per
  node per night to daily_track_reconciliation_log -- this script reports against THAT log
  instead of re-deriving its own comparison (rebuilt 2026-08-05, see docs/backlog_cache.md
  piece 4). Re-deriving would drift from the nightly job's own classification logic (entry
  Close-vs-Open counterfactuals, exit wick reconstruction, TIME-forced exclusion) and risk a
  second, subtly different implementation of the same judgment.

Both sides are now wl_id-scoped (paper_trade_log/daily_track_reconciliation_log both carry
wl_id as of 2026-08-05) rather than ticker-scoped -- ticker-only matching would silently
conflate live-track and daily-track paper trades for the same ticker now that both exist.

Usage: .venv/bin/python scripts/paper_vs_backtest_reconcile.py [--tickers ...] [--watchlist-id 65]
       [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--csv]
"""
import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import signals_db as db
from backtester import prep_inputs, prep_minute_inputs, run_backtest_ground_truth, OPEN
from scripts.export_trades import load_hourly, simulate_trail_both_annotated, simulate_trail_exit_chaos
from scripts.drought_overlay_test import build_indicators, TARGET_H0, TARGET_H1

LIVE_DB = Path("cache/live/trading_live.db")
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

# Rows worth surfacing individually, not just counting -- everything else ('match',
# 'pending_skip', 'exit_early') is either agreement or a case with no counterfactual to run.
FLAGGED_ACTIONS = {
    "entry_miss_unexplained", "exit_wick_unexplained", "exit_bar_mismatch",
    "ambiguous_position", "no_backtest_data", "replay_failed",
}


def get_paper_trades(wl_id, start, end):
    """end is a YYYY-MM-DD date; compared lexically against full timestamps, so it must be
    extended to end-of-day or every trade entered ON the end date is silently excluded
    ('2026-08-04 14:30:16' > '2026-08-04' lexically)."""
    con = sqlite3.connect(LIVE_DB)
    rows = con.execute("""
        SELECT ticker, entry_time, exit_time, pnl_pct, exit_reason
        FROM paper_trade_log
        WHERE wl_id = ? AND entry_time >= ? AND entry_time <= ?
    """, (wl_id, start, f"{end} 23:59:59")).fetchall()
    con.close()
    return pd.DataFrame(rows, columns=["ticker", "entry_time", "exit_time", "pnl_pct", "exit_reason"])


def _is_ground_truth_node(node):
    """v6 nodes are tagged via watch_list.version, the same column/convention
    run_optimization_sweep.py/locate_best_node.py already use to mark a
    backtest_cache/candidate_nodes row as ground-truth-kernel-sourced (version
    starting with 'v6', e.g. 'v6', 'v6-massive', 'v6-w2023-07-24_2026-08-21') --
    a promoted-to-live node's watch_list.version is copied straight from the
    candidate row it came from (see locate_best_node.node_dict), so this is the
    same string a v6 candidate already carries pre-promotion, not a new tag."""
    return str(node.get("version") or "").startswith("v6")


def get_trades_and_bars_since_ground_truth(node, sim_start):
    """v6 (ground-truth minute kernel) counterpart to get_trades_and_bars_since below.
    Real minute data through backtester.run_backtest_ground_truth, not the legacy
    hourly kernel -- replaying a v6 node through the wrong (hourly) kernel would
    silently give a misleading divergence read (the routing gap this function exists
    to close, see docs/deep_backlog.md).

    Cold-start (sim_start / flat-start-position): confirmed there IS a clean GT
    equivalent to get_trades_and_bars_since's own p_sliced approach, already used by
    the real GT sweep engine itself (run_optimization_sweep._load_node_inputs_ground_
    truth's start_date/end_date windowing) -- slice the HOURLY bars fed to prep_inputs/
    prep_minute_inputs to start at sim_start BEFORE computing them, while daily
    indicators (`ind`, from build_indicators) stay computed off the full, unsliced
    df_daily. That mirrors the hourly path's own sma_arr/std_arr-stay-unsliced
    reasoning (full lookback for the indicator, flat start for the simulator) and is
    the same windowing shape _load_node_inputs_ground_truth already relies on for
    every real GT campaign, not a new invention for this file. minute_df itself is
    never sliced either -- prep_minute_inputs buckets minutes onto whichever hourly
    index it's given, so handing it the windowed hourly index is sufficient to keep
    the simulator from ever seeing (or opening against) a pre-sim_start bar.

    Known imprecision (documented, not silently swept under the rug -- corrected
    2026-08-23 after paired Opus review caught the first draft understating this):
    the legacy hourly path tracks a trade's `signal_i` (the bar the z-score band was
    first breached, i.e. when a WAIT/trailing-buy STARTS) separately from its actual
    fill bar. run_backtest_ground_truth's own trade output does not expose the
    analogous internal wait-start bar (only entry/exit fill times), and this file is
    deliberately not re-deriving that internal state-machine detail here (would be a
    second, subtly-different reimplementation of _simulate_trail_ground_truth's own
    WAIT-state tracking -- exactly the drift risk CLAUDE.md's daily-track-reconciliation
    convention warns against). So for GT TrailingBoth trades, `signal_i` below is the
    bar OWNING the real fill time, not the original signal bar. This is exact ONLY for
    TrailingExitZScoreBreakout (no WAIT state exists in the kernel for that strategy,
    fill IS the signal). For TrailingBothZScoreBreakout it is NOT exact for either
    entry_timing -- `is_both` always goes through STATE_WAIT regardless of open_check
    vs close_check (see backtester.py's `_simulate_trail_ground_truth`, the
    `open_check_entry_timing and o <= band` branch under `if is_both:` sets STATE_WAIT,
    it does not fill immediately), so the fill can land any bar up to
    `wait_bar0 + max_hours_to_hold` later. Every real live node as of 2026-08-23 is
    entry_timing='open_check', and the TrailingBoth ones carry max_hold_hours in the
    11-126-bar range -- meaning this approximation applies to essentially every real
    live TrailingBoth v6 node, not just an edge case, and can drift far past
    evening_status.py Part 3's match_trades 4h tolerance, producing false PHANTOM/
    MISSED flags there. It does NOT affect the win-rate/compounded-return comparisons
    the rest of this script and Part 5's compute_divergence rely on (those only use
    `ret`, never `signal_i`/entry_time). Recovering the true signal bar would require
    changing run_backtest_ground_truth's return contract (out of this file's scope) --
    flagged as a real follow-up, not fixed here.
    """
    if node["strategy"] not in ("TrailingBothZScoreBreakout", "TrailingExitZScoreBreakout"):
        raise ValueError(f"unhandled strategy {node['strategy']}")

    # yahoo fully retired project-wide (2026-08-27) -- massive is the only real data
    # source now. Previously conditioned on '-massive' being present in node['version'],
    # but only 2 of 14 real live nodes ever carried that marker (ETHU/OILU); the other
    # 12 have plain version='v6' and were silently falling through to the dead yahoo
    # path, causing every real evening_status.py Part 3 "Live vs kernel" check for them
    # to fail with a stale-data RuntimeError.
    data_source = "massive"
    import db_cache
    df_h = db_cache.get_massive_hourly_ohlcv(node["ticker"])

    from run_optimization_sweep import _load_minute_df
    minute_df = _load_minute_df(node["ticker"], data_source=data_source)

    # Recent-cache tail extension (Task #11, 2026-09-01): massive_hourly_derived/
    # massive_minute_derived are only refreshed manually (no automated nightly
    # promotion -- confirmed a real reproducibility risk for the sweep pipeline if that
    # were ever cron-automated, see scripts/refresh_recent_minute_cache.py's own
    # docstring for the full finding). scripts/refresh_recent_minute_cache.py instead
    # maintains a genuinely separate, disposable same-day/recent-days cache that never
    # touches the canonical archive. Splice its rows in here, strictly beyond each
    # frame's own current max (never overriding/duplicating anything the canonical
    # snapshot already covers -- canonical always wins where both exist), BEFORE
    # df_daily/ind are derived from df_h below -- splicing any later (e.g. only into
    # df_h_sliced) would extend the bar-by-bar replay's DATA but leave the z-score
    # band's own indicator computation (ind, from df_daily) still blind to the fresh
    # days, silently mis-signaling near the tail. Also before the staleness guard
    # further down, so a same-day trade the canonical snapshot hasn't caught up to yet
    # still gets checked instead of silently reading as a false PHANTOM (tonight's
    # real UGL incident). KNOWN LIMITATION, accepted (see that script's docstring):
    # the recent cache is only split-adjusted, not dividend-adjusted like the
    # canonical series -- negligible over a few days for these tickers, but a real,
    # uncorrected discrepancy versus canonical is possible in principle.
    from scripts.refresh_recent_minute_cache import load_recent_cache
    recent_minute = load_recent_cache(node["ticker"])
    if not recent_minute.empty:
        new_minute_tail = recent_minute.loc[recent_minute.index > minute_df.index.max()] \
            if not minute_df.empty else recent_minute
        if not new_minute_tail.empty:
            minute_df = pd.concat([minute_df, new_minute_tail]).sort_index()
            from scripts.build_massive_hourly_derived import resample_to_hourly
            new_hourly_tail = resample_to_hourly(new_minute_tail)
            new_hourly_tail = new_hourly_tail.loc[new_hourly_tail.index > df_h.index.max()] \
                if not df_h.empty else new_hourly_tail
            if not new_hourly_tail.empty:
                df_h = pd.concat([df_h, new_hourly_tail]).sort_index()

    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    ind = build_indicators(node["strategy"], df_daily, node["window"])
    open_check = node["entry_timing"] == "open_check"

    df_h_sliced = df_h.loc[pd.Timestamp(sim_start):]
    empty_ts = pd.DatetimeIndex([])
    if df_h_sliced.empty:
        return [], empty_ts

    # Minute data is only refreshed manually (scripts/fetch_massive_minute_data.py /
    # build_massive_hourly_derived.py -- no cron entry, confirmed 2026-08-23 paired
    # review), unlike the hourly CSV's own crontab-scheduled collector. Without this
    # guard, a stale minute snapshot silently produces a bar_min_count=0 hourly bar for
    # every real bar past the snapshot's cutoff -- TrailingBoth then never leaves
    # STATE_WAIT and TrailingExit can only ever time out, so the kernel quietly reports
    # ZERO trades instead of erroring, and every real trade in that window reads as a
    # false PHANTOM (Part 3) / false divergence (Part 5). Fail loud instead: callers
    # (evening_status.py Parts 3/5, eod_live_node_table.window_replay) already wrap
    # this call in try/except and report "NOT CHECKED"/"unsupported" on exception,
    # which is the correct outcome here -- an unrefreshed snapshot must read as
    # "couldn't check," never as "checked, found nothing." (Still fires normally if
    # even the recent-cache splice above doesn't reach far enough -- e.g. the recent
    # cache itself hasn't been refreshed recently either.)
    if minute_df.empty or minute_df.index.max() < df_h_sliced.index.max():
        raise RuntimeError(
            f"get_trades_and_bars_since_ground_truth: minute data for {node['ticker']} "
            f"(data_source={data_source!r}) is stale -- covers through "
            f"{minute_df.index.max() if not minute_df.empty else 'no data'}, but the "
            f"hourly replay needs bars through {df_h_sliced.index.max()}. Refresh minute "
            f"data before trusting this v6 divergence check.")

    prep = prep_inputs(df_h_sliced, ind)
    mprep = prep_minute_inputs(minute_df, df_h_sliced)

    gt_trades = run_backtest_ground_truth(
        df_h_sliced, ind, node["ticker"], minute_df,
        fixed_sl=node["fixed_sl"], arm_pct=node["arm_pct"],
        trail_buy_pct=node.get("trail_buy_pct") or 0.0, trail_sell_pct=node["trail_sell_pct"],
        max_hours_to_hold=node["max_hold_hours"], z_score_threshold=node["z"],
        is_both=node["strategy"] == "TrailingBothZScoreBreakout",
        target_hours=(TARGET_H0, TARGET_H1), open_check_entry_timing=open_check,
        prep=prep, mprep=mprep, need_times=True,
    )

    timestamps = prep["timestamps"]
    real_trades = []
    for t in gt_trades:
        # floor-lookup: bar H:30 owns [H:30, H+1:30) -- same bucketing convention
        # prep_minute_inputs/run_backtest_ground_truth's own resolve_time() use.
        signal_i = int(timestamps.searchsorted(t["Entry Time"], side="right")) - 1
        real_trades.append({"signal_i": signal_i, "ret": t["Return"], "result": t["exit_reason"]})
    return real_trades, timestamps


def get_trades_and_bars_since(node, sim_start):
    """Same replay get_trades_and_bars() does, except the simulator's own bar-by-bar
    loop only sees hourly rows from sim_start onward -- so it starts flat (no position),
    matching how paper trading itself actually started, instead of a full multi-year
    replay that can be sitting mid-position on whatever calendar date happens to fall
    inside the comparison window.

    Found 2026-08-12 diagnosing a false 'paper diverged from backtest' reading for
    YANG: the full-history replay was still holding a position it opened 2026-07-20
    (predating this node's real activity), so its entry-check loop never got a chance
    to evaluate the 2026-07-28/29 window at all -- not a signal-computation bug, a
    cold-start artifact of comparing paper's real (flat-start) history against a
    backtest that assumes it's always been trading.

    Only the per-hourly-bar arrays (prices/highs/lows/opens/hours/daily_idx/timestamps)
    are sliced -- sma_arr/std_arr/trend_arr stay full/unsliced since they're indexed
    per calendar day via daily_idx, not per array position, so slicing them too would
    misalign every lookup. This keeps the full lookback available for computing SMA/std
    (a node's window needs real prior-day history), it just stops the simulator from
    opening/holding a position based on a signal from before sim_start.

    v5/v5.1 (hourly kernel) path only -- see _is_ground_truth_node/
    get_trades_and_bars_since_ground_truth for the v6 (minute ground-truth kernel)
    counterpart this dispatches to below. v5/v5.1 behavior here is unchanged.
    """
    if _is_ground_truth_node(node):
        return get_trades_and_bars_since_ground_truth(node, sim_start)

    df_h = load_hourly(node["ticker"])
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    ind = build_indicators(node["strategy"], df_daily, node["window"])
    p = prep_inputs(df_h, ind)
    open_check = node["entry_timing"] == "open_check"

    start_pos = p["timestamps"].searchsorted(pd.Timestamp(sim_start))
    p_sliced = dict(p)
    for k in ("prices", "highs", "lows", "opens", "hours", "daily_idx", "timestamps"):
        p_sliced[k] = p[k][start_pos:]

    if node["strategy"] == "TrailingBothZScoreBreakout":
        trades = simulate_trail_both_annotated(
            p_sliced, node["arm_pct"] / 100.0, node["fixed_sl"] / 100.0, node["max_hold_hours"],
            node["trail_buy_pct"] / 100.0, node["trail_sell_pct"] / 100.0,
            TARGET_H0, TARGET_H1, node["z"], open_check=open_check,
        )
    elif node["strategy"] == "TrailingExitZScoreBreakout":
        rng = np.random.default_rng(0)
        trades = simulate_trail_exit_chaos(
            p_sliced, node["arm_pct"] / 100.0, node["fixed_sl"] / 100.0, node["max_hold_hours"],
            node["trail_sell_pct"] / 100.0, TARGET_H0, TARGET_H1, node["z"],
            rng, "drop", 0.0, "drop", 0.0, open_check=open_check,
        )
    else:
        raise ValueError(f"unhandled strategy {node['strategy']}")

    real_trades = [t for t in trades if t["signal_i"] is not None and t["result"] != OPEN]
    return real_trades, p_sliced["timestamps"]


def get_backtest_trades_in_window(node, start, end):
    """end is a YYYY-MM-DD date; pd.Timestamp(end) is midnight, so extend by a day or every
    trade signaling ON the end date is silently excluded -- same class of bug as
    get_paper_trades' date-string truncation.

    sim_start is the node's own added_at (when this exact config started existing/being
    scanned), not the report's --start -- the simulator must start flat at the node's
    real inception regardless of what display window the caller asked for, or a report
    window starting after inception would still inherit a phantom already-open position
    from before it. --start/--end still filter which resulting trades are shown.

    added_at is stored in UTC (watch_list's schema default is `datetime('now')`, which
    SQLite evaluates in UTC -- no 'localtime' modifier -- confirmed directly against the
    real DB, 2026-08-12 paired review) while every hourly bar timestamp is naive
    US/Eastern. Converted here (not inside get_trades_and_bars_since, whose sim_start
    contract is "already a naive-ET timestamp") since the `start` fallback below is
    already in that form (a plain YYYY-MM-DD date) and needs no conversion -- only the
    added_at path does. Currently latent for every node this script has been run
    against (all created outside trading hours, so the ~4h UTC/ET offset doesn't cross a
    bar boundary), but real for nodes created intraday (e.g. wl_id=169, added 2026-08-06
    11:16:16 UTC = 07:16 ET -- a naive comparison would have started the slice ~4h later
    than the node actually existed, silently dropping a real inception-day signal, the
    same false-divergence shape this whole fix exists to eliminate)."""
    added_at = node.get("added_at")
    if added_at:
        sim_start = (pd.Timestamp(added_at, tz="UTC")
                     .tz_convert("America/New_York").tz_localize(None))
    else:
        sim_start = start
    trades, timestamps = get_trades_and_bars_since(node, sim_start)
    out = []
    end_bound = pd.Timestamp(end) + pd.Timedelta(days=1)
    # A retired node's config can't have generated real trades after it was archived --
    # without this cap, a node retired mid-window keeps accruing phantom backtest-implied
    # trades all the way to `end`, with no real counterpart possible, silently inflating
    # any real-vs-backtest divergence comparison (found 2026-08-28, SOXL wl_id=92).
    archived_at = node.get("archived_at")
    if archived_at:
        archived_bound = (pd.Timestamp(archived_at, tz="UTC")
                           .tz_convert("America/New_York").tz_localize(None))
        end_bound = min(end_bound, archived_bound)
    for t in trades:
        if t["signal_i"] is None:
            continue
        et = timestamps[t["signal_i"]]
        if pd.Timestamp(start) <= et <= end_bound:
            out.append({"entry_time": et, "ret": t["ret"], "result": t["result"]})
    return out


def resolve_live_track_nodes_by_activity(watchlist_id, tickers=None):
    """Live-track (paper_role IS NULL) nodes that actually have real trade history,
    resolved by wl_id rather than load_nodes()'s MIN(id)/state='paper' heuristic.

    That heuristic silently breaks two ways, both found 2026-08-12 while diagnosing a
    false 'paper diverged from backtest' reading: (1) once a node is promoted to
    state='live' (e.g. SOXL wl_id=92), it drops out of the state='paper' filter entirely,
    so the comparison falls back to a same-ticker sibling that never traded at all;
    (2) even while still state='paper', a ticker can have many duplicate/candidate nodes
    sharing it, and MIN(id) has no reason to land on the one with real paper_trade_log
    rows -- confirmed for every one of HIBL/USD/SOXL/KORU/YANG/UDOW/AGQ, each compared
    against a node with a genuinely different strategy/window/z/fixed_sl than the one
    that actually produced the real trades.

    Resolves per wl_id (not per ticker) since one ticker can have more than one
    live-track node with real history (e.g. HIBL 89 and its since-promoted-to-live
    clone 154) -- reporting per-node keeps each comparison apples-to-apples instead of
    conflating two distinct configs' trades under one ticker label.

    A ticker whose live-track node(s) exist but have NEVER produced any real paper
    activity (DPST, GDXU, NUGT as of 2026-08-12) still gets exactly one row -- the
    lowest-id `state='paper'` node, falling back to lowest-id overall if none is
    `paper` -- rather than vanishing from the report entirely. "Backtest fired N
    signals, paper fired zero" (the original YANG-shaped question this whole
    investigation started from) needs to stay answerable; filtering purely on
    activity would make it silently unanswerable for a genuinely-inactive node
    (found by paired Opus review, 2026-08-12, before this shipped).
    """
    con = sqlite3.connect(LIVE_DB)
    active_ids = {r[0] for r in con.execute("SELECT DISTINCT wl_id FROM paper_trade_log")}
    active_ids |= {r[0] for r in con.execute("SELECT DISTINCT wl_id FROM paper_positions")}
    active_ids |= {r[0] for r in con.execute("SELECT DISTINCT wl_id FROM paper_pending_buys")}
    con.close()

    cols = ["id", "ticker", "strategy", "window", "z_score_threshold", "arm_sell_pct",
            "take_profit", "fixed_sl", "trail_buy_pct", "trail_sell_pct", "max_hold_hours",
            "entry_timing", "state", "added_at", "version"]

    def _to_node(n):
        node = {c: n.get(c) for c in cols}
        node["z"] = node.pop("z_score_threshold")
        node["arm_pct"] = node["arm_sell_pct"] if node["strategy"] == "TrailingBothZScoreBreakout" else node["take_profit"]
        return node

    live_track_raw = [
        n for n in db.get_watchlist(watchlist_id)
        if n.get("paper_role") is None and (not tickers or n["ticker"] in tickers)
    ]
    nodes = [_to_node(n) for n in live_track_raw if n["id"] in active_ids]

    tickers_with_activity = {node["ticker"] for node in nodes}
    by_ticker = {}
    for n in live_track_raw:
        if n["ticker"] in tickers_with_activity:
            continue
        by_ticker.setdefault(n["ticker"], []).append(n)
    for ticker, candidates in by_ticker.items():
        paper_state = [n for n in candidates if n.get("state") == "paper"]
        pick = min(paper_state or candidates, key=lambda n: n["id"])
        nodes.append(_to_node(pick))

    return nodes


def report_live_track(nodes, watchlist_id, start, end, csv):
    print(f"=== Live-track (backtest vs paper) reconciliation: {start} to {end} ===\n")
    rows = []
    if not nodes:
        print(f"(no live-track nodes with real paper trade history on watchlist {watchlist_id})")
    for node in nodes:
        t = node["ticker"]
        paper = get_paper_trades(node["id"], start, end)
        bt_trades = get_backtest_trades_in_window(node, start, end)
        p_closed = paper.dropna(subset=["pnl_pct"])

        bt_rets = [tr["ret"] for tr in bt_trades]
        p_rets = (p_closed["pnl_pct"] / 100.0).tolist()

        bt_win = sum(1 for r in bt_rets if r > 0)
        p_win = sum(1 for r in p_rets if r > 0)
        bt_comp = float(np.prod([1 + r for r in bt_rets]) - 1) if bt_rets else float("nan")
        p_comp = float(np.prod([1 + r for r in p_rets]) - 1) if p_rets else float("nan")

        rows.append({
            "ticker": t, "wl_id": node["id"], "state": node.get("state"),
            "backtest_n": len(bt_rets), "backtest_win_rate": bt_win / len(bt_rets) if bt_rets else float("nan"),
            "backtest_compounded_pct": bt_comp * 100 if bt_rets else float("nan"),
            "paper_n": len(p_rets), "paper_win_rate": p_win / len(p_rets) if p_rets else float("nan"),
            "paper_compounded_pct": p_comp * 100 if p_rets else float("nan"),
            "paper_still_open": len(paper) - len(p_closed),
        })

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 160)
    print(df.round(2).to_string(index=False) if not df.empty else "(no live-track data)")

    if csv and not df.empty:
        OUTPUT_DIR.mkdir(exist_ok=True)
        df.to_csv(OUTPUT_DIR / "paper_vs_backtest_reconcile_livetrack.csv", index=False)
        print(f"\nWrote {OUTPUT_DIR / 'paper_vs_backtest_reconcile_livetrack.csv'}")

    if df.empty:
        return
    print("\n--- Divergence flags (backtest and paper disagree on direction, or trade-count gap >2) ---")
    print("(NOTE: backtest_n excludes a currently-open backtest position -- get_trades_and_bars filters "
          "result==OPEN upstream -- while paper_still_open counts paper's open position. Comparing "
          "backtest_n against paper_n+paper_still_open is asymmetric in the exact direction of 'paper "
          "fires more' (found by Opus review 2026-08-04 very late). Trade-count-gap flag below compares "
          "closed-vs-closed only; paper_still_open is reported for visibility, not counted in the gap.)")
    for _, r in df.iterrows():
        flags = []
        if pd.notna(r["backtest_compounded_pct"]) and pd.notna(r["paper_compounded_pct"]):
            if (r["backtest_compounded_pct"] > 0) != (r["paper_compounded_pct"] > 0):
                flags.append("direction mismatch (backtest and paper disagree on net win/loss for this window)")
        if abs(r["backtest_n"] - r["paper_n"]) > 2:
            flags.append(f"trade-count gap, closed trades only (backtest={r['backtest_n']}, paper={r['paper_n']}, "
                          f"paper also has {r['paper_still_open']} still open)")
        if r["backtest_n"] == 0 and r["paper_n"] > 0:
            flags.append("backtest shows zero closed signals in window but paper closed trades")
        if flags:
            print(f"{r['ticker']}: {'; '.join(flags)}")


def report_daily_track(tickers, daily_track_wl_ids, start, end, csv):
    print(f"\n=== Daily-track reconciliation (from daily_track_reconciliation_log): "
          f"{start} to {end} ===\n")
    if not daily_track_wl_ids:
        print("No daily-track nodes found.")
        return

    csv_rows = []
    for t in tickers:
        wl_id = daily_track_wl_ids.get(t)
        if wl_id is None:
            print(f"{t}: no daily-track node, skipping")
            continue
        log_rows = db.get_daily_track_reconciliation_log(wl_id, limit=1000)
        log_rows = [r for r in log_rows if start <= r["check_date"] <= end]
        if not log_rows:
            print(f"{t} (wl_id={wl_id}): no reconciliation log entries in window")
            continue

        counts = Counter(r["action"] for r in log_rows)
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(f"{t} (wl_id={wl_id}, {len(log_rows)} nights checked): {summary}")

        for r in log_rows:
            csv_rows.append(r)
            if r["action"] in FLAGGED_ACTIONS:
                print(f"    {r['action']:<24} {r['check_date']}  {r.get('detail') or ''}")

    if csv and csv_rows:
        OUTPUT_DIR.mkdir(exist_ok=True)
        pd.DataFrame(csv_rows).to_csv(OUTPUT_DIR / "paper_vs_backtest_reconcile_dailytrack.csv", index=False)
        print(f"\nWrote {OUTPUT_DIR / 'paper_vs_backtest_reconcile_dailytrack.csv'}")


def get_daily_track_wl_ids(watchlist_id, tickers=None):
    """ticker -> wl_id for real v5 daily-track nodes only. version=='v5' excludes
    'v5-overlay-test*' staged combo clones (2026-08-1x) -- without it, this dict
    comprehension (last-row-wins per ticker, no MIN(id) the way load_nodes() uses
    for live-track) silently resolves to whichever staged clone has the highest id
    instead of the real v5 daily-track node, same bug class as
    add_daily_track_paper_nodes.py's version=='v5' filter / drought_detection_test.
    load_nodes's paper_role IS NULL hardening -- found live the same morning the
    staged clones were created."""
    return {
        n["ticker"]: n["id"] for n in db.get_watchlist(watchlist_id)
        if n.get("paper_role") == "daily_sync" and n.get("version") == "v5"
        and (not tickers or n["ticker"] in tickers)
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", default=None)
    parser.add_argument("--watchlist-id", type=int, default=65)
    parser.add_argument("--start", default="2026-07-22")
    parser.add_argument("--end", default=None, help="Default: today")
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()
    end = args.end or pd.Timestamp.now().strftime("%Y-%m-%d")

    live_track_nodes = resolve_live_track_nodes_by_activity(args.watchlist_id, args.tickers)
    daily_track_wl_ids = get_daily_track_wl_ids(args.watchlist_id, args.tickers)
    tickers = args.tickers or sorted({n["ticker"] for n in live_track_nodes} | set(daily_track_wl_ids))

    report_live_track(live_track_nodes, args.watchlist_id, args.start, end, args.csv)
    report_daily_track(tickers, daily_track_wl_ids, args.start, end, args.csv)


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
