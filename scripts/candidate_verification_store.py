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


# --- phase5_trades: real per-trade persistence (Task, 2026-08-29 planner dispatch) ---
# Sibling table to `candidate_verification_results` above (which only stores the
# aggregate CAGR/delta numbers) -- this one stores the actual trade lists
# (`run_backtest_ground_truth(..., need_times=True)` output) so a later investigation
# of a Phase5 outlier can query real trades back out instead of re-paying the full
# ~176-290s 1-second-data-load cost just to regenerate them. Same column shape as
# this project's existing sibling trade-persistence table, `backtest_winner_trades`
# (see bench_phase1_phase2_inmemory.py's `_insert_winner_trades_rows`), plus
# `candidate_id` (the real candidate_nodes.id -- this table's actual unique key,
# since a candidate_nodes-sourced Phase5 node has no `node_key` computed for it the
# way Phase2.5's population does) and `resolution` ('1m' or '1s' -- the same
# trade_idx appears twice per candidate, once per resolution).
#
# node_key decision: stored as str(candidate_id), not left NULL and not computed via
# node_key.node_key() (that's a different identity scheme, built for the Phase2.5/
# backtest_winner_trades population, and was never computed for a candidate_nodes-
# sourced Phase5 candidate -- there is nothing to reuse). candidate_id is already the
# real UNIQUE key here, so node_key is purely a human-readable convenience column
# matching backtest_winner_trades' shape, not a second identity system.
def ensure_trades_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS phase5_trades (
            node_key TEXT, version TEXT, ticker TEXT, strategy TEXT, fixed_sl REAL,
            trade_idx INTEGER, entry_time TEXT, entry_price REAL, exit_time TEXT,
            exit_price REAL, exit_reason TEXT, return_pct REAL, armed INTEGER,
            arm_time TEXT, arm_price REAL, created_at TEXT,
            candidate_id INTEGER, resolution TEXT,
            UNIQUE(candidate_id, resolution, trade_idx)
        )""")


def trades_complete(conn, candidate_id, expected_n_1m, expected_n_1s):
    """True if phase5_trades already has every trade row for both resolutions of this
    candidate. Compares against the trade COUNTS already recorded in candidate_
    verification_results (`expected_n_1m`/`expected_n_1s`) rather than just "any row
    exists" -- a candidate with a genuinely empty trade list (n_trades=0, e.g. an
    armed-trade-free scope) must not be treated as incomplete forever just because it
    has zero rows to ever insert; a candidate with 0 < n_trades that has fewer stored
    rows than expected (e.g. a prior run crashed mid-insert) correctly stays
    incomplete and gets recomputed/backfilled."""
    ensure_trades_table(conn)
    counts = dict(conn.execute(
        "SELECT resolution, COUNT(*) FROM phase5_trades WHERE candidate_id=? "
        "GROUP BY resolution", (candidate_id,)).fetchall())
    ok_1m = expected_n_1m == 0 or counts.get("1m", 0) >= expected_n_1m
    ok_1s = expected_n_1s == 0 or counts.get("1s", 0) >= expected_n_1s
    return ok_1m and ok_1s


def insert_trades(conn, candidate_id, resolution, version, ticker, strategy, fixed_sl, trades):
    """Inserts one resolution's ('1m' or '1s') full trade list for `candidate_id`.
    Follows this project's exact existing convention for a sibling trade-persistence
    table (see bench_phase1_phase2_inmemory.py's `_insert_winner_trades_rows`):
    INSERT OR IGNORE via executemany, before/after COUNT(*) diff to report how many
    rows were skipped (already present) vs newly inserted. Returns
    (n_newly_inserted, n_total_in_buffer)."""
    ensure_trades_table(conn)
    node_key = str(candidate_id)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    buffer = []
    for i, t in enumerate(trades):
        buffer.append((
            node_key, version, ticker, strategy, float(fixed_sl), i,
            str(t['Entry Time']), t['Entry Price'], str(t['Exit Time']),
            t['Exit Price'], t['exit_reason'], t['Return'], int(t['armed']),
            str(t['Arm Time']) if t['armed'] else None, t['Arm Price'], now_iso,
            candidate_id, resolution,
        ))
    before = conn.execute(
        "SELECT COUNT(*) FROM phase5_trades WHERE candidate_id=? AND resolution=?",
        (candidate_id, resolution)).fetchone()[0]
    conn.executemany("""
        INSERT OR IGNORE INTO phase5_trades
            (node_key, version, ticker, strategy, fixed_sl, trade_idx, entry_time,
             entry_price, exit_time, exit_price, exit_reason, return_pct, armed,
             arm_time, arm_price, created_at, candidate_id, resolution)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, buffer)
    conn.commit()
    after = conn.execute(
        "SELECT COUNT(*) FROM phase5_trades WHERE candidate_id=? AND resolution=?",
        (candidate_id, resolution)).fetchone()[0]
    return after - before, len(buffer)
