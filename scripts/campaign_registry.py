"""Campaign registry: single source of truth for the in-memory GT sweep
pipeline's `version` discriminator string, plus a real job queue + status
board for `bench_phase1_phase2_inmemory.py` + `run_inmemory_sweep_queue.sh`.

Implements docs/plans/campaign_registry_design.md (Task #8, 2026-08-31).
Built specifically to close a real, real-that-night incident: `run_inmemory_
sweep_queue.sh` used to hand-build its own `VERSION` bash string once at
script start, while `bench_phase1_phase2_inmemory.py` independently re-derived
its own version string per-process from on-disk `PROMOTION_ALGO_VERSION` --
zero shared source of truth. A mid-queue code commit (the `N_GENERATIONS`
PROMOTION_ALGO_VERSION 3->4 bump) landed while a queue process was already
running, so GDXU's TrailingBoth candidates landed tagged `-pv3` while
TrailingExit landed `-pv4`, and the outer script's stale `-pv3` Phase4/5
`--version` calls silently resolved zero scopes for the new rows. Fixed
same-night as a literal-string patch (commit matching both sides by hand).

THIS module deduplicates the version-string CONSTRUCTION logic
(`build_version_string`) into one function both sides call -- real, and
verified byte-identical to the old inline logic across every real input
combination (paired review, 2026-08-31). `run_inmemory_sweep_queue.sh`'s own
`resolve_campaign()` reads bench_phase1_phase2_inmemory.py's on-disk module
constants fresh via a Python import, ONCE at script start (not a hardcoded
shell literal) -- so there is now exactly ONE place these values live, which
closes the "two independently hand-maintained copies silently diverge from
an ordinary editing mistake" failure class. It does NOT close the harder
class the original incident actually was: a commit landing WHILE a
multi-hour queue is actively draining. A round-2 attempt at re-resolving
per-ticker (reassigning the drain loop's own $CAMPAIGN_ID/$VERSION mid-run)
was tried and reverted -- paired review proved by simulation it made the
incident WORSE (silently stranded remaining tickers' queued jobs while
printing "All done", AND ran Phase4/5 against a version matching none of the
current ticker's own rows). True elimination needs commit-pinning a whole
campaign to one git commit, e.g. a worktree per campaign -- not built here,
see docs/plans/campaign_registry_design.md's own "What this does NOT fix"
section, which named this exact residual gap before it was ever hit.

Judgment calls made building this (design doc's 4 open questions -- user said
"proceed" without addressing them individually):
  1. The legacy `run_sweep_queue.sh`/`run_optimization_sweep.py`/
     `campaign_config.patch_config` path is explicitly OUT OF SCOPE -- stays
     exactly as-is. This registry covers the in-memory GT pipeline only.
  2. OS-level CPU throttling (nice/cpulimit on a live PID) is NOT built and
     still isn't. UPDATED 2026-08-31 (folded in while this commit was already
     on hold, superseding this judgment call's original "inter-job only"
     scope): `workers_budget` is now genuinely dynamic, mid-job, not just
     inter-job. `bench_phase1_phase2_inmemory.py._dispatch` -- the shared
     function every real Phase1/Phase2-island/Phase2.5 grid pass submits work
     through -- pre-spawns its ProcessPoolExecutor at a fixed size (unchanged)
     but now gates how many tasks are ever IN FLIGHT at once via
     `get_workers_budget(version)`, read ONCE per `_dispatch` call (NOT polled
     mid-call -- a single call can process 400K+ real cells; see that
     function's own docstring for why). Raise/lower it live via
     `set_workers_budget`/the CLI's `set-workers-budget` subcommand -- takes
     effect at that process's NEXT `_dispatch` call (one bench process calls
     it many times per run: once per Phase1-coarse, once per Phase2-island
     generation x N_GENERATIONS, once per Phase2.5, per fixed_sl, per
     strategy -- so typically seconds to a few minutes, not a fixed poll
     interval). No pool resize needed either way. Workers a throttle never
     gives a task to have NOT paid their one-time numba JIT warmup yet
     (that cost is per-worker-FIRST-TASK, not per-spawn) -- raising the
     budget later can pay that cost on the newly-engaged workers, it isn't
     free. A campaign with no budget set (None) or a
     non-positive value is unthrottled (falls back to the pool's own
     max_workers) -- fails toward the ORIGINAL submit-everything-upfront
     behavior, never toward a silent hang.
  3. `campaigns.label` uniqueness is ADVISORY, not a hard DB constraint --
     `version_string` (the thing that actually prevents the real bug class)
     IS a hard UNIQUE constraint. A label collision across genuinely
     different param tuples is a human-review concern (matches the existing
     docs/plans/ground_truth_kernel_rebuild.md name-reservation convention,
     which is also advisory/human-checked, not code-enforced).
  4. Existing `sweep_run_log`/`candidate_nodes` rows are NOT backfilled with
     a `campaign_id` -- this registry applies going forward only.

Phase4/Phase5 throttling + pause (2026-08-31, real user ask, folded in while this
commit was already on hold, after pushback on ROI -- deliberate infra-debt paydown so
the whole Phase1-2.5/4/5 pipeline shares ONE consistent throttle mechanism, not chasing
a specific proven bottleneck): `run_throttled()` below is the SAME budget-gated
submit-all-vs-bounded-submission logic `bench_phase1_phase2_inmemory._dispatch` already
has (paired-reviewed clean across 4 rounds), factored out here as a reusable helper so
`candidate_summary_report.py` (Phase4, previously ZERO parallelism at all -- confirmed
by grep before this) and `phase5_second_level_overlay_check.py` (Phase5, already had its
own separate, unthrottled ProcessPoolExecutor) can both use the identical, already-
verified throttle instead of each hand-rolling a 3rd/4th copy of the same logic (the
exact "two independently-maintained copies can drift" failure class this whole file
exists to prevent). Deliberately does NOT refactor `_dispatch` itself to call this
shared helper -- that code is already paired-review-clean across 4 rounds and
re-touching it would reopen review surface for zero behavioral gain.

Pause semantics -- TWO SEPARATE CONTROLS, not one (user's own correction, 2026-08-31,
after an earlier draft of this tried to reuse workers_budget==0 as the pause signal and
the user caught a real gap): `workers_budget` is an INTRA-phase concurrency throttle --
it changes how many cells/candidates are in flight WITHIN an already-running phase's own
pool, and has NO memory implication (a pool's already-spawned workers stay alive at full
RSS regardless of how low the in-flight cap goes -- there's no mid-phase pool-shrink,
and no way to time a workers_budget=0 to land precisely at a point where memory could
even be freed). `paused` (new `campaigns.paused` column, default 0) is a SEPARATE,
INTER-phase/inter-job control -- checked only at boundaries where a subprocess has
already fully exited (between claim-next calls in run_inmemory_sweep_queue.sh's drain
loop, and between Phase1-2.5 -> Phase4 -> Phase5's own subprocess launches for one
ticker) -- exactly the points where a pool from the PRIOR phase has already torn down
and its memory is already reclaimed, so pausing there has a real, immediate memory
effect that workers_budget=0 never could. `is_paused()`/the CLI's `pause`/`resume`
subcommands operate on this column; `claim_next`/`claim-next` refuse to claim (a NEW,
distinct exit 3 -- NOT conflated with exit 2's genuinely-empty-queue meaning, the same
kind of two-different-things confusion round 2's `resolve_campaign` mistake also made
in a different form, see this docstring's own account above) when `paused` is set for
that campaign_id, and run_inmemory_sweep_queue.sh checks it explicitly before each
Phase4 and Phase5 subprocess launch too.

CLI (see main() below for the full arg list):
  create        -- explicit campaign creation (rarely needed directly; resolve
                    creates-if-missing)
  resolve       -- the real fix: builds+registers a version string from the
                    same inputs bench_phase1_phase2_inmemory.py already has,
                    prints ONLY the version string to stdout (bash-capturable
                    via $(...))
  enqueue       -- append a (ticker, strategy, fixed_sl_values) job to a
                    campaign's queue. Safe to run WHILE a drain loop is
                    already draining the same campaign_id -- this is the
                    actual "add work to a running campaign without killing
                    it" mechanism the standing sweep-manager backlog item
                    asked for.
  claim-next    -- atomically (BEGIN IMMEDIATE, SQLite's own writer
                    serialization) pop the oldest queued job for a campaign,
                    mark it running, print "job_id ticker strategy
                    fixed_sl_values" on one line. Prints nothing and exits 2
                    when the queue is genuinely empty (2026-08-31, paired-
                    review CONFIRMED HIGH fixup: a plain exit 1 made "queue
                    empty" and "a real error, e.g. a DB-lock timeout on
                    BEGIN IMMEDIATE" indistinguishable to a caller checking
                    `|| break` -- a transient failure could silently truncate
                    an unattended multi-hour campaign and still print "All
                    done." An unhandled exception now propagates normally
                    (Python's default exit 1), distinct from the explicit 2.
                    Exits 3 (new, 2026-08-31) when `paused` is set for this
                    campaign_id -- deliberately distinct from exit 2: real
                    queued work may still exist, the caller should wait and
                    retry, not treat this as done either.
  mark-finished -- record a claimed job's real outcome (rc).
  update-job-pid -- record the REAL worker process's pid for an already-
                    claimed job (2026-08-31, Task #9) -- claim-next itself
                    leaves pid NULL, since the CLI process that claims a
                    job is not the process that does the real work (see
                    claim_next's own docstring for the incident this
                    fixes).
  skip-remaining -- mark every still-queued job for one (campaign, ticker)
                    'skipped' -- used when a ticker's Phase1-2.5 run fails,
                    matching run_inmemory_sweep_queue.sh's existing "skip the
                    rest of this ticker, keep the queue going" behavior.
  status        -- queryable campaign + job summary, replaces log-tailing.
  set-workers-budget -- update an already-created campaign's workers_budget
                    live (2026-08-31, real mid-job dynamic CPU control --
                    bench_phase1_phase2_inmemory.py._dispatch reads this once
                    per dispatch call (not polled) and caps in-flight tasks
                    at it -- takes effect at that process's next dispatch
                    call (typically seconds to a few minutes, see _dispatch's
                    own docstring), no restart needed. See judgment call #2
                    above).
  pause/resume  -- set/clear `campaigns.paused` (2026-08-31, real inter-phase/
                    inter-job control, deliberately SEPARATE from workers_
                    budget -- see this module's own "Pause semantics" section
                    above for why). Checked between claim-next calls and
                    between Phase4/Phase5 subprocess launches, never mid-phase.
  is-paused     -- plain exit-code check (0=paused, 1=not) for shell `if`.
"""
import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import FIRST_COMPLETED, as_completed, wait
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run_optimization_sweep import window_version_suffix, DB_PATH as _RESEARCH_DB_PATH

