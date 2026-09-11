"""Backtest-kernel canary node -- a pinned candidate with a known-good expected
result, re-verified whenever sweep/kernel formula code changes, so a silent
regression (like the pre-2026-09-08 drought_factor_gated no-op bug in
scripts/phase5_second_level_overlay_check.py, commit 10f1945 -- a DIFFERENT
function from the one this script checks, see below) gets caught by a real
check instead of discovered by accident days later in a stale report.

Compute path (2026-09-10, corrected after the first version of this script):
uses candidate_full_review.gt_full_review_rows -- the SAME function that
generates the real "Full Review" report tab (Phase 9/10's own compute) --
via candidates_override scoped to one candidate, NOT fresh_single_candidate_
overlay.compute_fresh. That tool only reads cached phase5_trades, and Phase5
is disabled for every campaign since 2026-09-08 -- confirmed live: candidate
50073 (a real v6.5.2 candidate) has ZERO phase5_trades rows, so compute_fresh
can never seed a canary for anything in v6.5.2 or later. gt_full_review_rows
has no such dependency -- it works for any candidate, any campaign.

Two tables:
  sweep_canary_nodes  -- aggregate expected CAGRs + tolerance + the git commit the
                         expectation was captured against. Fast pass/fail.
  sweep_canary_trades -- frozen trade-by-trade snapshot (same columns as
                         phase5_trades), copied ONCE at seed time, BEST-EFFORT
                         (only populated when phase5_trades actually has a row for
                         this candidate -- most new candidates won't). Deliberately
                         NOT a live pointer into phase5_trades even when it exists
                         (no build_id/staleness tracking of its own, a known,
                         already-flagged gap, docs/backlog_cache.md 2026-09-04, so
                         it could in principle be silently overwritten by a future
                         Phase5 rerun). Only consulted when the aggregate check
                         flags a mismatch, to show WHERE trades diverged -- a
                         candidate with no snapshot just can't get that deeper
                         drill-down, the aggregate check still applies fully.

Usage:
  .venv/bin/python scripts/sweep_canary.py --seed CANDIDATE_ID [--notes TEXT] [--tolerance-pct 0.1]
  .venv/bin/python scripts/sweep_canary.py --check
"""
import argparse
import datetime
import os
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from candidate_full_review import gt_full_review_rows, DEFAULT_VOL_GATE  # noqa: E402
from phase4_candidate_nodes_resolver import derive_phase25_candidates_from_candidate_nodes  # noqa: E402

DB_PATH = "cache/research/trading_universe.db"


