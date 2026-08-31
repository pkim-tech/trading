# Campaign registry — design proposal (implemented)

Status: **implemented 2026-08-31 (Task #8)**, `scripts/campaign_registry.py` +
`bench_phase1_phase2_inmemory.py`/`run_inmemory_sweep_queue.sh`/
`candidate_summary_report.py`/`phase5_second_level_overlay_check.py` changes.
Paired-reviewed across 7 rounds (independent-cold + contextual Opus, both re-run on
each new addition). Scope grew beyond this doc's original 4 open questions during
implementation, per real-time user asks -- see those files' own module docstrings/
comments for the final, as-built design (this doc is kept for the original problem
statement/rationale, not as a living spec of every addition since). Key deltas from
the original proposal below, decided during implementation:
- Judgment calls #1-4 (original open questions) resolved as documented in
  `campaign_registry.py`'s own module docstring.
- `workers_budget` ended up genuinely dynamic/mid-job (not just inter-job as
  originally scoped) -- `_dispatch`/Phase4/Phase5 all gate in-flight task submission
  against it via the shared `run_throttled` helper.
- A SEPARATE `paused` column + `pause`/`resume`/`is-paused` CLI, checked only at
  real subprocess-exit boundaries (not mid-phase, unlike workers_budget) -- added
  after an initial attempt to reuse `workers_budget==0` as a pause signal was
  correctly rejected (two different things, would have re-created the exact
  conflation-of-signals failure class this whole task exists to fix).
- Phase4 (`candidate_summary_report.py`) gained real parallelism (previously zero)
  and Phase5's existing pool was rewired onto the same throttle.
- The version-string split-brain fix itself went through 3 real design iterations
  (see `run_inmemory_sweep_queue.sh`'s own header comment for the full account) --
  round 2's "re-resolve fresh per ticker" attempt was proven by paired review to
  make the incident WORSE and was reverted; the final version only removes the
  hardcoded-duplicate-literal class of drift, not the harder "commit lands while
  the queue is actively draining" class (still open, see that file's own account).

## Problem this solves

Two real, related pieces of friction:

1. **Version-string split-brain** (real incident, 2026-08-31 tonight): `run_inmemory_sweep_queue.sh`
   computes its bash `VERSION` string once at script start; `bench_phase1_phase2_inmemory.py`
   independently re-derives its own version string per-process from on-disk
   `PROMOTION_ALGO_VERSION` + CLI flags. A mid-queue code commit (this session's own
   `N_GENERATIONS` change) bumped the Python side's constant while the already-running
   shell process kept its stale value — GDXU's TrailingBoth candidates landed tagged
   `-pv3`, TrailingExit landed `-pv4`, and the outer script's Phase4/5 `--version` calls
   (still holding the stale string) silently resolved zero scopes for the new rows.
   Today's fix (commit `0e1e42f` and the `PROMOTION_ALGO_VERSION` bump) is a same-night
   patch, not a structural one — the two sides still independently compute the same
   string with zero shared source of truth, so the exact same class of drift will recur
   the next time either side changes without the other being edited in lockstep.

2. **No job queue / mid-sweep control** (`docs/backlog_cache.md`, "sweep manager" item,
   raised 2026-08-29, recurred 2026-08-31): the only way to add work to a running
   campaign is to kill it and relaunch with a longer `&&`-chained command list. No way to
   append a ticker to a running queue, or see "what's actually running right now" other
   than tailing a log file.

The user's insight tonight: these are the same infrastructure gap. A registry that is
the single source of truth for "what version/params is this run" is naturally also the
thing that can hold a job queue and answer "what's running" — instead of building two
separate mechanisms.

## Scope boundary

This design covers the **in-memory GT pipeline** (`bench_phase1_phase2_inmemory.py` +
`run_inmemory_sweep_queue.sh`) only — matching the "Standard, 2026-08-16" convention of
scripts owning their own scope. `run_sweep_queue.sh`/`run_optimization_sweep.py` (the
legacy `backtest_cache` path) is a separate, still-real consumer with its own
`campaign_config.patch_config` convention; whether it adopts the same registry later is
a follow-on decision, not part of this proposal.

## 1. State model: a `campaigns` table, single source of truth for the version string

New table in `trading_universe.db` (same DB `sweep_run_log`/`candidate_nodes` already
live in — no new DB file, no cross-DB join problem):

```sql
CREATE TABLE campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,              -- e.g. "v6.5" -- the human-memorable name,
                                       -- FOREIGN to the docs/plans/ground_truth_kernel_
                                       -- rebuild.md name-reservation convention, not a
                                       -- replacement for it -- reserving a name in that
                                       -- doc still happens first, this table just makes
                                       -- the reservation machine-readable too.
    promotion_algo_version INTEGER NOT NULL,   -- what was PROMOTION_ALGO_VERSION
    data_source TEXT NOT NULL,        -- "massive" | "yahoo" (mirrors DATA_SOURCE)
    window_start TEXT NOT NULL,       -- START
    window_end TEXT NOT NULL,         -- END
    z_thresholds TEXT,                -- "0.5,1.0,1.5,2.0" or NULL (default grid)
    n_islands INTEGER,                -- NULL = default
    seed_watch_list_id INTEGER,       -- NULL unless smoke-test mode
    version_string TEXT NOT NULL UNIQUE,  -- the FULL derived string, computed ONCE here
                                           -- (see #2) and never independently rebuilt
    created_at TEXT NOT NULL,
    created_by TEXT,                  -- free text: "planner dispatch", "user", etc.
    notes TEXT
)
```

`version_string` is written by whichever side *creates* the campaign (almost always the
Python side, since it already owns `_build_version_string`) and is the row every
consumer reads back — nobody re-derives it from parts ever again. This directly answers
the "how do both sides resolve against ONE shared source" requirement in #2 below.

## 2. Shared-source resolution: `scripts/campaign_registry.py`

A new small script, callable from both Python (import) and bash (CLI), that is the
*only* place version-string construction logic lives. Sketch of its interface (no
implementation yet):

```
python scripts/campaign_registry.py create \
    --label v6.5 --promotion-algo-version 4 --data-source massive \
    --window-start 2021-08-23 --window-end 2026-08-21 \
    --z-thresholds 0.5,1.0,1.5,2.0 --n-islands 3
# -> prints campaign_id and the resolved version_string, and INSERTs the row

python scripts/campaign_registry.py resolve --label v6.5
# -> prints the version_string for the (label, promotion_algo_version, ...) tuple
#    that matches CURRENT on-disk module state, creating the row if it doesn't
#    exist yet (idempotent on the full param tuple, not just the label)

python scripts/campaign_registry.py status [--campaign-id N | --label v6.5]
# -> queryable status (see #4)
```

**How this actually closes the split-brain, concretely:**
- `bench_phase1_phase2_inmemory.py`'s `_build_version_string(args)` stops building the
  string itself. It calls `campaign_registry.resolve_or_create(...)` with its own
  module-level constants (`PROMOTION_ALGO_VERSION`, `DATA_SOURCE`, `START`/`END`) plus
  `args`, exactly the same inputs it uses today — the function's *inputs* don't change,
  only where the string gets assembled.
- `run_inmemory_sweep_queue.sh` stops hand-building `$VERSION` in bash string
  concatenation. It calls `python scripts/campaign_registry.py resolve --label v6.5`
  once at script start (same timing as today — this does NOT fix the mid-queue-commit
  problem, see "what this does NOT fix" below) and captures the printed string.
- Because both sides now call the *same function* (one in-process, one via subprocess),
  a change to version-string construction logic only has to be made in one file
  (`campaign_registry.py`) instead of two, eliminating the manual-sync convention that
  has already caused two real incidents (the `-pv` suffix miss earlier tonight, and this
  incident).

**What this does NOT fix on its own**: a long-running queue process that started before
a mid-flight code change will still hold a stale in-memory string for the rest of its
run, same as today — resolving the version string from one shared function doesn't make
a running bash process re-read it. Actually solving "a running campaign should not
silently drift mid-run" needs one of: (a) the campaign-pinning-to-a-commit idea already
noted in the backlog item (git worktree + symlinked `cache/`/`logs`/`output/`), or (b)
the job queue (#3 below) checking the registry's `version_string` fresh *per job* instead
of once per queue-script invocation — since each job is already a fresh Python process,
(b) is nearly free once the queue is registry-driven, and is the recommended direction
rather than pursuing (a) separately.

## 3. Job-queue table + interface

Second table, one row per **job** — the natural unit is already `(ticker, strategy)`,
matching `run_inmemory_sweep_queue.sh`'s existing inner loop (one
`bench_phase1_phase2_inmemory.py` invocation per ticker × strategy, looping `fixed_sl`
1-8 *inside* that one process via `--fixed-sl-values`, per tonight's `bench_phase1_
phase2_inmemory.py` change):

```sql
CREATE TABLE campaign_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    ticker TEXT NOT NULL,
    strategy TEXT NOT NULL,
    fixed_sl_values TEXT NOT NULL,   -- "1,2,3,4,5,6,7,8"
    status TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|failed
    pid INTEGER,
    started_at TEXT,
    finished_at TEXT,
    rc INTEGER,                       -- exit code, once finished/failed
    queued_at TEXT NOT NULL
)
```

Interface (again sketch, not implementation):

```
python scripts/campaign_registry.py enqueue --campaign-id N --ticker GDXU \
    --strategy TrailingBothZScoreBreakout --fixed-sl-values 1,2,3,4,5,6,7,8
# -> appends a queued row. Can be run WHILE a queue worker is already draining
#    the table -- this is the actual "add work without killing the run" ask.

python scripts/campaign_registry.py claim-next [--campaign-id N]
# -> atomically (single UPDATE ... WHERE status='queued' ... LIMIT 1, SQLite's
#    own row-level locking under WAL mode) picks the oldest queued row, marks
#    it running with this process's pid, prints its params. Returns nothing /
#    exit 1 if the queue is empty (caller decides: idle-poll and wait, or exit).
```

`run_inmemory_sweep_queue.sh`'s per-ticker/strategy `for` loop is replaced by a
`while claim-next; do <run bench_phase1_phase2_inmemory.py with claimed params>; done`
loop. This is the actual mechanism behind "append a job without disrupting what's
running" — a second `enqueue` call from an entirely separate terminal/session adds a row
the *already-running* worker loop will pick up on its next `claim-next`, no restart
needed. Multiple worker loops (e.g. one per available CPU budget tier) can run
concurrently against the same queue since `claim-next` is a single atomic UPDATE.

## 4. Queryable status

```
python scripts/campaign_registry.py status --campaign-id N
```
prints (from a straight `campaigns` + `campaign_jobs` + `sweep_run_log` join — no new
data source, just a view over what already gets written): campaign metadata, version
string, count of jobs queued/running/done/failed, and for each `running` job: ticker,
strategy, pid, elapsed time, and (by joining `sweep_run_log` on
`ticker,strategy,version`) which `fixed_sl` value is currently in flight if a row with
`finished_at IS NULL` exists. This directly replaces "tail the log file to see what's
happening" with a single query — matches the existing `scripts/daemon_status.py`
convention (a status script instead of manual `ps`/log inspection) rather than
introducing a new pattern.

A `--watch` flag (poll on an interval, redraw) is a plausible nice-to-have, not required
for the core design.

## 5. CPU / mid-sweep worker control

This is the least-resolved piece — flagging it explicitly rather than hand-waving.

**What's structurally NOT possible**: a `ProcessPoolExecutor` created with
`max_workers=N` cannot be resized while jobs are in flight — that's a Python stdlib
limitation, not something this design can route around. `bench_phase1_phase2_inmemory.py`
creates ONE shared pool per `bench_phase1_phase2_inmemory.py` invocation (i.e. per queue
job, `--fixed-sl-values` all sharing it — see that file's own header comment on this
2026-08-27 fix), so "CPU control" in this design means **inter-job**, not
**intra-job**: how many `campaign_jobs` run process-concurrently, not shrinking a
pool that's already mid-computation for one job.

**Proposed mechanism**: a `workers_budget` column on `campaigns` (mutable, defaults to
today's hardcoded `WORKERS=8`), read by the worker-loop wrapper (not the Python script
itself) each time it's about to `claim-next` + launch a job — i.e. **checked between
jobs**, not enforced mid-job. Lowering it takes effect for the *next* job that starts;
a job already running keeps its `--workers N` it was launched with. This is a real,
useful form of control ("stop starting big new jobs, let what's running finish") but it
is NOT the same as "throttle this specific running process's CPU right now" — that would
need OS-level `nice`/`cpulimit`/cgroups on the live PID, a different and separably-scoped
mechanism if the user actually wants it (worth asking explicitly rather than assuming,
per `feedback_ask_before_building`).

## Interaction with existing per-invocation `ProcessPoolExecutor` pattern

No change to the pool-per-invocation pattern itself. The registry/queue layer sits
strictly *above* it: `claim-next` returns job params, the worker loop launches
`bench_phase1_phase2_inmemory.py` exactly as it does today (subprocess or in-process
call), and that script's own `ProcessPoolExecutor(max_workers=args.workers)` at line
~1073 is unchanged. Phase4 (`candidate_summary_report.py`, confirmed zero parallelism
today per the backlog item) and Phase5 (`phase5_second_level_overlay_check.py`, its own
separate `ProcessPoolExecutor` at line 836) are unaffected the same way — they'd be
launched as separate queue-adjacent steps per job (mirroring today's per-ticker
Phase1→2.5→4→5 sequencing in the shell script), not folded into the same pool.

## Open questions for user review (not decided here)

1. Does the legacy `run_sweep_queue.sh`/`run_optimization_sweep.py` path adopt the same
   registry eventually, or does it stay on `campaign_config.patch_config` indefinitely?
2. Is OS-level CPU throttling (not just "stop launching new jobs") actually wanted, or
   is inter-job budget control (#5 above) sufficient?
3. Should `campaigns.label` enforce the `docs/plans/ground_truth_kernel_rebuild.md`
   name-reservation as a hard uniqueness constraint (reject `create` if the label's
   already claimed for a different param tuple), or stay advisory?
4. Migration: existing `sweep_run_log`/`candidate_nodes` rows have no `campaign_id` —
   is backfilling them in scope, or does the registry only apply going forward?
