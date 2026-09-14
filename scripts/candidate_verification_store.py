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
import sqlite3
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


# --- phase5_drought_windows: drought overlay's own buy/manage windows (2026-09-13,
# Phase4/Phase5 trade-persistence gap, docs/watchlist_candidate_checklist.md check 19) ---
# A drought window (backtester.simulate_drought_overlay_ground_truth's own 'best_rets'/
# 'best_window_times') is NOT a real strategy trade -- no fixed-SL/arm/trail-exit shape,
# just a buy-and-manage interval between two real signals -- so it doesn't fit phase5_
# trades' trade_idx/armed/arm_time/arm_price columns without conflating the two. Separate
# sibling table instead, same INSERT OR IGNORE/before-after-count convention as
# insert_trades above.
def ensure_drought_windows_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS phase5_drought_windows (
            candidate_id INTEGER, ticker TEXT, strategy TEXT, version TEXT,
            resolution TEXT, window_idx INTEGER, start_time TEXT, end_time TEXT,
            return_pct REAL, confirm_days INTEGER, vol_gate REAL, created_at TEXT,
            UNIQUE(candidate_id, resolution, window_idx)
        )""")


def insert_drought_windows(conn, candidate_id, ticker, strategy, version, resolution, drought):
    """`drought` is a real backtester.simulate_drought_overlay_ground_truth() return dict
    (or None / a dict with best_rets=None -- both no-ops, nothing to persist). Returns
    (n_newly_inserted, n_total_in_buffer), same shape as insert_trades."""
    if drought is None or not drought.get("best_rets"):
        return 0, 0
    ensure_drought_windows_table(conn)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    buffer = []
    for i, (ret, (start_t, end_t)) in enumerate(zip(drought["best_rets"], drought["best_window_times"])):
        buffer.append((
            candidate_id, ticker, strategy, version, resolution, i, str(start_t), str(end_t),
            ret * 100, drought["best_confirm_days"], drought["best_vol_gate"], now_iso,
        ))
    before = conn.execute(
        "SELECT COUNT(*) FROM phase5_drought_windows WHERE candidate_id=? AND resolution=?",
        (candidate_id, resolution)).fetchone()[0]
    conn.executemany("""
        INSERT OR IGNORE INTO phase5_drought_windows
            (candidate_id, ticker, strategy, version, resolution, window_idx,
             start_time, end_time, return_pct, confirm_days, vol_gate, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, buffer)
    conn.commit()
    after = conn.execute(
        "SELECT COUNT(*) FROM phase5_drought_windows WHERE candidate_id=? AND resolution=?",
        (candidate_id, resolution)).fetchone()[0]
    return after - before, len(buffer)


