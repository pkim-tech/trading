"""Shared persistence helper for `candidate_verification_results` -- new, 2026-08-29
(Task #1, planner dispatch). Records per-candidate Phase3/Phase5 second-level-check
results (core/addon/drought/core_both CAGR at both 1m/1s granularity, plus trade
counts) so a rerun over an already-checked scope can skip the expensive kernel work
instead of recomputing it. See phase3_second_level_check.py / phase5_second_level_
overlay_check.py for the actual read/write call sites -- this module is just the
table definition + a thin get/put pair, kept in one place per this project's "don't
duplicate a CREATE TABLE statement" convention (see bench_phase1_phase2_inmemory.py's
own `_log_sweep_run_start` for the sibling pattern this follows: inline
CREATE TABLE IF NOT EXISTS, no separate migration file, no FK enforcement).
"""
import time

_VALUE_COLUMNS = [
    "n_trades_1m", "n_trades_1s",
    "core_cagr_1m", "core_cagr_1s", "core_delta_pp",
    "addon_cagr_1m", "addon_cagr_1s", "addon_delta_pp",
    "drought_cagr_1m", "drought_cagr_1s", "drought_delta_pp",
    "core_both_cagr_1m", "core_both_cagr_1s", "core_both_delta_pp",
]


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidate_verification_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            phase TEXT NOT NULL,  -- 'phase3' or 'phase5'
            checked_at TEXT NOT NULL,
            n_trades_1m INTEGER, n_trades_1s INTEGER,
            core_cagr_1m REAL, core_cagr_1s REAL, core_delta_pp REAL,
            addon_cagr_1m REAL, addon_cagr_1s REAL, addon_delta_pp REAL,
            drought_cagr_1m REAL, drought_cagr_1s REAL, drought_delta_pp REAL,
            core_both_cagr_1m REAL, core_both_cagr_1s REAL, core_both_delta_pp REAL,
            UNIQUE(candidate_id, phase)
        )""")
    # Fast "is this node verified" check straight off candidate_nodes, no join needed
    # (2026-08-29, planner refinement) -- the full numeric results still live only in
    # candidate_verification_results above; these two columns are a denormalized
    # pointer into it, kept in sync by upsert() below. sqlite has no
    # "ADD COLUMN IF NOT EXISTS" (pre-3.35), so probe for the column first.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(candidate_nodes)")}
    for col in ("phase3_checked_at", "phase5_checked_at"):
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE candidate_nodes ADD COLUMN {col} TEXT")


def get_stored(conn, candidate_id, phase):
    """Returns a dict with keys 'checked_at' + all of _VALUE_COLUMNS, or None if
    no row exists yet for this (candidate_id, phase)."""
    ensure_table(conn)
    row = conn.execute(
        "SELECT checked_at, " + ", ".join(_VALUE_COLUMNS) +
        " FROM candidate_verification_results WHERE candidate_id=? AND phase=?",
        (candidate_id, phase)).fetchone()
    if row is None:
        return None
    return dict(zip(["checked_at"] + _VALUE_COLUMNS, row))


def upsert(conn, candidate_id, phase, fields):
    """`fields` may be any dict containing (a subset of) _VALUE_COLUMNS as keys --
    anything not present is stored NULL (e.g. Phase3's addon/drought/core_both
    fields, which don't apply to a core-only check). Validates `candidate_id` is a
    real candidate_nodes row first -- this project's sqlite convention doesn't
    enforce real FK constraints (confirmed elsewhere in the codebase), so this is a
    manual check, not a DB-level guarantee; raises rather than inserting garbage.

    A rerun for the same (candidate_id, phase) UPDATEs via INSERT OR REPLACE
    (relying on the UNIQUE(candidate_id, phase) constraint) rather than duplicating
    -- note this assigns a NEW surrogate `id` on replace (old row deleted, new one
    inserted), which is fine here since nothing depends on the surrogate id staying
    stable, only on (candidate_id, phase) being unique. Returns the checked_at
    timestamp written."""
    ensure_table(conn)
    exists = conn.execute("SELECT 1 FROM candidate_nodes WHERE id=?", (candidate_id,)).fetchone()
    if exists is None:
        raise ValueError(f"candidate_id={candidate_id} not found in candidate_nodes -- refusing to insert")
    checked_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    cols = ["candidate_id", "phase", "checked_at"] + _VALUE_COLUMNS
    placeholders = ", ".join("?" for _ in cols)
    values = [candidate_id, phase, checked_at] + [fields.get(c) for c in _VALUE_COLUMNS]
    conn.execute(
        f"INSERT OR REPLACE INTO candidate_verification_results ({', '.join(cols)}) "
        f"VALUES ({placeholders})", values)
    if phase not in ("phase3", "phase5"):
        raise ValueError(f"phase={phase!r} not in ('phase3', 'phase5') -- refusing to "
                          f"update an arbitrary candidate_nodes column name from it")
    conn.execute(f"UPDATE candidate_nodes SET {phase}_checked_at=? WHERE id=?",
                 (checked_at, candidate_id))
    conn.commit()
    return checked_at
