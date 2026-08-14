"""
The real North Star instrument for "trades must equal backtest" (docs/plans/account_checkin_process.md,
2026-08-14) -- neither existing tool actually answers this question:

- scripts/verify_live_parity.py compares the kernel against active_signals.py's live-orchestration
  FUNCTIONS, replayed against cached historical CSV data. It never reads a real trade_log row, and
  its single-bar signal-then-immediate-open replay loop structurally can't represent
  TrailingBothZScoreBreakout's real resting-order bounce-fill mechanic (the live default strategy) --
  see that script's docstring for the corrected, current framing.
- scripts/paper_vs_backtest_reconcile.py reconciles PAPER activity only, by design (paper_trading.py
  never touches schwab_client) -- not a gap, deliberately scoped.

This script reads real, broker-executed trades (trade_log, is_dry_run_sim=0, position_source='core'
-- excludes drought/add-on legs, which have no kernel-replay path yet, and dry-run-synthesized fills,
which aren't real broker executions) and replays the SAME kernel machinery
paper_vs_backtest_reconcile.py already uses (get_trades_and_bars_since/get_backtest_trades_in_window
-- imported, not reimplemented) against each node's real config, over the real historical price data
covering the same window. The kernel is ground truth for what SHOULD have happened; a real trade with
no plausible kernel counterpart (no backtest trade within MATCH_TOLERANCE_HOURS of its entry) is
exactly the shape of divergence that matters -- e.g. RETL's 2026-08-13 same-bar re-entry (a real trade
the kernel structurally cannot produce, since its per-bar loop enforces a minimum 1-bar gap between
an exit and the next entry-check).

Caveat shared with paper_vs_backtest_reconcile.py: node config is read as it exists NOW, not as it
was at trade time -- a since-edited node will misreport its own trade history. Same accepted
limitation, not fixed here.

Only TrailingBothZScoreBreakout/TrailingExitZScoreBreakout nodes are checkable (the only two
get_trades_and_bars_since supports); a real trade from any other strategy is reported as skipped,
not silently dropped.

Usage: .venv/bin/python scripts/verify_real_trades_vs_kernel.py [--tickers ...] [--accounts ...]
       [--min-notional 0] [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--match-tolerance-hours 4] [--csv]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import signals_db as db
from scripts.paper_vs_backtest_reconcile import get_backtest_trades_in_window

LIVE_DB = Path("cache/live/trading_live.db")
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

SUPPORTED_STRATEGIES = {"TrailingBothZScoreBreakout", "TrailingExitZScoreBreakout"}

# How close a real trade's entry must land to a kernel-predicted entry to count as "the same
# trade" -- generous relative to a 1h bar (real fills can drift from the idealized trigger via
# the trailing-buy bounce, manual catch-up entries, etc.), but tight enough that a same-bar
# re-entry the kernel could never produce (the RETL shape) still shows up unmatched rather than
# accidentally pairing with an unrelated nearby kernel trade.
DEFAULT_MATCH_TOLERANCE_HOURS = 4

NODE_COLS = ["id", "ticker", "strategy", "window", "z_score_threshold", "arm_sell_pct",
             "take_profit", "fixed_sl", "trail_buy_pct", "trail_sell_pct", "max_hold_hours",
             "entry_timing", "state", "added_at", "account", "starting_notional"]


def _to_node(row):
    node = {c: row.get(c) for c in NODE_COLS}
    node["z"] = node.pop("z_score_threshold")
    node["arm_pct"] = node["arm_sell_pct"] if node["strategy"] == "TrailingBothZScoreBreakout" else node["take_profit"]
    return node


def get_real_trades(start, end, tickers=None, accounts=None):
    """end is a YYYY-MM-DD date; extended to end-of-day, same lexical-truncation fix as
    paper_vs_backtest_reconcile.get_paper_trades."""
    con = sqlite3.connect(LIVE_DB)
    con.row_factory = sqlite3.Row
    q = """
        SELECT wl_id, ticker, account, strategy, entry_time, exit_time, pnl_pct, exit_reason
        FROM trade_log
        WHERE is_dry_run_sim = 0 AND position_source = 'core' AND wl_id IS NOT NULL
          AND entry_time >= ? AND entry_time <= ?
    """
    params = [start, f"{end} 23:59:59"]
    if tickers:
        q += f" AND ticker IN ({','.join('?' * len(tickers))})"
        params += tickers
    if accounts:
        q += f" AND account IN ({','.join('?' * len(accounts))})"
        params += accounts
    rows = con.execute(q, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


def resolve_nodes(wl_ids, min_notional):
    nodes = {}
    skipped = {}
    for wl_id in wl_ids:
        row = db.get_watch_list_node_by_id(wl_id)
        if not row:
            skipped[wl_id] = "node no longer exists"
            continue
        if (row.get("starting_notional") or 0) < min_notional:
            skipped[wl_id] = f"starting_notional below --min-notional ({row.get('starting_notional')})"
            continue
        if row["strategy"] not in SUPPORTED_STRATEGIES:
            skipped[wl_id] = f"strategy '{row['strategy']}' has no kernel-replay path yet"
            continue
        nodes[wl_id] = _to_node(row)
    return nodes, skipped


def match_trades(real_trades, bt_trades, tolerance_hours):
    """Greedy nearest-entry-time matching, real trades sorted chronologically.  Returns
    (matched pairs, unmatched real trades, unmatched backtest trades) -- unmatched real trades
    are the actionable finding (a real fill the kernel has no counterpart for at all)."""
    bt_pool = sorted(bt_trades, key=lambda t: t["entry_time"])
    used = [False] * len(bt_pool)
    matched, unmatched_real = [], []

    for rt in sorted(real_trades, key=lambda t: t["entry_time"]):
        best_i, best_delta = None, None
        for i, bt in enumerate(bt_pool):
            if used[i]:
                continue
            delta = abs((pd.Timestamp(rt["entry_time"]) - pd.Timestamp(bt["entry_time"])).total_seconds()) / 3600.0
            if delta <= tolerance_hours and (best_delta is None or delta < best_delta):
                best_i, best_delta = i, delta
        if best_i is not None:
            used[best_i] = True
            matched.append((rt, bt_pool[best_i], best_delta))
        else:
            unmatched_real.append(rt)

    unmatched_bt = [bt for i, bt in enumerate(bt_pool) if not used[i]]
    return matched, unmatched_real, unmatched_bt


def report(nodes, skipped, start, end, tolerance_hours, csv):
    print(f"=== Real trades vs kernel replay: {start} to {end} (match tolerance {tolerance_hours}h) ===\n")
    if skipped:
        print("Skipped nodes (not checkable):")
        for wl_id, why in skipped.items():
            print(f"  wl_id={wl_id}: {why}")
        print()

    rows = []
    unmatched_detail = []
    for wl_id, node in sorted(nodes.items()):
        real = get_real_trades(start, end, tickers=[node["ticker"]], accounts=[node["account"]] if node["account"] else None)
        real = [r for r in real if r["wl_id"] == wl_id]
        real_closed = [r for r in real if r["pnl_pct"] is not None]
        bt_trades = get_backtest_trades_in_window(node, start, end)

        real_rets = [r["pnl_pct"] / 100.0 for r in real_closed]
        bt_rets = [t["ret"] for t in bt_trades]

        matched, unmatched_real, unmatched_bt = match_trades(
            [{"entry_time": r["entry_time"], "ret": r["pnl_pct"] / 100.0 if r["pnl_pct"] is not None else None}
             for r in real_closed],
            [{"entry_time": str(t["entry_time"]), "ret": t["ret"]} for t in bt_trades],
            tolerance_hours,
        )

        real_win = sum(1 for r in real_rets if r > 0)
        bt_win = sum(1 for r in bt_rets if r > 0)
        real_comp = float(np.prod([1 + r for r in real_rets]) - 1) if real_rets else float("nan")
        bt_comp = float(np.prod([1 + r for r in bt_rets]) - 1) if bt_rets else float("nan")

        rows.append({
            "ticker": node["ticker"], "account": node["account"], "wl_id": wl_id,
            "real_n": len(real_rets), "real_win_rate": real_win / len(real_rets) if real_rets else float("nan"),
            "real_compounded_pct": real_comp * 100 if real_rets else float("nan"),
            "backtest_n": len(bt_rets), "backtest_win_rate": bt_win / len(bt_rets) if bt_rets else float("nan"),
            "backtest_compounded_pct": bt_comp * 100 if bt_rets else float("nan"),
            "still_open": len(real) - len(real_closed),
            "unmatched_real_trades": len(unmatched_real),
        })
        for r in unmatched_real:
            unmatched_detail.append({"ticker": node["ticker"], "account": node["account"],
                                      "wl_id": wl_id, "entry_time": r["entry_time"], "ret": r["ret"]})

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 160)
    print(df.round(2).to_string(index=False) if not df.empty else "(no real core trades in window for checkable nodes)")

    if csv and not df.empty:
        OUTPUT_DIR.mkdir(exist_ok=True)
        df.to_csv(OUTPUT_DIR / "verify_real_trades_vs_kernel.csv", index=False)
        print(f"\nWrote {OUTPUT_DIR / 'verify_real_trades_vs_kernel.csv'}")

    if not df.empty:
        print("\n--- Divergence flags ---")
        for _, r in df.iterrows():
            flags = []
            if r["unmatched_real_trades"] > 0:
                flags.append(f"{int(r['unmatched_real_trades'])} real trade(s) with NO kernel counterpart "
                              f"within {tolerance_hours}h -- the kernel could not have produced this trade sequence")
            if pd.notna(r["real_compounded_pct"]) and pd.notna(r["backtest_compounded_pct"]):
                if (r["real_compounded_pct"] > 0) != (r["backtest_compounded_pct"] > 0):
                    flags.append("direction mismatch (real and kernel disagree on net win/loss for this window)")
            if abs(r["real_n"] - r["backtest_n"]) > 2:
                flags.append(f"trade-count gap (real={int(r['real_n'])}, kernel={int(r['backtest_n'])})")
            if flags:
                print(f"{r['ticker']}/{r['account']} (wl_id={r['wl_id']}): {'; '.join(flags)}")

    if unmatched_detail:
        print("\n--- Unmatched real trades (no kernel counterpart) ---")
        for u in unmatched_detail:
            print(f"  {u['ticker']}/{u['account']} (wl_id={u['wl_id']}): entered {u['entry_time']}, ret={u['ret']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", default=None)
    parser.add_argument("--accounts", nargs="*", default=None)
    parser.add_argument("--min-notional", type=float, default=0,
                         help="Only check nodes with starting_notional >= this (e.g. 5000 for "
                              "capital-at-stake-only scope, matching CAPITAL_AT_STAKE_THRESHOLD).")
    parser.add_argument("--start", default=None, help="Default: earliest real trade in scope.")
    parser.add_argument("--end", default=None, help="Default: today")
    parser.add_argument("--match-tolerance-hours", type=float, default=DEFAULT_MATCH_TOLERANCE_HOURS)
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()

    end = args.end or pd.Timestamp.now().strftime("%Y-%m-%d")
    probe_start = args.start or "2020-01-01"
    real_trades = get_real_trades(probe_start, end, tickers=args.tickers, accounts=args.accounts)
    if not real_trades:
        print("(no real core trades found in scope)")
        return
    start = args.start or min(r["entry_time"] for r in real_trades)[:10]

    wl_ids = sorted({r["wl_id"] for r in real_trades})
    nodes, skipped = resolve_nodes(wl_ids, args.min_notional)
    report(nodes, skipped, start, end, args.match_tolerance_hours, args.csv)


if __name__ == "__main__":
    main()
