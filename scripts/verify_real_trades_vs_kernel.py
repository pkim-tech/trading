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
no plausible kernel counterpart (no backtest trade within MATCH_TOLERANCE_HOURS) is exactly the shape
of divergence that matters -- e.g. RETL's 2026-08-13 same-bar re-entry (a real trade the kernel
structurally cannot produce, since its per-bar loop enforces a minimum 1-bar gap between an exit and
the next entry-check).

Caveat shared with paper_vs_backtest_reconcile.py: node config is read as it exists NOW, not as it
was at trade time -- a since-edited node will misreport its own trade history.

Only TrailingBothZScoreBreakout/TrailingExitZScoreBreakout nodes are checkable (the only two
get_trades_and_bars_since supports); a real trade from any other strategy is reported as skipped,
not silently dropped.

Design corrections made 2026-08-14 after a paired (independent-cold + contextual) Opus review of the
first version found real bugs -- all fixed here, not just noted:
  1. get_backtest_trades_in_window's returned "entry_time" is actually the SIGNAL bar
     (timestamps[t['signal_i']], see paper_vs_backtest_reconcile.get_trades_and_bars_since) -- for
     TrailingBothZScoreBreakout the real bounce-fill wait can be many hours, so matching a real FILL
     time (trade_log.entry_time) against a kernel SIGNAL time was comparing apples to oranges and
     manufactured false "no kernel counterpart" flags. Now matches on trade_log.signal_time instead.
  2. Still-open real trades (exit_time/pnl_pct NULL) were excluded from matching entirely -- this is
     exactly the shape of the RETL same-bar re-entry the tool exists to catch (the re-entry trade was
     still open when first checked), and it was invisible. Now included in matching (excluded only
     from the closed-trade return/win-rate aggregates, which have nothing to compute for an open trade).
  3. Greedy nearest-time matching is not maximum-cardinality/correct-identity -- an adversarial
     ordering could have an earlier real trade "steal" a kernel trade a later real trade needed more,
     both misreporting which trade is actually unmatched. Replaced with optimal bipartite matching
     (scipy.optimize.linear_sum_assignment on the |delta-hours| cost matrix, tolerance-violating pairs
     excluded post-hoc).
  4. Kernel trades with no real counterpart (a signal the live daemon apparently never acted on) were
     computed and silently discarded -- the other half of "trades must equal backtest." Now reported.
  5. wl_id=-1 (or any wl_id<=0) rows are excluded -- verify_live_parity.py's synthetic replay nodes use
     'id': -1 and, until a real bug in that script's temp-DB isolation was fixed the same day, briefly
     wrote 156 such rows into the real trade_log (see docs/deep_backlog.md's 2026-08-14 entry). This
     filter is deliberately defensive against a repeat, not just a one-time cleanup.
  6. A real trade with a coverage_events 'staged_live_test'/'gap_resize' entry near its signal_time
     (human-staged test order, bypasses the signal path entirely -- e.g. GDXU's 2026-07-27/07-30
     trades) or exit_reason='MANUAL' (a backdated manual catch-up entry, e.g. SOXL/ira wl_id=45) is
     classified as STAGED/MANUAL rather than reported as unexplained kernel divergence -- these are
     real trades that were never supposed to be signal-driven in the first place, and treating them
     identically to genuine divergence trains the reader to ignore the one flag that matters. This
     match is approximate (coverage_events' staged_live_test rows don't carry node_id, only ticker --
     joined on ticker + a time window around signal_time, not a precise position_id join) -- flagged
     as approximate in the report, not asserted as certain.

Usage: .venv/bin/python scripts/verify_real_trades_vs_kernel.py [--tickers ...] [--accounts ...]
       [--min-notional 0] [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--match-tolerance-hours 4] [--csv]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import signals_db as db
from scripts.paper_vs_backtest_reconcile import get_backtest_trades_in_window

LIVE_DB = Path("cache/live/trading_live.db")
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

SUPPORTED_STRATEGIES = {"TrailingBothZScoreBreakout", "TrailingExitZScoreBreakout"}

# How close a real trade's SIGNAL time must land to a kernel-predicted signal time to count as
# "the same trade" -- generous relative to a 1h bar (a real signal can be detected a poll cycle
# or two late), but tight enough that a same-bar re-entry the kernel could never produce (the RETL
# shape) still shows up unmatched rather than accidentally pairing with an unrelated kernel trade.
DEFAULT_MATCH_TOLERANCE_HOURS = 4

# How far around a real trade's ENTRY (fill) time to look for a staged/manual coverage_events
# marker. Anchored to entry_time, not signal_time -- checked directly against real data
# (2026-08-14): a staged_live_test event can land ~8h before the eventual entry_time (a pre-market
# order that fills at the next open_check), and a gap_resize event lands the same day as entry_time
# but can be days after signal_time (the real bounce-fill wait for a TrailingBoth node), so
# signal_time was the wrong anchor -- it produced a false "genuinely unmatched" on GDXU's own
# already-explained staged trade in this script's first version.
STAGED_TEST_WINDOW_HOURS = 24

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
    paper_vs_backtest_reconcile.get_paper_trades. wl_id > 0 excludes synthetic replay-harness
    rows (see module docstring point 5) -- a real watch_list PK is always a positive autoincrement
    id, so this costs nothing on genuine data."""
    con = sqlite3.connect(LIVE_DB)
    con.row_factory = sqlite3.Row
    q = """
        SELECT wl_id, ticker, account, strategy, signal_time, entry_time, exit_time, pnl_pct, exit_reason
        FROM trade_log
        WHERE is_dry_run_sim = 0 AND position_source = 'core' AND wl_id IS NOT NULL AND wl_id > 0
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


def is_staged_or_manual(ticker, entry_time, exit_reason):
    """Approximate classification, not a precise join -- see module docstring point 6.
    exit_reason='MANUAL' is a direct, reliable signal (that column is real trade_log data).
    The coverage_events check is a heuristic: staged_live_test rows carry ticker but not
    node_id, so this joins on ticker + a window around entry_time (the real fill) rather than an
    exact position_id match (trade_log itself has no position_id column to join on post-close)."""
    if exit_reason == "MANUAL":
        return True
    con = sqlite3.connect(LIVE_DB)
    row = con.execute(
        """SELECT 1 FROM coverage_events
           WHERE ticker = ? AND scenario_key IN ('staged_live_test', 'gap_resize')
             AND ts BETWEEN datetime(?, ?) AND datetime(?, ?) LIMIT 1""",
        (ticker, entry_time, f"-{STAGED_TEST_WINDOW_HOURS} hours",
         entry_time, f"+{STAGED_TEST_WINDOW_HOURS} hours"),
    ).fetchone()
    con.close()
    return row is not None


def resolve_nodes(wl_ids, min_notional):
    nodes = {}
    skipped = {}
    for wl_id in wl_ids:
        row = db.get_watch_list_node_by_id(wl_id)
        if not row:
            skipped[wl_id] = "node lookup returned nothing (removed, or a transient DB error -- " \
                              "get_watch_list_node_by_id swallows exceptions, can't distinguish here)"
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
    """Optimal (maximum-cardinality, minimum-total-delta) bipartite matching between real and
    kernel trades by signal time -- NOT greedy nearest-first, which can misassign under
    adversarial orderings (found by cold review 2026-08-14, see module docstring point 3).
    Returns (matched pairs with delta_hours, unmatched real trades, unmatched kernel trades)."""
    if not real_trades or not bt_trades:
        return [], list(real_trades), list(bt_trades)

    n, m = len(real_trades), len(bt_trades)
    BIG = 1e6
    cost = np.full((n, m), BIG)
    for i, rt in enumerate(real_trades):
        for j, bt in enumerate(bt_trades):
            delta = abs((pd.Timestamp(rt["signal_time"]) - pd.Timestamp(bt["entry_time"])).total_seconds()) / 3600.0
            if delta <= tolerance_hours:
                cost[i, j] = delta

    row_ind, col_ind = linear_sum_assignment(cost)
    matched, used_real, used_bt = [], set(), set()
    for i, j in zip(row_ind, col_ind):
        if cost[i, j] < BIG:
            matched.append((real_trades[i], bt_trades[j], cost[i, j]))
            used_real.add(i)
            used_bt.add(j)

    unmatched_real = [rt for i, rt in enumerate(real_trades) if i not in used_real]
    unmatched_bt = [bt for j, bt in enumerate(bt_trades) if j not in used_bt]
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
    missed_kernel_detail = []
    for wl_id, node in sorted(nodes.items()):
        real = get_real_trades(start, end, tickers=[node["ticker"]], accounts=[node["account"]] if node["account"] else None)
        real = [r for r in real if r["wl_id"] == wl_id]
        real_closed = [r for r in real if r["pnl_pct"] is not None]
        bt_trades = get_backtest_trades_in_window(node, start, end)

        real_rets = [r["pnl_pct"] / 100.0 for r in real_closed]
        bt_rets = [t["ret"] for t in bt_trades]

        # Matching uses ALL real trades (incl. still-open) against kernel signal times -- an
        # open real trade with no kernel counterpart is just as much a real finding as a closed
        # one (module docstring point 2).
        real_for_matching = [{"signal_time": r["signal_time"], "entry_time": r["entry_time"],
                               "ret": r["pnl_pct"] / 100.0 if r["pnl_pct"] is not None else None,
                               "ticker": r["ticker"], "exit_reason": r["exit_reason"]}
                              for r in real]
        bt_for_matching = [{"entry_time": str(t["entry_time"]), "ret": t["ret"]} for t in bt_trades]
        matched, unmatched_real, unmatched_bt = match_trades(real_for_matching, bt_for_matching, tolerance_hours)

        genuine_unmatched, staged_unmatched = [], []
        for r in unmatched_real:
            if is_staged_or_manual(r["ticker"], r["entry_time"], r["exit_reason"]):
                staged_unmatched.append(r)
            else:
                genuine_unmatched.append(r)

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
            "unmatched_real_genuine": len(genuine_unmatched),
            "unmatched_real_staged": len(staged_unmatched),
            "kernel_trades_missed_by_real": len(unmatched_bt),
        })
        for r in genuine_unmatched:
            unmatched_detail.append({"ticker": node["ticker"], "account": node["account"], "wl_id": wl_id,
                                      "signal_time": r["signal_time"], "ret": r["ret"]})
        for bt in unmatched_bt:
            missed_kernel_detail.append({"ticker": node["ticker"], "account": node["account"], "wl_id": wl_id,
                                          "kernel_signal_time": bt["entry_time"], "ret": bt["ret"]})

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 180)
    print(df.round(2).to_string(index=False) if not df.empty else "(no real core trades in window for checkable nodes)")

    if csv and not df.empty:
        OUTPUT_DIR.mkdir(exist_ok=True)
        df.to_csv(OUTPUT_DIR / "verify_real_trades_vs_kernel.csv", index=False)
        print(f"\nWrote {OUTPUT_DIR / 'verify_real_trades_vs_kernel.csv'}")

    if not df.empty:
        print("\n--- Divergence flags (unmatched_real_staged is informational, not a divergence) ---")
        for _, r in df.iterrows():
            flags = []
            if r["unmatched_real_genuine"] > 0:
                flags.append(f"{int(r['unmatched_real_genuine'])} real trade(s) with NO kernel counterpart "
                              f"within {tolerance_hours}h and no staged/manual marker -- the kernel could not "
                              f"have produced this trade sequence")
            if r["unmatched_real_staged"] > 0:
                flags.append(f"({int(r['unmatched_real_staged'])} more flagged staged/manual test order(s), "
                              f"not counted as divergence)")
            if r["kernel_trades_missed_by_real"] > 0:
                flags.append(f"kernel predicted {int(r['kernel_trades_missed_by_real'])} trade(s) real never took")
            if pd.notna(r["real_compounded_pct"]) and pd.notna(r["backtest_compounded_pct"]):
                if (r["real_compounded_pct"] > 0) != (r["backtest_compounded_pct"] > 0):
                    flags.append("direction mismatch (real and kernel disagree on net win/loss for this window)")
            if flags:
                print(f"{r['ticker']}/{r['account']} (wl_id={r['wl_id']}): {'; '.join(flags)}")

    if unmatched_detail:
        print("\n--- UNEXPLAINED: real trades with no kernel counterpart (not staged/manual) ---")
        for u in unmatched_detail:
            print(f"  {u['ticker']}/{u['account']} (wl_id={u['wl_id']}): signalled {u['signal_time']}, ret={u['ret']}")

    if missed_kernel_detail:
        print("\n--- Kernel signals the real daemon never acted on ---")
        for m in missed_kernel_detail:
            print(f"  {m['ticker']}/{m['account']} (wl_id={m['wl_id']}): kernel signal {m['kernel_signal_time']}, ret={m['ret']}")


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