# --- backtest_winner_trades read-back (Task #1, 2026-08-29 planner dispatch, Phase4 TRADES) ---
# Phase4 (build_candidate_report_ground_truth in run_optimization_sweep.py) used to ALWAYS
# re-simulate every candidate's trades via run_backtest_ground_truth, even though Phase2.5
# (bench_phase1_phase2_inmemory.py's _insert_winner_trades_rows) already persists the
# IDENTICAL same_bar_reentry=True/need_times=True trade list for its own top-9-per-scope
# population, under the same real inputs (same node_key params, same version -- which
# encodes data_source + start/end date window) Phase4 re-derives from scratch. Reading it
# back saves a full run_backtest_ground_truth call per candidate whenever it's already
# there. Real coverage check at dispatch time (see this task's own commit message):
# Phase2.5 only EVER persists its own top-9-per-scope population, so a Phase4 population
# wider than that top-9 (e.g. candidate_nodes' full_population=True path) will legitimately
# miss cache for the non-top-9 candidates -- get_cached_trades returning None is that real,
# expected fallback-to-resimulate signal, not a bug.
def get_cached_trades(conn, node_key_val, version, ticker=None,
                       kernel_version=None, hourly_build_id=None, minute_build_id=None):
    """Real persisted trade list for (node_key_val, version) from backtest_winner_trades,
    reconstructed into the exact dict shape build_candidate_report_ground_truth's downstream
    checks (4/8/11/13, drought) expect: 'Entry Time'/'Exit Time' as real pandas Timestamps
    (NOT the TEXT strings stored on disk -- _check13_walk_forward_gt's fold-span math and
    simulate_drought_overlay_ground_truth's idx.searchsorted both require real Timestamp
    objects, not strings), 'armed' as a real bool, 'Arm Time'/'Arm Price' as None when
    armed=0 (matching run_backtest_ground_truth's own None-when-unarmed convention rather
    than echoing back whatever NULL/non-NULL happens to be stored), plus 'Ticker' (added
    2026-08-29, paired-review LOW finding -- run_backtest_ground_truth's own trade dicts
    normally carry this; a fresh-resim trade list already has it, so a cache-hit trade
    list omitting it was a real shape divergence that could KeyError a future consumer
    even though nothing reads it today).

    Staleness invalidation (2026-08-29, paired-review HIGH finding, both independent-cold
    and contextual Opus review independently converged on this against the original
    version of this function): node_key/version alone do NOT catch (1) a future
    backtester.py fix changing what run_backtest_ground_truth computes, or (2) a
    scripts/promote_derived_build.py promotion changing the real underlying bars with
    ZERO change to `version` (real precedent: the 2026-08-27 SOXL/DPST/DFEN minute-
    archive narrowing incident) -- unlike backtest_cache, which uses kernel_version as
    real row-identity, not just an informational column. When the caller passes
    `kernel_version`/`hourly_build_id`/`minute_build_id` (run_optimization_sweep.py's
    real caller always does), a stored row whose own kernel_version/hourly_build_id/
    minute_build_id doesn't EXACTLY match -- including when the caller's OR the stored
    value is None (an unresolved/never-promoted build, or a legacy pre-this-fix row that
    predates these columns) -- is treated as stale and skipped: 'unknown' is never
    silently trusted as 'matches'. Passing None for all three (the default) skips this
    check entirely -- ONLY safe for a caller that doesn't care about staleness (no real
    caller in this codebase does that today).

    Returns None (not []) when the table doesn't exist yet, has zero rows for this
    (node_key_val, version) that also pass the staleness check above, or the stored
    trade_idx sequence has a gap (a prior insert that never finished) -- all of these are
    this function's real "not safely cached, caller must fall back to a fresh
    run_backtest_ground_truth call" signal. A genuinely trade-free candidate (real
    n_trades=0) is indistinguishable from "never cached" here (no separate expected-count
    row exists for this table, unlike phase5_trades' trades_complete check) -- an
    accepted, documented limitation, not a silent bug: the caller always has a real
    re-simulation fallback available, so worst case is one wasted (but still correct)
    recompute, never a wrong answer."""
    try:
        # fill_resolution (2026-09-06, paired-review HIGH finding -- confirmed by both
        # independent-cold and contextual review): _insert_winner_trades_rows can now
        # write real second-resolution trade sequences, but this function previously had
        # no way to tell the caller that -- run_optimization_sweep.py's caller was
        # unconditionally hard-labeling every cache hit 'minute_cache', which was true
        # when this function was written but became a real, silent mislabel the moment
        # bench started writing 1s trades here. Stamped onto each returned trade dict
        # (matching the existing 'Ticker' convention) rather than changing this
        # function's return shape, since scripts/persist_addon_overlay_trades.py -- a
        # REAL existing consumer, not a hypothetical one -- already destructures this
        # exact dict shape.
        rows = conn.execute("""
            SELECT trade_idx, entry_time, entry_price, exit_time, exit_price, exit_reason,
                   return_pct, armed, arm_time, arm_price,
                   kernel_version, hourly_build_id, minute_build_id, fill_resolution
            FROM backtest_winner_trades
            WHERE node_key=? AND version=?
            ORDER BY trade_idx
        """, (node_key_val, version)).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None
    if [r[0] for r in rows] != list(range(len(rows))):
        return None  # gapped/partial persistence -- don't trust it, fall back
    # Staleness check -- every row shares the same (kernel_version, hourly_build_id,
    # minute_build_id) by construction (one _insert_winner_trades_rows call now does a
    # real DELETE-then-INSERT per (node_key, version), see that function's own docstring
    # for the round-2 paired-review HIGH fix this depends on -- without it, a re-run
    # could leave a "Frankenstein" mix of old/new-vintage rows at one node_key, silently
    # breaking this stands-in-for-the-whole-set assumption), so checking row 0 stands in
    # for the whole set.
    #
    # EXPLICIT None-handling (2026-08-29, paired-review LOW finding, round 2,
    # independent-cold): plain `!=` would let a stored None (never-stamped/legacy row)
    # silently "match" a caller-passed None (a ticker/table with nothing ever promoted)
    # -- unreachable today since run_optimization_sweep.py's real caller always resolves
    # real values, but the comparison must match this function's own documented "unknown
    # is never trusted as matches" guarantee regardless of what a future caller passes.
    if kernel_version is not None or hourly_build_id is not None or minute_build_id is not None:
        _stored_kv, _stored_hb, _stored_mb = rows[0][10], rows[0][11], rows[0][12]
        _mismatch = (
            _stored_kv is None or _stored_kv != kernel_version
            or _stored_hb is None or _stored_hb != hourly_build_id
            or _stored_mb is None or _stored_mb != minute_build_id
        )
        if _mismatch:
            return None  # stale or unresolvable -- fall back to a fresh resimulation
    import pandas as pd
    trades = []
    for (_, entry_time, entry_price, exit_time, exit_price, exit_reason,
         return_pct, armed, arm_time, arm_price, _kv, _hb, _mb, _fill_res) in rows:
        armed = bool(armed)
        trades.append({
            'Ticker': ticker,
            # NULL (a legacy row from before this column existed) means minute --
            # every row ever written before 2026-09-06 was minute-only.
            'fill_resolution': _fill_res if _fill_res is not None else 'minute',
            'Entry Time': pd.Timestamp(entry_time), 'Entry Price': entry_price,
            'Exit Time': pd.Timestamp(exit_time), 'Exit Price': exit_price,
            'exit_reason': exit_reason, 'Return': return_pct, 'armed': armed,
            'Arm Time': pd.Timestamp(arm_time) if armed and arm_time else None,
            'Arm Price': arm_price if armed else None,
        })
    return trades