# Env override (matches signals_config.py's existing TRADING_DB_PATH convention for the
# live DB) -- lets a smoke-test/CLI-integration-test run the real CLI end-to-end without
# ever touching the real trading_universe.db. Every function also accepts a per-call
# db_path= override for unit tests that want a fresh DB per test.
DB_PATH = os.environ.get("CAMPAIGN_REGISTRY_DB_PATH", str(_RESEARCH_DB_PATH))

_CLAIM_LOCK_TIMEOUT_SECS = 60.0


def ensure_tables(db_path=None):
    db_path = db_path or DB_PATH
    with sqlite3.connect(db_path, timeout=60.0) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT,
                promotion_algo_version INTEGER NOT NULL,
                data_source TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                z_thresholds TEXT,
                n_islands INTEGER,
                seed_watch_list_id INTEGER,
                workers_budget INTEGER,
                paused INTEGER NOT NULL DEFAULT 0,
                version_string TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                created_by TEXT,
                notes TEXT
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS campaign_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
                ticker TEXT NOT NULL,
                strategy TEXT NOT NULL,
                fixed_sl_values TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                pid INTEGER,
                queued_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                rc INTEGER
            )""")
        # ADD COLUMN guard (2026-08-31, paired-review CONFIRMED HIGH fixup, both
        # independent reviewers): `paused` was added to the CREATE TABLE above AFTER
        # `campaigns` already existed against every DB this file's own paired review used
        # this session -- CREATE TABLE IF NOT EXISTS is a no-op against an existing table,
        # so any of those pre-existing DBs would hit `sqlite3.OperationalError: no such
        # column: paused` on `pause`/`set_paused`, while `is_paused`'s own fail-safe
        # `except Exception: return False` would silently swallow that error and report
        # "not paused" forever -- exactly the schema-drift class this project's own
        # `PRAGMA table_info` probe-first convention exists to prevent (see
        # candidate_verification_store.ensure_phase4_table's identical pattern for
        # `phase4_checked_at`). Same probe here, run every ensure_tables() call (cheap --
        # a single PRAGMA read) so this is safe against ANY existing campaigns table,
        # not just the ones this session happened to create.
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(campaigns)")}
        if "paused" not in existing_cols:
            conn.execute("ALTER TABLE campaigns ADD COLUMN paused INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def build_version_string(label, promotion_algo_version, data_source, window_start, window_end,
                          z_thresholds=None, n_islands=None, seed_watch_list_id=None):
    """Pure -- no I/O. Same construction bench_phase1_phase2_inmemory.py's
    _build_version_string used to do inline; moved here so it's the ONE place
    this logic lives (see this module's own docstring for the incident this
    closes). z_thresholds: pass None to omit the -z suffix (matches the
    original's `args.z_thresholds is not None` gate -- NOT the same as
    passing an empty list)."""
    prefix = f"{label}-" if label else ""
    version = (prefix + "bench-inmemory-v6"
               + ("-massive" if data_source == "massive" else "")
               + window_version_suffix(window_start, window_end))
    if z_thresholds is not None:
        version += f"-z{'-'.join(str(z) for z in z_thresholds)}"
    if seed_watch_list_id is not None:
        version += f"-seed{seed_watch_list_id}"
    if n_islands is not None:
        version += f"-isl{n_islands}"
    version += f"-pv{promotion_algo_version}"
    return version


def get_workers_budget(version_string, db_path=None):
    """Live read, by version_string (not campaign_id -- bench_phase1_phase2_inmemory.py's
    _dispatch already has `version` in scope at every call site, avoiding threading
    campaign_id through the whole call chain just for this). Returns None if the campaign
    row doesn't exist yet, workers_budget was never set, OR the read itself fails for any
    reason (paired-review CONFIRMED HIGH, 2026-08-31: this is called from inside
    bench_phase1_phase2_inmemory.py._dispatch, the hot path of a real multi-hour sweep, on a
    DB that's concurrently written by the sweep's own tables plus any enqueue/claim-next/
    mark-finished/status call -- an unhandled sqlite3.OperationalError here, e.g. a lock
    timeout, used to propagate straight out of _dispatch and kill the bench process,
    discarding hours of completed in-memory work; that directly contradicted _dispatch's own
    documented 'fails toward unthrottled, never toward a crash' contract). Caller (_dispatch)
    treats None as 'no cap, use the pool's own max_workers', never as 'pause'."""
    db_path = db_path or DB_PATH
    try:
        with sqlite3.connect(db_path, timeout=60.0) as conn:
            row = conn.execute("SELECT workers_budget FROM campaigns WHERE version_string = ?",
                                (version_string,)).fetchone()
        return row[0] if row and row[0] is not None else None
    except Exception:
        return None


