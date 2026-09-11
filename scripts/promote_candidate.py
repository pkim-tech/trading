"""Generic candidate-to-live(/paper/dry_run) promotion tool (2026-09-10), replacing
the one-off promote_v6_*.py pattern (promote_v6_2026_08_23_batch1.py,
promote_v6_2026_08_24_ethu_oilu.py) -- built after hand-deriving the strategy-
conditional axis mapping for a real SOXL promotion (scripts/promote_soxl_2026_09_10_
50073.py) surfaced that the same reverse-engineering (TB's take_profit really means
arm_sell_pct, TE's means take_profit directly; trail_buy_pct is real for TB, absent
for TE) shouldn't have to be re-derived by hand every time.

Axis mapping (2026-09-10, moved into strategies.py as a real schema reference after
this script's first version hardcoded a params.get('arm_pct', params.get('take_profit'))
guess -- see strategies.BaseStrategy.params_take_profit_key's own comment for why that
belongs there, not here): strategies.params_json_take_profit_key(strategy) returns the
real candidate_nodes.params_json key ('arm_pct' for TB/TrailingBuy, 'take_profit' for
TE/most others) for this script to read. trail_buy_pct/trail_pct/fixed_sl are read
directly by the same key name in both strategies (trail_buy_pct is simply absent from
TE's params_json, which read as None here -- add_node treats that as 0.0/not-applicable).
Requires params_json to exist; refuses rather than guess a flat-column fallback for an
older pre-params_json row.

Safety: refuses to archive --old-wl-id if it has any real open_positions or
pending_buys rows (docs/watchlist_candidate_checklist.md check 17 -- a real,
documented gap in past promotion passes, e.g. 2026-08-19's SOXL/DPST resting-
order miss) -- pass --force only if you've confirmed by hand that proceeding
anyway is correct (e.g. the position is being intentionally carried through).
--dry-run prints the full derived plan (mapping, label, old-node status) with
zero DB writes.

addon_enabled/drought_overlay_enabled default to 0/unset -- pass --addon/--drought
explicitly rather than silently inheriting a default; this project's history
(SOXL 2026-09-10) is to cross-check a newly-lifted overlay mechanism before
flipping it on, not enable it by default.

Usage:
  .venv/bin/python scripts/promote_candidate.py --candidate-id 50073 --account ira \\
      --notional 10000 [--old-wl-id 249] [--addon] [--drought] [--label-suffix TEXT] \\
      [--state live] [--watchlist-id 65] [--dry-run] [--force]
"""
import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import signals_db  # noqa: E402
import strategies  # noqa: E402

RESEARCH_DB_PATH = "cache/research/trading_universe.db"


def load_candidate(candidate_id):
    conn = sqlite3.connect(RESEARCH_DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, ticker, strategy, version, window, entry_timing, fixed_sl, "
        "max_hold_hours, z AS z_score_threshold, params_json FROM candidate_nodes WHERE id=?",
        (candidate_id,)).fetchone()
    conn.close()
    if row is None:
        raise SystemExit(f"candidate_nodes id={candidate_id} not found")
    node = dict(row)
    if not node.get("params_json"):
        raise SystemExit(
            f"candidate_id={candidate_id} has no params_json (older pre-params_json row) -- "
            f"this tool requires it rather than guess a flat-column axis mapping. For an "
            f"older row, derive the promote_v6_*.py-style call by hand instead.")
    node["params"] = json.loads(node["params_json"])
    return node


def derive_axis_args(strategy_name, params):
    """Returns (take_profit, trail_buy_pct, trail_pct, fixed_sl) for add_node --
    the take_profit key is resolved via strategies.params_json_take_profit_key(),
    the real schema reference, not guessed here."""
    tp_key = strategies.params_json_take_profit_key(strategy_name)
    take_profit = params.get(tp_key)
    if take_profit is None:
        raise SystemExit(f"params_json missing {tp_key!r} (strategy={strategy_name!r}'s "
                          f"real take_profit key per strategies.params_json_take_profit_key): {params}")
    trail_buy_pct = params.get("trail_buy_pct")
    trail_pct = params.get("trail_pct")
    fixed_sl = params.get("fixed_sl")
    if fixed_sl is None:
        raise SystemExit(f"params_json missing 'fixed_sl': {params}")
    return take_profit, trail_buy_pct, trail_pct, fixed_sl


