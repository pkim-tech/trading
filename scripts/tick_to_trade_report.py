"""Read-only tick-to-trade latency report for entry_timing='open_check' nodes --
joins two ALREADY-LOGGED real timestamps (no changes to the live trading code):
`open_price_quality_log` (the real tick -- when the pinned check actually fetched
the Schwab session-open/current price for a target moment, e.g. 14:30) against
`coverage_events` (the real trade outcome -- automated_buy_execution's 'placed'
result, or any of check_order's SafetyViolation block scenario_keys, e.g.
buy_signal_window_block/daily_order_cap_block/global_burst_cap_block/same_day_block).

Built 2026-09-17 after manually reconstructing this exact join by hand to
diagnose ETHU's missed 2026-09-15 14:30 entry (blocked by buy_signal_window_block,
~90s after its own window closed) and DPST/OILU's multi-minute-late-but-still-
inside-window near-misses on 2026-09-16 -- this formalizes that reconstruction so
it doesn't have to be redone by hand next time. Both source tables store `ts` in
UTC (SQLite's `datetime('now')` default); this script keeps everything in UTC
internally and only converts to ET for display.

Deliberately NOT wired into active_signals.py/signals_notify.py -- this is a
standalone diagnostic, not a change to the live trading path. Matching is
best-effort (ticker + nearest coverage_event within MATCH_WINDOW_SECS after the
price-fetch ts), not node_id-exact, since open_price_quality_log doesn't record
node_id -- acceptable for a reporting tool, not a safety-critical decision.

Usage:
  .venv/bin/python scripts/tick_to_trade_report.py                  # last 3 days
  .venv/bin/python scripts/tick_to_trade_report.py --since 2026-09-15
  .venv/bin/python scripts/tick_to_trade_report.py --since 2026-09-15 --flag-over 30
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_config as cfg

MATCH_WINDOW_SECS = 15 * 60  # a pinned check's own retry loop is <=3 attempts, 5s apart -- 15min is generous
DEFAULT_FLAG_OVER_SECS = 30

OUTCOME_SCENARIO_KEYS = (
    "automated_buy_execution",
    "buy_signal_window_block",
    "daily_order_cap_block",
    "global_burst_cap_block",
    "same_day_block",
    "hard_order_ceiling_block",
)
# same_day_block's 'skipped_margin_account' result is informational, not an actual
# block (margin accounts aren't stopped by it) -- including it let the join match
# an irrelevant same-ticker log line instead of the real outcome a few seconds later.
NON_BLOCKING_RESULTS = ("skipped_margin_account",)


def _fetch(con, since):
    ticks = con.execute(
        "SELECT ts, ticker, target_h, target_m, price, is_true_open "
        "FROM open_price_quality_log WHERE ts >= ? ORDER BY ts",
        (since,),
    ).fetchall()
    placeholders = ",".join("?" for _ in OUTCOME_SCENARIO_KEYS)
    outcomes = con.execute(
        f"SELECT ts, ticker, scenario_key, result, detail, node_id FROM coverage_events "
        f"WHERE scenario_key IN ({placeholders}) AND ts >= ? AND mode='live' "
        f"AND result NOT IN ({','.join('?' for _ in NON_BLOCKING_RESULTS)}) ORDER BY ts",
        (*OUTCOME_SCENARIO_KEYS, since, *NON_BLOCKING_RESULTS),
    ).fetchall()
    return ticks, outcomes


def _to_et(ts_str):
    # Both tables store naive UTC ('now' default) -- fixed -4h display offset
    # (EDT). Good enough for a diagnostic report; not meant for DST-boundary days.
    return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S") - timedelta(hours=4)


def build_report(con, since):
    """Returns (tick, [(delta, outcome), ...]) -- ALL live outcomes within
    MATCH_WINDOW_SECS of each tick, not just a single "best match". A ticker with
    2+ concurrently active live open_check nodes (happened with DPST's ira+soxl_ira
    pair, 2026-09-16) can legitimately produce 2+ real, DISTINCT outcomes off the
    SAME shared tick (open_price_quality_log logs one tick per ticker, not per
    node -- one real Schwab quote, reused for every node on that ticker) -- that's
    not ambiguous, it's just two separate real events, each with its own node_id
    already on the coverage_events row. Picking a single "nearest" winner would
    silently drop a real event; report every one instead."""
    ticks, outcomes = _fetch(con, since)
    outcomes_by_ticker = {}
    for o in outcomes:
        outcomes_by_ticker.setdefault(o["ticker"], []).append(o)

    rows = []
    for t in ticks:
        tick_dt = datetime.strptime(t["ts"], "%Y-%m-%d %H:%M:%S")
        candidates = outcomes_by_ticker.get(t["ticker"], [])
        in_window = []
        for o in candidates:
            o_dt = datetime.strptime(o["ts"], "%Y-%m-%d %H:%M:%S")
            delta = (o_dt - tick_dt).total_seconds()
            if 0 <= delta <= MATCH_WINDOW_SECS:
                in_window.append((delta, o))
        in_window.sort(key=lambda x: x[0])
        rows.append((t, in_window))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="YYYY-MM-DD (ET) -- defaults to 3 days ago")
    ap.add_argument("--flag-over", type=float, default=DEFAULT_FLAG_OVER_SECS,
                     help=f"seconds -- flag rows at/above this latency (default {DEFAULT_FLAG_OVER_SECS})")
    args = ap.parse_args()

    since_dt = (datetime.strptime(args.since, "%Y-%m-%d") if args.since
                else datetime.now() - timedelta(days=3))
    # since_dt is an ET calendar date boundary; stored ts is UTC, so push back 5h
    # to make sure we don't clip the first few UTC hours of that ET day.
    since_utc = (since_dt - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")

    con = sqlite3.connect(cfg.DB_PATH)
    con.row_factory = sqlite3.Row
    rows = build_report(con, since_utc)

    print(f"=== Tick-to-trade latency report, open_check pinned entries since {args.since or since_dt.date()} ===\n")
    if not rows:
        print("No pinned price-fetch rows in range.")
        return

    unmatched = 0
    n_outcomes = 0
    flagged = []
    for t, matches in sorted(rows, key=lambda r: r[0]["ts"]):
        tick_et = _to_et(t["ts"])
        target = f"{t['target_h']:02d}:{t['target_m']:02d}"
        if not matches:
            print(f"{tick_et:%Y-%m-%d %H:%M:%S} ET  {t['ticker']:6s} target={target}  "
                  f"price={t['price']:.4f}  -> NO MATCHING OUTCOME within {MATCH_WINDOW_SECS}s "
                  f"(no signal fired, or fired outside this window)")
            unmatched += 1
            continue
        for delta, o in matches:
            n_outcomes += 1
            outcome_label = o["result"] if o["scenario_key"] == "automated_buy_execution" else f"BLOCKED:{o['scenario_key']}"
            flag = " <<< SLOW" if delta >= args.flag_over else ""
            multi = f" [node_id={o['node_id']}]" if len(matches) > 1 else ""
            print(f"{tick_et:%Y-%m-%d %H:%M:%S} ET  {t['ticker']:6s} target={target}  "
                  f"price={t['price']:.4f}  -> {outcome_label:30s} latency={delta:6.1f}s{multi}{flag}")
            if delta >= args.flag_over:
                flagged.append((t, delta, outcome_label, o["node_id"]))

    print(f"\n{len(rows)} pinned ticks, {n_outcomes} real live outcome(s) matched "
          f"({len(rows) - unmatched} tick(s) with at least one), "
          f"{unmatched} tick(s) unmatched (no BUY signal fired at/after that tick).")
    if flagged:
        print(f"\n{len(flagged)} outcome(s) at/over {args.flag_over:.0f}s latency:")
        for t, delta, outcome_label, node_id in flagged:
            print(f"  {t['ticker']:6s} {t['target_h']:02d}:{t['target_m']:02d}  {delta:.1f}s  "
                  f"{outcome_label}  node_id={node_id}")
    else:
        print(f"\nNo outcomes at/over {args.flag_over:.0f}s.")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
