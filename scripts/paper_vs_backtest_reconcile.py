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
from scripts.drought_overlay_test import load_nodes, get_trades_and_bars

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


def get_backtest_trades_in_window(node, start, end):
    """end is a YYYY-MM-DD date; pd.Timestamp(end) is midnight, so extend by a day or every
    trade signaling ON the end date is silently excluded -- same class of bug as
    get_paper_trades' date-string truncation."""
    trades, df_h = get_trades_and_bars(node)
    out = []
    end_bound = pd.Timestamp(end) + pd.Timedelta(days=1)
    for t in trades:
        if t["signal_i"] is None:
            continue
        et = df_h.index[t["signal_i"]]
        if pd.Timestamp(start) <= et <= end_bound:
            out.append({"entry_time": et, "ret": t["ret"], "result": t["result"]})
    return out


def report_live_track(tickers, live_track_nodes, watchlist_id, start, end, csv):
    print(f"=== Live-track (backtest vs paper) reconciliation: {start} to {end} ===\n")
    rows = []
    for t in tickers:
        node = live_track_nodes.get(t)
        if node is None:
            print(f"{t}: no live-track (paper_role IS NULL) research node on watchlist "
                  f"{watchlist_id}, skipping")
            continue
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
            "ticker": t,
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", default=None)
    parser.add_argument("--watchlist-id", type=int, default=65)
    parser.add_argument("--start", default="2026-07-22")
    parser.add_argument("--end", default=None, help="Default: today")
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()
    end = args.end or pd.Timestamp.now().strftime("%Y-%m-%d")

    live_track_nodes = {n["ticker"]: n for n in load_nodes(args.watchlist_id, args.tickers)}
    daily_track_wl_ids = {
        n["ticker"]: n["id"] for n in db.get_watchlist()
        if n.get("watchlist_id") == args.watchlist_id and n.get("paper_role") == "daily_sync"
        and n.get("version") == "v5"  # excludes 'v5-overlay-test*' staged combo clones (2026-08-1x) --
        # without this, this dict comprehension (last-row-wins per ticker, no MIN(id) the way
        # load_nodes() uses for live-track) silently resolves to whichever staged clone has the
        # highest id instead of the real v5 daily-track node, same bug class as
        # add_daily_track_paper_nodes.py's version=='v5' filter / drought_detection_test.load_nodes's
        # paper_role IS NULL hardening -- found live the same morning the staged clones were created.
        and (not args.tickers or n["ticker"] in args.tickers)
    }
    tickers = args.tickers or sorted(set(live_track_nodes) | set(daily_track_wl_ids))

    report_live_track(tickers, live_track_nodes, args.watchlist_id, args.start, end, args.csv)
    report_daily_track(tickers, daily_track_wl_ids, args.start, end, args.csv)


if __name__ == "__main__":
    main()