def check_old_node_clear(old_wl_id, force):
    if old_wl_id is None:
        return
    conn = sqlite3.connect("cache/live/trading_live.db", timeout=15)
    n_pos = conn.execute("SELECT COUNT(*) FROM open_positions WHERE wl_id=?",
                          (old_wl_id,)).fetchone()[0]
    n_pending = conn.execute("SELECT COUNT(*) FROM pending_buys WHERE wl_id=?",
                              (old_wl_id,)).fetchone()[0]
    conn.close()
    if (n_pos or n_pending) and not force:
        raise SystemExit(
            f"REFUSING: old wl_id={old_wl_id} has {n_pos} open position(s) and "
            f"{n_pending} pending buy(s) -- checklist item 17. Let it resolve first, "
            f"or pass --force if you've confirmed proceeding anyway is correct.")
    return n_pos, n_pending


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate-id", type=int, required=True)
    ap.add_argument("--account", required=True)
    ap.add_argument("--notional", type=float, required=True)
    ap.add_argument("--old-wl-id", type=int, default=None,
                     help="archive this wl_id (checklist 17 gate applies)")
    ap.add_argument("--watchlist-id", type=int, default=None,
                     help="default: the currently active watchlist")
    ap.add_argument("--state", default="live", choices=["live", "paper", "dry_run"])
    ap.add_argument("--addon", action="store_true", help="set addon_enabled=1")
    ap.add_argument("--drought", action="store_true", help="set drought_overlay_enabled=1")
    ap.add_argument("--label-suffix", default=None,
                     help="extra text appended to the auto-generated label, e.g. checklist summary")
    ap.add_argument("--force", action="store_true",
                     help="skip the check-17 open-position/pending-order refusal")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    node = load_candidate(args.candidate_id)
    take_profit, trail_buy_pct, trail_pct, fixed_sl = derive_axis_args(
        node["strategy"], node["params"])

    label = (f"sweep pick (candidate_nodes id={args.candidate_id}, version={node['version']})")
    if args.old_wl_id is not None:
        label += f", replaces wl_id={args.old_wl_id}"
    if args.label_suffix:
        label += f". {args.label_suffix}"

    plan = dict(
        ticker=node["ticker"], strategy=node["strategy"], version=node["version"],
        window=node["window"], entry_timing=node["entry_timing"],
        take_profit=take_profit, trail_buy_pct=trail_buy_pct, trail_pct=trail_pct,
        fixed_sl=fixed_sl, max_hold_hours=node["max_hold_hours"],
        z_score_threshold=node["z_score_threshold"], account=args.account,
        notional=args.notional, state=args.state, addon_enabled=int(args.addon),
        drought_overlay_enabled=int(args.drought), label=label,
    )

    if args.old_wl_id is not None:
        n_pos, n_pending = check_old_node_clear(args.old_wl_id, args.force) or (0, 0)
        plan["old_wl_id_status"] = f"open_positions={n_pos}, pending_buys={n_pending}"

    if args.dry_run:
        print("DRY RUN -- no DB writes. Derived plan:")
        for k, v in plan.items():
            print(f"  {k}: {v}")
        return

    if args.old_wl_id is not None:
        with signals_db._conn() as c:
            c.execute("UPDATE watch_list SET archived_at=datetime('now') WHERE id=?",
                      (args.old_wl_id,))
            c.commit()
        print(f"Archived old wl_id={args.old_wl_id}")

    print(f"\n{node['ticker']}: creating {args.state} node (candidate_nodes id={args.candidate_id})")
    signals_db.add_node(
        ticker=node["ticker"], strategy=node["strategy"], version=node["version"],
        window=node["window"], take_profit=take_profit, stop_loss=fixed_sl,
        max_hold_hours=node["max_hold_hours"], label=label,
        z_score_threshold=node["z_score_threshold"], watchlist_id=args.watchlist_id,
        state=args.state, trail_buy_pct=trail_buy_pct, trail_pct=trail_pct,
        entry_timing=node["entry_timing"], starting_notional=args.notional,
        fixed_sl_override=fixed_sl, account=args.account,
    )

    with signals_db._conn() as c:
        new_id = c.execute(
            "SELECT id FROM watch_list WHERE label=? AND ticker=? ORDER BY added_at DESC LIMIT 1",
            (label, node["ticker"])).fetchone()[0]
        c.execute("UPDATE watch_list SET addon_enabled=?, drought_overlay_enabled=? WHERE id=?",
                  (int(args.addon), int(args.drought), new_id))
        c.commit()
    print(f"  wl_id={new_id}, account={args.account}, notional=${args.notional:,.0f}, "
          f"addon_enabled={int(args.addon)}, drought_overlay_enabled={int(args.drought)}")

    print("\nSeeding config-drift baseline (check 14)...")
    subprocess.run([sys.executable, "scripts/seed_baseline_config.py"], check=True)


if __name__ == "__main__":
    import script_usage
    script_usage.record_invocation()
    main()
