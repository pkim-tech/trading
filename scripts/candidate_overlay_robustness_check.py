"""Robustness check for a candidate's overlay result (drought or add-on),
built 2026-08-08 after doing this by hand (ad hoc inline python) for TNA's
drought overlay and being asked to have a real script ready for next time.

Applies the mechanical steps from docs/overlay_parameter_robustness_process.md
that make sense against an already-generated trade list (steps 1 and 3 --
drought/add-on here use a fixed generic config via run_overlay_shim.py, not a
per-ticker parameter search, so there's no fit-half "search" step; the
chronological check here is read-only, splitting the trades that already
exist rather than re-running a search on each half):

1. Chronological split -- are the wins/losses spread across the full history,
   or clustered in one half? A result driven entirely by trades in one half
   is a weaker candidate even if the whole-period number looks fine.
2. Single-trade-removal stress test -- remove the single biggest winner and
   recompute. If the result flips from clearly positive to clearly negative,
   the "edge" is one lucky trade, not a repeatable signal (this is exactly
   what killed TNA's drought overlay: +8.59% with the winner, -18.29%
   without it, on 11 total trades).

Usage:
  .venv/bin/python scripts/candidate_overlay_robustness_check.py TNA --mechanism drought
  .venv/bin/python scripts/candidate_overlay_robustness_check.py TNA --mechanism addon
"""
import argparse
import sqlite3

DB_PATH = "cache/research/trading_universe.db"


def compounded(rets):
    prod = 1.0
    for r in rets:
        prod *= (1 + r)
    return (prod - 1) * 100


def win_rate(rets):
    if not rets:
        return 0.0
    return sum(1 for r in rets if r > 0) / len(rets) * 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--mechanism", choices=["drought", "addon"], required=True)
    ap.add_argument("--node-id", type=int, default=None,
                     help="Specific candidate_nodes.id to check. A ticker can have "
                          "multiple registered nodes (e.g. an old fragile pick and a "
                          "later corrected safe one) -- omitting this pools ALL of "
                          "them together, which is wrong once more than one exists "
                          "(found 2026-08-08: UGL alone had 3). If omitted and more "
                          "than one node exists for this ticker, this script now "
                          "lists them and stops rather than silently pooling.")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    c = conn.cursor()

    if args.node_id is None:
        c.execute("""
            SELECT DISTINCT cn.id, cn.robust_alpha, cn.created_at
            FROM candidate_overlay_results cor
            JOIN candidate_nodes cn ON cn.id = cor.candidate_node_id
            WHERE cn.ticker=? AND cor.mechanism=?
            ORDER BY cn.id
        """, (args.ticker, args.mechanism))
        node_options = c.fetchall()
        if len(node_options) > 1:
            print(f"Multiple candidate nodes exist for {args.ticker}/{args.mechanism} -- "
                  f"pass --node-id to pick one, not pooling them:")
            for nid, ralpha, created in node_options:
                print(f"  id={nid}  robust_alpha={ralpha:.1f}%  created={created}")
            conn.close()
            return
        elif len(node_options) == 1:
            args.node_id = node_options[0][0]

    if args.node_id is not None:
        c.execute("""
            SELECT cor.entry_time, cor.exit_time, cor.exit_reason, cor.ret
            FROM candidate_overlay_results cor
            WHERE cor.candidate_node_id=? AND cor.mechanism=?
            ORDER BY cor.entry_time
        """, (args.node_id, args.mechanism))
    else:
        c.execute("""
            SELECT cor.entry_time, cor.exit_time, cor.exit_reason, cor.ret
            FROM candidate_overlay_results cor
            JOIN candidate_nodes cn ON cn.id = cor.candidate_node_id
            WHERE cn.ticker=? AND cor.mechanism=?
            ORDER BY cor.entry_time
        """, (args.ticker, args.mechanism))
    trades = c.fetchall()
    conn.close()

    if not trades:
        print(f"No {args.mechanism} trades found for {args.ticker}.")
        return

    rets = [t[3] for t in trades]
    print(f"=== {args.ticker} / {args.mechanism} -- {len(trades)} trades ===\n")
    print(f"{'Entry':20} {'Exit':20} {'Reason':8} {'Ret%':>8}")
    for entry, exit_, reason, ret in trades:
        print(f"{entry:20} {exit_:20} {reason:8} {ret*100:>8.2f}")

    print(f"\nWhole-period: compounded={compounded(rets):+.2f}%  win_rate={win_rate(rets):.1f}%  n={len(rets)}")

    # Step 1: chronological split
    mid = len(trades) // 2
    first_half = rets[:mid]
    second_half = rets[mid:]
    print(f"\n--- Chronological split ---")
    print(f"First half  (n={len(first_half)}): compounded={compounded(first_half):+.2f}%  win_rate={win_rate(first_half):.1f}%")
    print(f"Second half (n={len(second_half)}): compounded={compounded(second_half):+.2f}%  win_rate={win_rate(second_half):.1f}%")

    # Step 3: single-trade-removal stress test
    biggest_idx = max(range(len(rets)), key=lambda i: rets[i])
    biggest_ret = rets[biggest_idx]
    without_biggest = rets[:biggest_idx] + rets[biggest_idx + 1:]
    comp_all = compounded(rets)
    comp_without = compounded(without_biggest)
    print(f"\n--- Single-trade-removal stress test ---")
    print(f"Biggest winner: {trades[biggest_idx][0]} ret={biggest_ret*100:+.2f}%")
    print(f"Compounded WITH biggest winner:    {comp_all:+.2f}%")
    print(f"Compounded WITHOUT biggest winner: {comp_without:+.2f}%")
    if (comp_all > 0) != (comp_without > 0):
        print("\n!!! FLIPS SIGN when the single biggest winner is removed -- this is a")
        print("!!! single-trade artifact, not a real edge, per")
        print("!!! docs/overlay_parameter_robustness_process.md step 3. Reject.")
    else:
        print("\nSign holds with the biggest winner removed -- passes this check")
        print("(does not by itself mean the result is real -- see the doc's other steps).")


if __name__ == "__main__":
    main()