def set_workers_budget(campaign_id, workers_budget, db_path=None):
    """The real mid-run control lever get_workers_budget/_dispatch's once-per-call read
    exists to serve -- update an already-created campaign's budget. Keyed by campaign_id (not
    version_string) to match every other mutating CLI verb's convention (enqueue/
    claim-next/mark-finished/skip-remaining/status all take --campaign-id). Returns True
    if a row was actually updated, False if campaign_id doesn't exist."""
    db_path = db_path or DB_PATH
    with sqlite3.connect(db_path, timeout=60.0) as conn:
        cur = conn.execute("UPDATE campaigns SET workers_budget = ? WHERE id = ?",
                            (workers_budget, campaign_id))
        conn.commit()
        return cur.rowcount > 0


def is_paused(campaign_id, db_path=None):
    """The INTER-phase/inter-job pause check (2026-08-31, see this module's own docstring
    for why this is deliberately separate from workers_budget). Fails toward NOT paused on
    any DB error, same fail-safe direction as get_workers_budget -- a broken pause check
    must never accidentally halt a real campaign. Returns False (not an exception) for an
    unknown campaign_id too, matching get_workers_budget's own None-for-unknown contract
    (a caller with a stale/wrong id should not get stuck, it should proceed and let the
    real work surface the id problem some other way). Calls ensure_tables() itself
    (2026-08-31, paired-review CONFIRMED HIGH fixup, both independent reviewers) -- unlike
    get_workers_budget (whose column has existed since this file's very first version),
    `paused` is new and this is a real, reproduced failure mode otherwise: a `campaigns`
    table created by an EARLIER version of this file (before `paused` existed) has no such
    column, and relying on some OTHER function to have already called ensure_tables() on
    that exact db_path first is not a safe assumption for a standalone pause/resume check."""
    db_path = db_path or DB_PATH
    try:
        ensure_tables(db_path)
        with sqlite3.connect(db_path, timeout=60.0) as conn:
            row = conn.execute("SELECT paused FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def set_paused(campaign_id, paused, db_path=None):
    """Real pause/resume lever -- checked between subprocess launches (run_inmemory_
    sweep_queue.sh, before each Phase4/Phase5 launch and before each claim-next) and NOT
    mid-phase, since a subprocess that's already exited has already torn down its own
    pool -- real memory reclaimed, unlike workers_budget=0's mid-flight no-op. Returns
    True if a row was actually updated, False if campaign_id doesn't exist. Calls
    ensure_tables() itself first (2026-08-31, paired-review CONFIRMED HIGH fixup) --
    see is_paused's own docstring for why this is real, not defensive-programming
    theater: a `campaigns` table from before `paused` existed has no such column."""
    db_path = db_path or DB_PATH
    ensure_tables(db_path)
    with sqlite3.connect(db_path, timeout=60.0) as conn:
        cur = conn.execute("UPDATE campaigns SET paused = ? WHERE id = ?",
                            (1 if paused else 0, campaign_id))
        conn.commit()
        return cur.rowcount > 0


def register_campaign(version_string, label, promotion_algo_version, data_source,
                       window_start, window_end, z_thresholds=None, n_islands=None,
                       seed_watch_list_id=None, workers_budget=None, created_by=None,
                       notes=None, db_path=None):
    """Idempotent on version_string (the real UNIQUE constraint) -- a second
    call with the SAME version_string returns the existing row's id, never
    inserts a duplicate. Returns campaign_id."""
    db_path = db_path or DB_PATH
    ensure_tables(db_path)
    z_str = ",".join(str(z) for z in z_thresholds) if z_thresholds is not None else None
    with sqlite3.connect(db_path, timeout=60.0) as conn:
        row = conn.execute("SELECT id FROM campaigns WHERE version_string = ?",
                            (version_string,)).fetchone()
        if row:
            return row[0]
        cur = conn.execute("""
            INSERT INTO campaigns (label, promotion_algo_version, data_source, window_start,
                window_end, z_thresholds, n_islands, seed_watch_list_id, workers_budget,
                version_string, created_at, created_by, notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (label, promotion_algo_version, data_source, window_start, window_end, z_str,
              n_islands, seed_watch_list_id, workers_budget, version_string,
              time.strftime("%Y-%m-%dT%H:%M:%S"), created_by, notes))
        conn.commit()
        return cur.lastrowid


def resolve_or_create(label, promotion_algo_version, data_source, window_start, window_end,
                       z_thresholds=None, n_islands=None, seed_watch_list_id=None,
                       workers_budget=None, created_by=None, notes=None, db_path=None):
    """build_version_string + register_campaign in one call -- what the CLI
    `resolve` subcommand and bench_phase1_phase2_inmemory.py's main() both
    use. Returns (campaign_id, version_string)."""
    version_string = build_version_string(label, promotion_algo_version, data_source,
                                           window_start, window_end, z_thresholds, n_islands,
                                           seed_watch_list_id)
    campaign_id = register_campaign(version_string, label, promotion_algo_version, data_source,
                                     window_start, window_end, z_thresholds, n_islands,
                                     seed_watch_list_id, workers_budget, created_by, notes,
                                     db_path)
    return campaign_id, version_string


def enqueue(campaign_id, ticker, strategy, fixed_sl_values, db_path=None):
    """fixed_sl_values: comma-joined string (e.g. '1,2,3,4,5,6,7,8'), matching
    the shell script's own Z_SUFFIX join convention -- kept as a single opaque
    field here since this module never needs to parse it, only pass it
    through to the real bench_phase1_phase2_inmemory.py invocation. Returns
    the new job's id."""
    db_path = db_path or DB_PATH
    ensure_tables(db_path)
    with sqlite3.connect(db_path, timeout=60.0) as conn:
        cur = conn.execute("""
            INSERT INTO campaign_jobs (campaign_id, ticker, strategy, fixed_sl_values,
                status, queued_at)
            VALUES (?, ?, ?, ?, 'queued', ?)
        """, (campaign_id, ticker, strategy, fixed_sl_values, time.strftime("%Y-%m-%dT%H:%M:%S")))
        conn.commit()
        return cur.lastrowid


def claim_next(campaign_id=None, db_path=None):
    """Atomically pops the oldest 'queued' job (optionally scoped to one
    campaign_id) and marks it 'running'. BEGIN IMMEDIATE acquires SQLite's
    RESERVED lock for the whole read-then-update, so two processes racing
    claim_next() can never both win the same row -- SQLite serializes
    writers, the loser's BEGIN IMMEDIATE simply blocks until the winner
    commits, then sees the row already flipped to 'running' and moves on.
    Returns a dict (id, ticker, strategy, fixed_sl_values) or None if the
    queue (scoped or global) is empty.

    pid is left NULL here, not os.getpid() (2026-08-31, Task #9, real bug found live
    testing against DPST): claim-next is invoked as its OWN short-lived CLI subprocess
    by run_inmemory_sweep_queue.sh (`CLAIMED=$($PYTHON campaign_registry.py claim-next
    ...)`), which exits the instant it prints the claimed job -- os.getpid() here was
    recording THAT ephemeral process's pid, not the real long-running bench_phase1_
    phase2_inmemory.py worker the shell launches afterward, so `status` showed a
    stale/already-exited pid for the entire duration of the real work. A wrong-looking-
    real number is worse than an honest 'not yet known' -- see update_job_pid below,
    which the shell now calls with the REAL worker pid right after backgrounding it."""
    db_path = db_path or DB_PATH
    ensure_tables(db_path)
    conn = sqlite3.connect(db_path, timeout=_CLAIM_LOCK_TIMEOUT_SECS, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        q = "SELECT id, ticker, strategy, fixed_sl_values FROM campaign_jobs WHERE status='queued'"
        params = []
        if campaign_id is not None:
            q += " AND campaign_id=?"
            params.append(campaign_id)
        q += " ORDER BY id LIMIT 1"
        row = conn.execute(q, params).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        job_id, ticker, strategy, fixed_sl_values = row
        conn.execute("UPDATE campaign_jobs SET status='running', pid=NULL, started_at=? WHERE id=?",
                     (time.strftime("%Y-%m-%dT%H:%M:%S"), job_id))
        conn.execute("COMMIT")
        return dict(id=job_id, ticker=ticker, strategy=strategy, fixed_sl_values=fixed_sl_values)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def update_job_pid(job_id, pid, db_path=None):
    """Records the REAL worker process's pid for an already-claimed job (2026-08-31,
    Task #9) -- called by run_inmemory_sweep_queue.sh right after backgrounding the real
    bench_phase1_phase2_inmemory.py process and capturing its pid via `$!`, so `status`
    shows the process that's actually doing the work instead of claim-next's own already-
    exited CLI pid (see claim_next's own docstring for the incident this fixes). Returns
    True if a row was actually updated, False if job_id doesn't exist."""
    db_path = db_path or DB_PATH
    with sqlite3.connect(db_path, timeout=60.0) as conn:
        cur = conn.execute("UPDATE campaign_jobs SET pid = ? WHERE id = ?", (pid, job_id))
        conn.commit()
        return cur.rowcount > 0


def mark_finished(job_id, rc, db_path=None):
    db_path = db_path or DB_PATH
    with sqlite3.connect(db_path, timeout=60.0) as conn:
        conn.execute("""
            UPDATE campaign_jobs SET status=?, finished_at=?, rc=? WHERE id=?
        """, ('done' if rc == 0 else 'failed', time.strftime("%Y-%m-%dT%H:%M:%S"), rc, job_id))
        conn.commit()


def skip_remaining(campaign_id, ticker, db_path=None):
    """Marks every still-'queued' job for (campaign_id, ticker) 'skipped' --
    matches run_inmemory_sweep_queue.sh's existing behavior of abandoning the
    rest of a ticker's work (but not the whole queue) once one of its jobs
    fails. Returns the number of rows skipped."""
    db_path = db_path or DB_PATH
    with sqlite3.connect(db_path, timeout=60.0) as conn:
        cur = conn.execute("""
            UPDATE campaign_jobs SET status='skipped', finished_at=?
            WHERE campaign_id=? AND ticker=? AND status='queued'
        """, (time.strftime("%Y-%m-%dT%H:%M:%S"), campaign_id, ticker))
        conn.commit()
        return cur.rowcount


def status(campaign_id=None, label=None, db_path=None):
    """Returns a dict summary: campaign row(s) + per-status job counts +
    the real still-running jobs (ticker/strategy/pid/started_at). Read-only."""
    db_path = db_path or DB_PATH
    ensure_tables(db_path)
    with sqlite3.connect(db_path, timeout=60.0) as conn:
        conn.row_factory = sqlite3.Row
        q = "SELECT * FROM campaigns WHERE 1=1"
        params = []
        if campaign_id is not None:
            q += " AND id=?"
            params.append(campaign_id)
        if label is not None:
            q += " AND label=?"
            params.append(label)
        q += " ORDER BY id DESC"
        campaigns = [dict(r) for r in conn.execute(q, params).fetchall()]
        out = []
        for c in campaigns:
            counts = dict(conn.execute("""
                SELECT status, COUNT(*) FROM campaign_jobs WHERE campaign_id=? GROUP BY status
            """, (c['id'],)).fetchall())
            running = [dict(r) for r in conn.execute("""
                SELECT ticker, strategy, pid, started_at FROM campaign_jobs
                WHERE campaign_id=? AND status='running' ORDER BY started_at
            """, (c['id'],)).fetchall()]
            out.append(dict(campaign=c, job_counts=counts, running=running))
        return out


def _print_status(rows):
    if not rows:
        print("No matching campaign(s).")
        return
    for r in rows:
        c = r['campaign']
        print(f"campaign id={c['id']} label={c['label']!r} version={c['version_string']}")
        print(f"  pv={c['promotion_algo_version']} data_source={c['data_source']} "
              f"window={c['window_start']}..{c['window_end']} z={c['z_thresholds']} "
              f"n_islands={c['n_islands']} workers_budget={c['workers_budget']} "
              f"paused={bool(c['paused'])}")
        print(f"  jobs: {r['job_counts'] or '(none enqueued)'}")
        for j in r['running']:
            print(f"    RUNNING {j['ticker']:6s} {j['strategy']:28s} pid={j['pid']} "
                  f"since {j['started_at']}")
        print()


def run_throttled(pool, submit_fn, tasks, version, on_result):
    """Generic budget-gated dispatch, factored out of bench_phase1_phase2_inmemory.py's
    already paired-review-clean `_dispatch` (2026-08-31, Task #8 further follow-up) so
    Phase4 (candidate_summary_report.py) and Phase5 (phase5_second_level_overlay_check.py)
    can share the exact same throttle logic instead of each hand-rolling a copy -- see this
    module's own docstring for the full reasoning. `_dispatch` itself is NOT refactored to
    call this (already reviewed clean, not worth reopening for zero behavioral gain).

    submit_fn(pool, task) -> Future -- caller-supplied so each pool's own real worker-
    function signature (different per caller) doesn't need to be forced into one shape.
    on_result(task, result_or_exception) -- called EXACTLY ONCE per task in `tasks`, in
    THIS function's own calling thread (never inside a worker), so callers can safely
    print/append/re-raise without needing their own synchronization. This "exactly once
    per task" guarantee is load-bearing for at least one real caller (Phase4's own
    completeness bookkeeping in candidate_summary_report.run_gt_mode, flagged by paired
    review, 2026-08-31) -- don't change the submission/completion loop below without
    preserving it. Passed either the real result or the raised Exception -- each caller
    decides its own error-handling posture (bench's _dispatch counts+continues; Phase5's
    original code let a worker exception propagate via fut.result(); Phase4's original
    loop printed 'skipping' and continued) -- this helper does not impose one.

    Same fail-safe contract as _dispatch: get_workers_budget failing for any reason, or
    returning None/non-positive, falls back to unthrottled submit-all (uses the pool's own
    max_workers) -- never toward a hang or a crash from the budget lookup itself."""
    tasks = list(tasks)
    max_workers = pool._max_workers
    b = get_workers_budget(version)
    budget = max_workers if not isinstance(b, int) or b <= 0 else max(1, min(b, max_workers))

    if budget >= max_workers:
        futures_map = {submit_fn(pool, task): task for task in tasks}
        for future in as_completed(futures_map):
            task = futures_map[future]
            try:
                res = future.result()
            except Exception as e:
                on_result(task, e)
            else:
                on_result(task, res)
        return

    task_iter = iter(tasks)
    in_flight = {}

    def _submit_next():
        try:
            task = next(task_iter)
        except StopIteration:
            return False
        in_flight[submit_fn(pool, task)] = task
        return True

    while len(in_flight) < budget and _submit_next():
        pass
    while in_flight:
        done, _pending = wait(in_flight.keys(), return_when=FIRST_COMPLETED)
        for future in done:
            task = in_flight.pop(future)
            try:
                res = future.result()
            except Exception as e:
                on_result(task, e)
            else:
                on_result(task, res)
        while len(in_flight) < budget and _submit_next():
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    def _campaign_args(p, required=True):
        p.add_argument('--label', default=None)
        p.add_argument('--promotion-algo-version', type=int, required=required)
        p.add_argument('--data-source', default='massive')
        p.add_argument('--window-start', required=required)
        p.add_argument('--window-end', required=required)
        p.add_argument('--z-thresholds', default=None,
                        help="comma-joined, e.g. '0.5,1.0,1.5,2.0'; omit for no -z suffix")
        p.add_argument('--n-islands', type=int, default=None)
        p.add_argument('--seed-watch-list-id', type=int, default=None)
        p.add_argument('--workers-budget', type=int, default=None)
        p.add_argument('--created-by', default=None)
        p.add_argument('--notes', default=None)

    p_create = sub.add_parser('create')
    _campaign_args(p_create)
    p_resolve = sub.add_parser('resolve')
    _campaign_args(p_resolve)

    p_enqueue = sub.add_parser('enqueue')
    p_enqueue.add_argument('--campaign-id', type=int, required=True)
    p_enqueue.add_argument('--ticker', required=True)
    p_enqueue.add_argument('--strategy', required=True)
    p_enqueue.add_argument('--fixed-sl-values', required=True,
                            help="comma-joined, e.g. '1,2,3,4,5,6,7,8'")

    p_claim = sub.add_parser('claim-next')
    p_claim.add_argument('--campaign-id', type=int, default=None)

    p_finish = sub.add_parser('mark-finished')
    p_finish.add_argument('--job-id', type=int, required=True)
    p_finish.add_argument('--rc', type=int, required=True)

    p_pid = sub.add_parser('update-job-pid')
    p_pid.add_argument('--job-id', type=int, required=True)
    p_pid.add_argument('--pid', type=int, required=True)

    p_skip = sub.add_parser('skip-remaining')
    p_skip.add_argument('--campaign-id', type=int, required=True)
    p_skip.add_argument('--ticker', required=True)

    p_status = sub.add_parser('status')
    p_status.add_argument('--campaign-id', type=int, default=None)
    p_status.add_argument('--label', default=None)

    p_setbudget = sub.add_parser('set-workers-budget')
    p_setbudget.add_argument('--campaign-id', type=int, required=True)
    p_setbudget.add_argument('--workers-budget', type=int, required=True)

    p_pause = sub.add_parser('pause')
    p_pause.add_argument('--campaign-id', type=int, required=True)

    p_resume = sub.add_parser('resume')
    p_resume.add_argument('--campaign-id', type=int, required=True)

    p_ispaused = sub.add_parser('is-paused')
    p_ispaused.add_argument('--campaign-id', type=int, required=True)

    args = ap.parse_args()

    def _z(argstr):
        return [float(x) for x in argstr.split(',')] if argstr else None

    if args.cmd in ('create', 'resolve'):
        campaign_id, version_string = resolve_or_create(
            label=args.label, promotion_algo_version=args.promotion_algo_version,
            data_source=args.data_source, window_start=args.window_start,
            window_end=args.window_end, z_thresholds=_z(args.z_thresholds),
            n_islands=args.n_islands, seed_watch_list_id=args.seed_watch_list_id,
            workers_budget=args.workers_budget, created_by=args.created_by, notes=args.notes)
        if args.cmd == 'resolve':
            print(version_string)
        else:
            print(f"campaign_id={campaign_id} version={version_string}")
    elif args.cmd == 'enqueue':
        job_id = enqueue(args.campaign_id, args.ticker, args.strategy, args.fixed_sl_values)
        print(job_id)
    elif args.cmd == 'claim-next':
        # Paused check BEFORE attempting to claim (2026-08-31) -- exit 3, deliberately
        # distinct from exit 2's genuinely-empty-queue meaning: real queued work may
        # still exist, the caller should wait and retry, NOT treat this as "done."
        if args.campaign_id is not None and is_paused(args.campaign_id):
            sys.exit(3)
        job = claim_next(args.campaign_id)
        if job is None:
            # Exit 2 == genuinely empty queue, NOT an error. Deliberately distinct
            # from an unhandled exception's default exit 1 -- see this subcommand's
            # own --help text above for the incident this distinction fixes.
            sys.exit(2)
        print(f"{job['id']} {job['ticker']} {job['strategy']} {job['fixed_sl_values']}")
    elif args.cmd == 'mark-finished':
        mark_finished(args.job_id, args.rc)
    elif args.cmd == 'update-job-pid':
        ok = update_job_pid(args.job_id, args.pid)
        if not ok:
            print(f"No job with id={args.job_id}", file=sys.stderr)
            sys.exit(1)
        print(f"job_id={args.job_id} pid={args.pid}")
    elif args.cmd == 'skip-remaining':
        n = skip_remaining(args.campaign_id, args.ticker)
        print(n)
    elif args.cmd == 'status':
        _print_status(status(args.campaign_id, args.label))
    elif args.cmd == 'set-workers-budget':
        ok = set_workers_budget(args.campaign_id, args.workers_budget)
        if not ok:
            print(f"No campaign with id={args.campaign_id}", file=sys.stderr)
            sys.exit(1)
        print(f"campaign_id={args.campaign_id} workers_budget={args.workers_budget}")
    elif args.cmd in ('pause', 'resume'):
        ok = set_paused(args.campaign_id, args.cmd == 'pause')
        if not ok:
            print(f"No campaign with id={args.campaign_id}", file=sys.stderr)
            sys.exit(1)
        print(f"campaign_id={args.campaign_id} paused={args.cmd == 'pause'}")
    elif args.cmd == 'is-paused':
        # Plain exit-code interface for shell `if` checks -- 0=paused, 1=not paused.
        sys.exit(0 if is_paused(args.campaign_id) else 1)


if __name__ == "__main__":
    main()
