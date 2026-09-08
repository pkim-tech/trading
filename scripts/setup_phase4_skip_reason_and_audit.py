"""One-time (idempotent) schema setup + backfill for the Phase4/Phase5 1s-canonical
migration (2026-09-07, peer-session dispatch). Does three things, additive/safe:

1. Adds `phase4_skip_reason` TEXT and `phase4_eligible` INTEGER (nullable bool) columns
   to `candidate_nodes` (ALTER TABLE ADD COLUMN, probe-first pattern, same convention
   every other real writer in this codebase uses).

2. Creates `candidate_nodes_audit_log` -- a trigger-based audit table capturing the full
   "before" row (as JSON) whenever `candidate_nodes` or `candidate_verification_results`
   is UPDATEd or DELETEd (never on INSERT -- keeps it cheap, matches the ask). SQLite has
   no row-to-json shorthand, so each trigger's json_object() call enumerates every real
   column, generated here from PRAGMA table_info at trigger-creation time -- KNOWN LIMIT:
   a column added to either table LATER will not be captured until this script is re-run
   (DROP + recreate the 4 triggers) to regenerate the enumeration.

3. Backfills `phase4_skip_reason` for real v6.5.1 candidates with no
   candidate_verification_results row, using the VERIFIED (not guessed) 3-way taxonomy --
   checked directly against real data (2026-09-07): every one of the ~4,196 gaps across
   all 14 v6.5.1 tickers falls into exactly one of:
     - 'below_trade_count'      trades < MIN_TRADES_FOR_PHASE4 (50)
     - 'negative_worst_neighbor' worst_neighbor_cagr < 0
     - 'core_safe_false'        has a real phase4_results row with core_safe=0 (CLIFF-
                                 disqualified by Phase4 itself; Phase5's own core-safe
                                 gate in phase5_second_level_overlay_check.py then skips
                                 it as wasted compute -- see that function's docstring)
   NOT phase4_eligible/PHASE25_ISLAND_CAGR_MIN (a different, real mechanism -- gates
   drought/addon overlay compute WITHIN a Phase4 run, confirmed NOT the cause of any of
   these gaps). Precedence matches phase5_second_level_overlay_check.py's own pre-filter
   order (trades check first, then worst_neighbor_cagr) for the first two buckets.

   phase4_eligible itself is NOT backfilled (left NULL for every historical row) --
   it's computed fresh every Phase4 run from the ISLAND's top_cagr (transient, re-
   derived from df_final each time), never stored at candidate-promotion time, so there
   is no historical value to recover without re-running Phase4's island-detection logic
   per scope. Going forward, run_optimization_sweep.py's build_candidate_report_ground_
   truth persists it directly when computed (see that function's own comment near line
   3002).

Usage:
  .venv/bin/python scripts/setup_phase4_skip_reason_and_audit.py --ticker SOXL
  .venv/bin/python scripts/setup_phase4_skip_reason_and_audit.py --all-tickers
"""
import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from run_optimization_sweep import DB_PATH

MIN_TRADES_FOR_PHASE4 = 50


def _ensure_schema(conn):
    existing = {r[1] for r in conn.execute("PRAGMA table_info(candidate_nodes)")}
    for col in ("phase4_skip_reason TEXT", "phase4_eligible INTEGER"):
        name = col.split()[0]
        if name not in existing:
            conn.execute(f"ALTER TABLE candidate_nodes ADD COLUMN {col}")
            print(f"  added candidate_nodes.{name}")


def _column_list(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _json_object_sql(cols, alias):
    parts = ", ".join(f"'{c}', {alias}.{c}" for c in cols)
    return f"json_object({parts})"


def _ensure_audit(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidate_nodes_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            row_id INTEGER NOT NULL,
            operation TEXT NOT NULL,
            before_json TEXT NOT NULL,
            changed_at TEXT NOT NULL
        )
    """)
    for table in ("candidate_nodes", "candidate_verification_results"):
        cols = _column_list(conn, table)
        for op in ("UPDATE", "DELETE"):
            trig_name = f"{table}_audit_{op.lower()}"
            conn.execute(f"DROP TRIGGER IF EXISTS {trig_name}")
            json_sql = _json_object_sql(cols, "OLD")
            conn.execute(f"""
                CREATE TRIGGER {trig_name}
                AFTER {op} ON {table}
                BEGIN
                    INSERT INTO candidate_nodes_audit_log
                        (table_name, row_id, operation, before_json, changed_at)
                    VALUES ('{table}', OLD.id, '{op}', {json_sql}, datetime('now'));
                END
            """)
    print("  audit table + 4 triggers (candidate_nodes x2, candidate_verification_results x2) ready")


def backfill_skip_reason(conn, ticker=None):
    where = "cn.version LIKE 'v6.5.1%'"
    params = []
    if ticker:
        where += " AND cn.ticker=?"
        params.append(ticker)
    rows = conn.execute(f"""
        SELECT cn.id, cn.trades, cn.worst_neighbor_cagr
        FROM candidate_nodes cn
        LEFT JOIN candidate_verification_results cvr ON cvr.candidate_id = cn.id
        WHERE {where} AND cvr.id IS NULL AND cn.phase4_skip_reason IS NULL
    """, params).fetchall()
    counts = {"below_trade_count": 0, "negative_worst_neighbor": 0, "core_safe_false": 0, "unexplained": 0}
    updates = []
    for cid, trades, wnc in rows:
        if trades is not None and trades < MIN_TRADES_FOR_PHASE4:
            reason = "below_trade_count"
        elif wnc is not None and wnc < 0:
            reason = "negative_worst_neighbor"
        else:
            p4 = conn.execute("SELECT core_safe FROM phase4_results WHERE candidate_id=?", (cid,)).fetchone()
            if p4 is not None and p4[0] == 0:
                reason = "core_safe_false"
            else:
                reason = "unexplained"
        counts[reason] += 1
        updates.append((reason, cid))
    if updates:
        conn.executemany("UPDATE candidate_nodes SET phase4_skip_reason=? WHERE id=?", updates)
        conn.commit()
    print(f"  backfilled {len(updates)} row(s){' for ' + ticker if ticker else ' (all tickers)'}: {counts}")
    if counts["unexplained"]:
        print(f"  *** {counts['unexplained']} row(s) did not match the verified 3-way taxonomy -- "
              f"investigate before trusting this backfill further ***")
    return counts


if __name__ == "__main__":
    import script_usage
    script_usage.record_invocation()
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker")
    ap.add_argument("--all-tickers", action="store_true")
    args = ap.parse_args()
    if not args.ticker and not args.all_tickers:
        raise SystemExit("pass --ticker TICK or --all-tickers")
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    _ensure_schema(conn)
    _ensure_audit(conn)
    backfill_skip_reason(conn, ticker=args.ticker)