def ensure_canary_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sweep_canary_nodes (
            candidate_id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            strategy TEXT NOT NULL,
            expected_core_cagr_pct REAL,
            expected_addon_cagr_pct REAL,
            expected_drought_cagr_pct REAL,
            expected_core_both_cagr_pct REAL,
            expected_n_trades INTEGER,
            expected_as_of_commit TEXT,
            tolerance_pct REAL NOT NULL DEFAULT 0.1,
            has_trade_snapshot INTEGER NOT NULL DEFAULT 0,
            seeded_at TEXT NOT NULL,
            last_checked_at TEXT,
            last_check_status TEXT,
            notes TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sweep_canary_trades (
            candidate_id INTEGER NOT NULL,
            trade_idx INTEGER NOT NULL,
            entry_time TEXT, entry_price REAL,
            exit_time TEXT, exit_price REAL,
            exit_reason TEXT, return_pct REAL,
            armed INTEGER, arm_time TEXT, arm_price REAL,
            PRIMARY KEY (candidate_id, trade_idx)
        )
    """)
    conn.commit()


def _current_commit():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                               capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return None


def _load_node(conn, candidate_id):
    row = conn.execute(
        "SELECT id, ticker, strategy, version, window, entry_timing, fixed_sl "
        "FROM candidate_nodes WHERE id=?", (candidate_id,)).fetchone()
    if row is None:
        raise SystemExit(f"candidate_nodes id={candidate_id} not found")
    return dict(row)


def compute_via_report(conn, candidate_id):
    """Real aggregate compute for ONE candidate via the actual report-generation
    code path (candidate_full_review.gt_full_review_rows), scoped with
    candidates_override -- works regardless of phase5_trades history."""
    node = _load_node(conn, candidate_id)
    population = derive_phase25_candidates_from_candidate_nodes(
        node["ticker"], node["strategy"], node["version"], fixed_sl=node["fixed_sl"],
        entry_timing=node["entry_timing"], window=node["window"], full_population=True)
    override = [c for c in population if c["id"] == candidate_id]
    if not override:
        return dict(candidate_id=candidate_id, ok=False,
                     reason=f"candidate_id={candidate_id} not found in its own scope's "
                            f"real population (derive_phase25_candidates_from_candidate_nodes)")
    rows = gt_full_review_rows(conn, node["ticker"], node["strategy"], node["version"],
                                node["entry_timing"], node["fixed_sl"],
                                vol_gate=DEFAULT_VOL_GATE, candidates_override=override)
    if not rows:
        return dict(candidate_id=candidate_id, ok=False,
                     reason="gt_full_review_rows returned no rows for this candidate")
    r = rows[0]
    return dict(candidate_id=candidate_id, ok=True, node=node,
                core=r.get("abs_return_pct"), addon=r.get("core_addon_cagr_pct"),
                drought=r.get("core_drought_cagr_pct"), both=r.get("core_both_cagr_pct"),
                n_trades=r.get("trades"))


def seed_canary(conn, candidate_id, notes=None, tolerance_pct=0.1):
    ensure_canary_tables(conn)
    result = compute_via_report(conn, candidate_id)
    if not result["ok"]:
        raise SystemExit(f"cannot seed candidate_id={candidate_id}: {result['reason']}")
    node = result["node"]

    trade_rows = conn.execute("""
        SELECT trade_idx, entry_time, entry_price, exit_time, exit_price, exit_reason,
               return_pct, armed, arm_time, arm_price
        FROM phase5_trades WHERE candidate_id=? AND resolution='1s' ORDER BY trade_idx
    """, (candidate_id,)).fetchall()
    has_snapshot = bool(trade_rows)

    now = datetime.datetime.utcnow().isoformat()
    commit = _current_commit()
    conn.execute("""
        INSERT INTO sweep_canary_nodes
          (candidate_id, ticker, strategy, expected_core_cagr_pct, expected_addon_cagr_pct,
           expected_drought_cagr_pct, expected_core_both_cagr_pct, expected_n_trades,
           expected_as_of_commit, tolerance_pct, has_trade_snapshot, seeded_at, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(candidate_id) DO UPDATE SET
          expected_core_cagr_pct=excluded.expected_core_cagr_pct,
          expected_addon_cagr_pct=excluded.expected_addon_cagr_pct,
          expected_drought_cagr_pct=excluded.expected_drought_cagr_pct,
          expected_core_both_cagr_pct=excluded.expected_core_both_cagr_pct,
          expected_n_trades=excluded.expected_n_trades,
          expected_as_of_commit=excluded.expected_as_of_commit,
          tolerance_pct=excluded.tolerance_pct,
          has_trade_snapshot=excluded.has_trade_snapshot,
          seeded_at=excluded.seeded_at,
          notes=excluded.notes
    """, (candidate_id, node["ticker"], node["strategy"], result["core"], result["addon"],
          result["drought"], result["both"], result["n_trades"], commit,
          tolerance_pct, int(has_snapshot), now, notes))

    conn.execute("DELETE FROM sweep_canary_trades WHERE candidate_id=?", (candidate_id,))
    if has_snapshot:
        conn.executemany("""
            INSERT INTO sweep_canary_trades
              (candidate_id, trade_idx, entry_time, entry_price, exit_time, exit_price,
               exit_reason, return_pct, armed, arm_time, arm_price)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, [(candidate_id, *r) for r in trade_rows])
    conn.commit()
    snap_note = f"{len(trade_rows)} trades snapshotted" if has_snapshot else \
        "NO trade snapshot (no phase5_trades for this candidate -- aggregate-only canary)"
    print(f"Seeded canary candidate_id={candidate_id} ({node['ticker']}/{node['strategy']}): "
          f"core={result['core']:.2f} addon={result['addon']:.2f} drought={result['drought']:.2f} "
          f"both={result['both']:.2f} n_trades={result['n_trades']}, {snap_note}, commit={commit}")


def _pct_diff(name, exp, act, tol):
    if exp is None or act is None:
        return None if exp == act else f"{name}: expected={exp} actual={act}"
    if abs(exp) < 1e-9:
        return None if abs(act) < 1e-9 else f"{name}: expected={exp:.3f} actual={act:.3f}"
    diff = abs(act - exp) / abs(exp) * 100.0
    if diff > tol:
        return f"{name}: expected={exp:.3f} actual={act:.3f} (diff {diff:.2f}% > tol {tol}%)"
    return None


def check_canaries(conn):
    ensure_canary_tables(conn)
    rows = conn.execute("""
        SELECT candidate_id, ticker, strategy, expected_core_cagr_pct,
               expected_addon_cagr_pct, expected_drought_cagr_pct,
               expected_core_both_cagr_pct, expected_n_trades, tolerance_pct,
               has_trade_snapshot
        FROM sweep_canary_nodes
    """).fetchall()
    if not rows:
        print("No canary nodes registered -- seed one with --seed CANDIDATE_ID first.")
        return True

    all_ok = True
    for (cid, ticker, strategy, exp_core, exp_addon, exp_drought, exp_both, exp_n, tol,
         has_snapshot) in rows:
        result = compute_via_report(conn, cid)
        now = datetime.datetime.utcnow().isoformat()
        if not result["ok"]:
            print(f"FAIL candidate_id={cid} ({ticker}/{strategy}): {result['reason']}")
            conn.execute("UPDATE sweep_canary_nodes SET last_checked_at=?, last_check_status=? "
                         "WHERE candidate_id=?", (now, f"ERROR: {result['reason']}", cid))
            all_ok = False
            continue

        mismatches = [m for m in (
            _pct_diff("core", exp_core, result["core"], tol),
            _pct_diff("addon", exp_addon, result["addon"], tol),
            _pct_diff("drought", exp_drought, result["drought"], tol),
            _pct_diff("both", exp_both, result["both"], tol),
        ) if m]
        if result["n_trades"] != exp_n:
            mismatches.append(f"n_trades: expected={exp_n} actual={result['n_trades']}")

        status = "PASS" if not mismatches else "FAIL: " + "; ".join(mismatches)
        conn.execute("UPDATE sweep_canary_nodes SET last_checked_at=?, last_check_status=? "
                     "WHERE candidate_id=?", (now, status, cid))
        if mismatches:
            all_ok = False
            print(f"FAIL candidate_id={cid} ({ticker}/{strategy}):")
            for m in mismatches:
                print(f"  {m}")
            if has_snapshot:
                print(f"  -> for a trade-level diff: compare current trades against "
                      f"sweep_canary_trades WHERE candidate_id={cid}")
            else:
                print("  -> no trade-level snapshot exists for this candidate (never had "
                      "phase5_trades) -- only the aggregate diff above is available")
        else:
            print(f"PASS candidate_id={cid} ({ticker}/{strategy}): "
                  f"core={result['core']:.2f} addon={result['addon']:.2f} "
                  f"drought={result['drought']:.2f} both={result['both']:.2f} "
                  f"n_trades={result['n_trades']}")
    conn.commit()
    return all_ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=None, metavar="CANDIDATE_ID")
    ap.add_argument("--notes", default=None)
    ap.add_argument("--tolerance-pct", type=float, default=0.1)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30.0)
    conn.row_factory = sqlite3.Row
    if args.seed is not None:
        seed_canary(conn, args.seed, notes=args.notes, tolerance_pct=args.tolerance_pct)
    elif args.check:
        ok = check_canaries(conn)
        sys.exit(0 if ok else 1)
    else:
        ap.error("specify --seed CANDIDATE_ID or --check")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