def get_phase5_1s_trades(conn, candidate_id, ticker=None, strategy=None, fixed_sl=None,
                          start_date=None, end_date=None):
    """Real persisted 1-second-resolution core trade list from `phase5_trades`
    (scripts/phase5_second_level_overlay_check.py's own persistence, `_persist_trades`),
    reconstructed into the EXACT dict shape get_cached_trades above produces -- same
    downstream consumers (build_candidate_report_ground_truth's checks 4/8/11/13,
    apply_addon_overlay_ground_truth, simulate_drought_overlay_ground_truth), same shape
    requirement (real pandas Timestamps, not strings; 'armed' a real bool; 'Arm Time'/
    'Arm Price' None when unarmed).

    Real gap found 2026-09-04 (docs/research_log.md / this session's conversation): that
    report's own core_addon_cagr_pct/core_drought_cagr_pct/core_both_cagr_pct always
    multiplied a core_factor from a FRESH re-simulation of `run_backtest_ground_truth`
    against MINUTE-resolution data (every call site in run_optimization_sweep.py passes
    minute_df, confirmed directly) -- a third, independent re-derivation of the same
    core trade list Phase5 already computed and stored at 1-second resolution
    (candidate_verification_results.core_cagr_1s, the number this project's own
    convention already treats as the trusted one). Feeding the SAME stored 1s trades
    into the checklist compute instead of re-simulating removes this divergence at the
    source, for any candidate Phase5 already verified.

    WINDOW VALIDATION (added after paired review, 2026-09-04 -- both independent-cold
    and contextual Opus review independently converged on this as a real, DB-confirmed
    CRITICAL/HIGH bug in the first version of this function): `phase5_second_level_
    overlay_check.py`'s own main() hardcodes ONE fixed simulation window
    ("2021-08-23"/"2026-08-21") for EVERY campaign version it ever processes, regardless
    of that version's own real (narrower, or differently-dated) window -- confirmed
    against the real DB: version 'v6.5-...-w2021-08-23_2026-05-23-...-pv4' has 823
    candidates whose persisted 1s trades run past 2026-05-23 into 2026-08-21, ~3 months
    outside that version's own real window. A plain version-string match does NOT catch
    this (the rows carry the correct version label; they were simply simulated over a
    wider window than that version represents). So: when `start_date`/`end_date` are
    given (the caller's own real campaign window), any stored row falling outside
    [start_date, end_date] makes the WHOLE trade list untrustworthy for this call --
    return None (fall back to resimulation) rather than silently feeding a years/fold-
    span mismatch into check8/11/13 and the core_factor math.

    PARAM CROSS-CHECK (same review round, contextual MEDIUM finding): candidate_id alone
    is trusted as the join key with no verification against the row's own stored ticker/
    strategy/fixed_sl -- cheap to check, done here now. Ticker is validated, not
    silently overridden by the caller's value as the pre-review version of this function
    did.

    Returns None (never []) when: candidate_id is None, `phase5_trades` doesn't exist
    yet, there is no resolution='1s' row for this candidate_id, the stored trade_idx
    sequence has a gap, a provided ticker/strategy/fixed_sl doesn't match the stored
    row, or (when start_date/end_date are given) any trade falls outside that window --
    all of these are the caller's real "no stored 1s trades safely usable here, fall
    back to a fresh run_backtest_ground_truth call" signal, same contract get_cached_
    trades already establishes for backtest_winner_trades. A genuinely trade-free
    candidate (real n_trades=0) is indistinguishable from "never persisted" here, same
    accepted limitation get_cached_trades documents -- the caller always has a real
    re-simulation fallback, so worst case is one wasted (but still correct) recompute,
    never a wrong answer.

    KNOWN RESIDUAL GAP, not fixed here (flagged, not silently skipped): unlike
    get_cached_trades/backtest_winner_trades, `phase5_trades` has no kernel_version/
    hourly_build_id/minute_build_id columns, so a Phase5 re-run after a real kernel fix
    or a derived-build promotion (the 2026-08-27 SOXL/DPST/DFEN incident class) cannot
    be detected as stale here, and `insert_trades`'s INSERT OR IGNORE (no DELETE-first)
    means such a re-run can't even overwrite old rows. Real fix (schema parity with
    backtest_winner_trades) intentionally out of scope for this pass -- flagged to
    docs/backlog_cache.md, not silently left undocumented."""
    if candidate_id is None:
        return None
    try:
        rows = conn.execute("""
            SELECT trade_idx, entry_time, entry_price, exit_time, exit_price, exit_reason,
                   return_pct, armed, arm_time, arm_price, ticker, strategy, fixed_sl
            FROM phase5_trades
            WHERE candidate_id=? AND resolution='1s'
            ORDER BY trade_idx
        """, (candidate_id,)).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None
    if [r[0] for r in rows] != list(range(len(rows))):
        return None  # gapped/partial persistence -- don't trust it, fall back
    _, _, _, _, _, _, _, _, _, _, row_ticker, row_strategy, row_fixed_sl = rows[0]
    if ticker is not None and row_ticker != ticker:
        return None
    if strategy is not None and row_strategy != strategy:
        return None
    if fixed_sl is not None and float(row_fixed_sl) != float(fixed_sl):
        return None
    import pandas as pd
    if start_date is not None and pd.Timestamp(rows[0][1]) < pd.Timestamp(start_date):
        return None  # first trade's entry predates the caller's real window
    if end_date is not None and pd.Timestamp(rows[-1][3]) > pd.Timestamp(end_date) + pd.Timedelta(days=1):
        return None  # last trade's exit runs past the caller's real window
    trades = []
    for (_, entry_time, entry_price, exit_time, exit_price, exit_reason,
         return_pct, armed, arm_time, arm_price, _rt, _rs, _rf) in rows:
        armed = bool(armed)
        trades.append({
            'Ticker': row_ticker,
            'Entry Time': pd.Timestamp(entry_time), 'Entry Price': entry_price,
            'Exit Time': pd.Timestamp(exit_time), 'Exit Price': exit_price,
            'exit_reason': exit_reason, 'Return': return_pct, 'armed': armed,
            'Arm Time': pd.Timestamp(arm_time) if armed and arm_time else None,
            'Arm Price': arm_price if armed else None,
        })
    return trades


# --- phase4_results: Phase4 aggregate-result persistence (Task #2, 2026-08-29 planner
# dispatch, PERF) -- sibling shape to candidate_verification_results above, but keyed on
# candidate_id ALONE (no 'phase' dimension -- this table only ever holds Phase4's own
# result, unlike candidate_verification_results which shares one table across Phase3/
# Phase5). Persists Phase4's real per-candidate aggregate output (checks 4/8/11/13,
# cliff-safety verdict, overlay summary, cagr) -- field names taken directly from
# candidate_summary_report.gt_rows_for_scope's real `out` dict (build_candidate_report_
# ground_truth's actual return shape), not invented. check13's 5 real per-fold values are
# summarized to worst_fold_cagr_pct/any_fold_fragile (matching this table's "aggregate
# result" scope, same granularity candidate_verification_results already uses for its own
# check-derived columns) rather than exploding into 15 columns the way GT_COLUMN_DEFS
# does for the full per-candidate CSV/xlsx export.
_PHASE4_VALUE_COLUMNS = [
    "cagr_pct", "robust_alpha_pct", "n_trades",
    "core_safe", "addon_safe", "core_addon_disagreement",
    "check4_early_wr_pct", "check4_late_wr_pct",
    "check8_compounded_pct", "check8_compounded_without_best_pct",
    "check8_best_trade_share_pct", "check8_too_few_trades",
    "check11_max_drawdown_pct",
    "check13_worst_fold_cagr_pct", "check13_any_fold_fragile",
    "addon_cagr_pct", "drought_compounded_pct", "drought_combined_compounded_pct",
    # trades_resolution (2026-09-07, Review-Gate Persistence Rule item -- paired-review
    # HIGH finding #3): which real trade source produced this row's cagr_pct/checks --
    # see candidate_summary_report.GT_COLUMN_DEFS' own entry for the real value set.
    # Persisted so a post-1s-fix phase4_results row is distinguishable from a pre-fix
    # one after the fact (previously computed and threaded all the way to the report's
    # `out` dict, but discarded before ever reaching this table).
    "trades_resolution", "second_build_id",
    # Phase4's own stacked overlay CAGRs (2026-09-08, Phase5-consolidation): Phase4 now
    # computes core+addon / core+drought / gated-triple-stack itself, off the same trade
    # list its own checks ran against (run_optimization_sweep._stacked_overlay_cagrs_gt),
    # so a report generator no longer needs candidate_verification_results' Phase5-written
    # addon_cagr_1s/drought_cagr_1s/core_both_cagr_1s for these. Stored as real PERCENTAGES
    # here (Phase5 stored the same quantities as raw fractions -- do not mix the two).
    # The `_ungated` suffix on the first two is deliberate and load-bearing (2026-09-08
    # paired-review MEDIUM finding): scripts/candidate_full_review.py already emits
    # in-memory keys named exactly `core_addon_cagr_pct`/`core_drought_cagr_pct` holding
    # the GATED flavor of the same concept (and scripts/build_portfolio_prototype.py reads
    # that flavor by name), so the unsuffixed names must NOT be reused here. Only
    # core_both_cagr_pct is the same quantity in both places. See run_optimization_sweep.
    # _stacked_overlay_cagrs_gt's NAMING WARNING.
    "core_addon_cagr_ungated_pct", "core_drought_cagr_ungated_pct", "core_both_cagr_pct",
    # Overlay-inclusive Check11/Check13 risk checks (2026-09-11, backlog item found
    # 2026-09-08 -- every check11/check13 above is CORE-only, so a promoted addon/
    # drought node had zero drawdown/fold-fragility verification on the overlay portion
    # of its equity curve). One (max_drawdown_pct, worst_fold_cagr_pct, any_fold_fragile,
    # n_folds_populated) quadruple per equity curve -- see run_optimization_sweep.
    # _overlay_risk_checks_gt's own docstring for exactly what each curve is, why the 3
    # core-inclusive combos carry an `_ungated` suffix (a real, deliberate naming-
    # collision fix -- these are NOT the same curve as this table's own gated
    # core_both_cagr_pct), why n_folds_populated exists (an empty check13 fold silently
    # reads as "not fragile" -- this lets a reader tell that apart from a genuinely
    # healthy fold after the fact), and why the drought-inclusive combos degrade to
    # their non-drought counterpart (not None) when drought found no real windows.
    "addon_only_max_drawdown_pct", "addon_only_worst_fold_cagr_pct", "addon_only_any_fold_fragile",
    "addon_only_n_folds_populated",
    "core_addon_ungated_max_drawdown_pct", "core_addon_ungated_worst_fold_cagr_pct",
    "core_addon_ungated_any_fold_fragile", "core_addon_ungated_n_folds_populated",
    "drought_only_max_drawdown_pct", "drought_only_worst_fold_cagr_pct", "drought_only_any_fold_fragile",
    "drought_only_n_folds_populated",
    "core_drought_ungated_max_drawdown_pct", "core_drought_ungated_worst_fold_cagr_pct",
    "core_drought_ungated_any_fold_fragile", "core_drought_ungated_n_folds_populated",
    "core_both_ungated_max_drawdown_pct", "core_both_ungated_worst_fold_cagr_pct",
    "core_both_ungated_any_fold_fragile", "core_both_ungated_n_folds_populated",
]


def ensure_phase4_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS phase4_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            checked_at TEXT NOT NULL,
            cagr_pct REAL, robust_alpha_pct REAL, n_trades INTEGER,
            core_safe INTEGER, addon_safe INTEGER, core_addon_disagreement INTEGER,
            check4_early_wr_pct REAL, check4_late_wr_pct REAL,
            check8_compounded_pct REAL, check8_compounded_without_best_pct REAL,
            check8_best_trade_share_pct REAL, check8_too_few_trades INTEGER,
            check11_max_drawdown_pct REAL,
            check13_worst_fold_cagr_pct REAL, check13_any_fold_fragile INTEGER,
            addon_cagr_pct REAL, drought_compounded_pct REAL, drought_combined_compounded_pct REAL,
            trades_resolution TEXT, second_build_id INTEGER,
            core_addon_cagr_ungated_pct REAL, core_drought_cagr_ungated_pct REAL,
            core_both_cagr_pct REAL,
            UNIQUE(candidate_id)
        )""")
    # Same sqlite "no ADD COLUMN IF NOT EXISTS" probe-first pattern as ensure_table()
    # above (Task #4, 2026-08-29 planner dispatch, STATE): phase4_checked_at is a
    # denormalized pointer into phase4_results, kept in sync by upsert_phase4() below.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(candidate_nodes)")}
    if "phase4_checked_at" not in existing_cols:
        conn.execute("ALTER TABLE candidate_nodes ADD COLUMN phase4_checked_at TEXT")
    # trades_resolution (2026-09-07, added to _PHASE4_VALUE_COLUMNS above -- same
    # probe-first ALTER pattern, existing phase4_results rows get NULL, same "old rows
    # just go unread as a resolution marker" convention this project already uses for
    # backtest_winner_trades' own fill_resolution column).
    existing_p4_cols = {row[1] for row in conn.execute("PRAGMA table_info(phase4_results)")}
    if "trades_resolution" not in existing_p4_cols:
        conn.execute("ALTER TABLE phase4_results ADD COLUMN trades_resolution TEXT")
    if "second_build_id" not in existing_p4_cols:
        conn.execute("ALTER TABLE phase4_results ADD COLUMN second_build_id INTEGER")
    # 2026-09-08 Phase5-consolidation, same probe-first ALTER pattern.
    #
    # The two `_ungated` columns were briefly written under the unsuffixed names
    # core_addon_cagr_pct/core_drought_cagr_pct earlier the same day, before the
    # gated-vs-ungated name collision with candidate_full_review.py was caught in paired
    # review. RENAME COLUMN (not a fresh ADD) so the real rows already persisted under the
    # old names keep their values instead of being stranded in a dead column -- sqlite has
    # supported ALTER TABLE ... RENAME COLUMN since 3.25 (this repo's runtime is 3.37+).
    # Guarded on "old present AND new absent" so it is a genuine one-time no-op afterwards,
    # same spirit as the probe-first ADDs around it.
    for _old, _new in (("core_addon_cagr_pct", "core_addon_cagr_ungated_pct"),
                       ("core_drought_cagr_pct", "core_drought_cagr_ungated_pct")):
        if _old in existing_p4_cols and _new not in existing_p4_cols:
            conn.execute(f"ALTER TABLE phase4_results RENAME COLUMN {_old} TO {_new}")
            existing_p4_cols.discard(_old)
            existing_p4_cols.add(_new)
    for _col in ("core_addon_cagr_ungated_pct", "core_drought_cagr_ungated_pct",
                 "core_both_cagr_pct"):
        if _col not in existing_p4_cols:
            conn.execute(f"ALTER TABLE phase4_results ADD COLUMN {_col} REAL")
    # Overlay-inclusive Check11/Check13 risk checks (2026-09-11), same probe-first ALTER
    # pattern -- any_fold_fragile/n_folds_populated columns are INTEGER (bool/count),
    # matching check13_any_fold_fragile's own type above.
    for _prefix in ("addon_only", "core_addon_ungated", "drought_only",
                     "core_drought_ungated", "core_both_ungated"):
        for _col, _type in ((f"{_prefix}_max_drawdown_pct", "REAL"),
                             (f"{_prefix}_worst_fold_cagr_pct", "REAL"),
                             (f"{_prefix}_any_fold_fragile", "INTEGER"),
                             (f"{_prefix}_n_folds_populated", "INTEGER")):
            if _col not in existing_p4_cols:
                conn.execute(f"ALTER TABLE phase4_results ADD COLUMN {_col} {_type}")


def get_stored_phase4(conn, candidate_id):
    """Returns a dict with keys 'checked_at' + all of _PHASE4_VALUE_COLUMNS, or None if
    no row exists yet for this candidate_id."""
    ensure_phase4_table(conn)
    row = conn.execute(
        "SELECT checked_at, " + ", ".join(_PHASE4_VALUE_COLUMNS) +
        " FROM phase4_results WHERE candidate_id=?", (candidate_id,)).fetchone()
    if row is None:
        return None
    return dict(zip(["checked_at"] + _PHASE4_VALUE_COLUMNS, row))


def upsert_phase4(conn, candidate_id, fields):
    """`fields` may be any dict containing (a subset of) _PHASE4_VALUE_COLUMNS as keys --
    anything not present is stored NULL, UNLESS a prior row already had a real value for
    that column (2026-08-30, paired-review HIGH finding, against candidate_summary_
    report.py's own candidate_nodes.core_safe/addon_safe consolidation onto this table):
    `core_safe`/`addon_safe` genuinely come back None from run_addon_cliff_safety_ground_
    truth when no neighbor cell evaluated (a real, documented fail-closed-to-unknown
    outcome, not a bug) -- a plain INSERT OR REPLACE would let that later, degraded/
    partial Phase4 pass silently clobber a previously-persisted real True/False verdict
    back to NULL, which the Phase5 SAFE/SAFE gate then reads as "unverified" and skips
    (the exact waste this whole verdict-persistence feature exists to prevent). Fix:
    merge with whatever row already exists first -- a None in `fields` keeps the prior
    stored value instead of overwriting it; a real value always wins (freshest-real-value
    semantics, same intent COALESCE(?, existing) would express in SQL, done in Python here
    since this file's own established convention is plain INSERT OR REPLACE/OR IGNORE, not
    ON CONFLICT DO UPDATE). Same real-candidate_id validation as before. Also stamps
    candidate_nodes.phase4_checked_at (Task #4, STATE) in the same call, same pattern
    upsert() already uses for phase3_checked_at/phase5_checked_at. Returns the checked_at
    timestamp written."""
    ensure_phase4_table(conn)
    exists = conn.execute("SELECT 1 FROM candidate_nodes WHERE id=?", (candidate_id,)).fetchone()
    if exists is None:
        raise ValueError(f"candidate_id={candidate_id} not found in candidate_nodes -- refusing to insert")
    prior = get_stored_phase4(conn, candidate_id)
    checked_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    cols = ["candidate_id", "checked_at"] + _PHASE4_VALUE_COLUMNS
    placeholders = ", ".join("?" for _ in cols)
    merged = [fields.get(c) if fields.get(c) is not None else (prior.get(c) if prior else None)
              for c in _PHASE4_VALUE_COLUMNS]
    values = [candidate_id, checked_at] + merged
    conn.execute(
        f"INSERT OR REPLACE INTO phase4_results ({', '.join(cols)}) VALUES ({placeholders})", values)
    conn.execute("UPDATE candidate_nodes SET phase4_checked_at=? WHERE id=?", (checked_at, candidate_id))
    conn.commit()
    return checked_at
