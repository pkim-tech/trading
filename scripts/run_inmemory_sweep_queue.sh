#!/bin/bash
# Resumable full-pipeline (Phase1->2->2.5->4->5) queue runner for the in-memory
# GT sweep pipeline (scripts/bench_phase1_phase2_inmemory.py), NOT the legacy
# run_optimization_sweep.py path run_sweep_queue.sh already covers -- see that
# file for the old-pipeline equivalent, do not merge the two.
#
# Per ticker, in order: run bench_phase1_phase2_inmemory.py once per strategy
# (--fixed-sl-values loops fixed_sl 1-8 within that one process/pool, see that
# script's own --fixed-sl-values help), then Phase4 (candidate_summary_report.py
# --kernel gt) against the SAME campaign just produced, before moving to the
# next ticker. (Phase5 used to run here too -- disabled 2026-09-08, see the
# commented-out block at its old call site below for why.) A ticker that
# errors at any phase is logged (PROGRESS-style marker) and skipped -- the rest
# of the queue keeps going, matching the user's explicit call that partial
# completion across the 6-ticker queue is an acceptable outcome for a long
# unattended run.
#
# config.json is NOT backed up/restored here (unlike run_sweep_queue.sh's own
# trap-based backup/restore) -- confirmed by reading bench_phase1_phase2_
# inmemory.py: it never reads/writes config.json (grid params come from
# campaign_config.STRATEGIES + this script's own CLI overrides), so that
# pattern doesn't apply to this pipeline.
#
# Version resolution + real job queue (2026-08-31, Task #8, implements
# docs/plans/campaign_registry_design.md): this script used to hand-build its
# own $VERSION bash string, independently of bench_phase1_phase2_inmemory.py's
# own construction -- with zero shared source of truth. That drifted for real
# the same night (a mid-queue PROMOTION_ALGO_VERSION 3->4 commit landed while a
# queue process was already running: GDXU's TrailingBoth candidates landed
# tagged -pv3, TrailingExit landed -pv4, and this script's stale -pv3 Phase4/
# Phase5 --version calls silently resolved zero scopes for the new rows).
#
# Round 1 of this fix (same night) only deduplicated the version-STRING
# CONSTRUCTION into scripts/campaign_registry.build_version_string -- it still
# hardcoded its OWN bash copies of PROMOTION_ALGO_VERSION/CAMPAIGN_LABEL/
# DATA_SOURCE/window dates and resolved $VERSION once at script start, which
# does NOT close the incident: the actual drift was in the INPUTS, and a
# once-per-script-start bash literal is exactly as stale as the original bug
# for a commit landing mid-run (paired-review CONFIRMED HIGH, both independent
# reviewers).
#
# Round 2 tried re-resolving `resolve_campaign()` fresh before EACH ticker's
# Phase4/5 step, reassigning the loop's own $CAMPAIGN_ID/$VERSION each time --
# BOTH independent reviewers proved by simulation this made the incident
# WORSE, not better: a mid-campaign commit made `resolve_campaign()` `create`
# a genuinely NEW campaign row and overwrite $CAMPAIGN_ID, so the drain
# loop's next `claim-next` silently found an empty queue for the NEW
# (unpopulated) campaign, `break`-ed, and printed "All done" while every
# remaining ticker's real jobs sat stranded `queued` forever under the OLD
# campaign_id -- plus Phase4/5 for the CURRENT ticker ran against a version
# matching NONE of that ticker's own just-written rows (the original
# incident, inverted: ahead of the rows instead of behind them). Reverted.
#
# Round 3 (this version, final): `resolve_campaign()` below still reads
# bench_phase1_phase2_inmemory.py's OWN on-disk constants FRESH via a Python
# import -- no hardcoded shell copies at all -- but is called ONLY ONCE, at
# script start, exactly like round 1's simpler shape. $CAMPAIGN_ID/$VERSION
# are the drain loop's stable operating identity for its entire life; nothing
# reassigns them mid-run. What THIS actually fixes, honestly: the "two
# independently hand-maintained copies of the same constants can silently
# diverge from an ordinary editing mistake" class (there is now exactly ONE
# place -- bench_phase1_phase2_inmemory.py's own module constants -- these
# values can be edited at all). What this does NOT fix, same as it never
# did: a commit landing WHILE this specific queue is actively draining still
# produces the same drift the original incident hit -- a live bash process
# holding $VERSION from start cannot track a live-changing on-disk Python
# constant without re-reading it, and re-reading it necessarily invalidates
# consistency with whatever's already been dispatched under the OLD value.
# True elimination needs pinning a whole campaign to one git commit (e.g. a
# worktree per campaign) -- a real, buildable follow-up, explicitly NOT built
# here (see docs/plans/campaign_registry_design.md's own "What this does NOT
# fix" section, which named this exact residual gap before it was ever hit).
#
# Job dispatch is now a real, persisted queue (campaign_jobs table in
# trading_universe.db) instead of a bare bash `for ticker in $TICKERS` loop:
# every (ticker, strategy) job is enqueued up front, then drained via
# `claim-next` (SQLite-serialized, safe against a second concurrent claimer).
# This is what actually answers the standing "sweep manager" backlog item
# (docs/backlog_cache.md, raised 2026-08-29/reaffirmed 2026-08-31): while this
# loop is draining, run
#   .venv/bin/python scripts/campaign_registry.py enqueue --campaign-id N \
#       --ticker TICKER --strategy STRATEGY --fixed-sl-values 1,2,3,4,5,6,7,8
# from a SEPARATE terminal to append work -- it lands in the same FIFO queue
# and gets picked up without restarting anything. `python scripts/
# campaign_registry.py status --campaign-id N` gives a real-time queryable
# summary instead of tailing this script's log.
#
# KNOWN LIMITATION, accepted for v1 (design doc's own open question #2 --
# CPU control -- and a related concurrency gap found while building this):
# this script's own Phase4/5-per-ticker-completion tracking (the REMAINING
# bash associative array below) is per-PROCESS, not cross-process-atomic. The
# only tested/supported usage today is ONE drain loop (this script) per
# campaign. If a SECOND concurrent instance of this script were ever run
# against the SAME campaign_id and a ticker's two strategy jobs happened to
# split across the two processes, EACH process's own REMAINING counter would
# only ever see its own completions and never reach zero -- Phase4/5 would
# then SILENTLY NEVER RUN for that ticker (not double-run; corrected
# 2026-08-31, paired review -- the original comment here had this backwards).
# True cross-process ticker-completion tracking (e.g. via campaign_jobs' own
# claim-next primitive on a synthetic per-ticker "phase45" job) is a real,
# buildable follow-up, deferred here since nothing today actually runs two
# concurrent drain loops against one campaign_id.
#
# ALSO KNOWN (contextual paired review, CONFIRMED MEDIUM, not fixed here per
# this project's policy of acting only on CONFIRMED HIGH/CRITICAL findings):
# appending a genuinely NEW ticker via `enqueue` while this loop drains (the
# advertised use case) can trigger Phase4/5 prematurely -- REMAINING has no
# entry for a ticker that wasn't in the original $TICKERS list, so it defaults
# to 1 and hits zero after the FIRST of that ticker's appended strategy jobs
# finishes, not after all of them. Self-healing (Phase5's per-ticker CSV and
# the combined-file rebuild both just re-run against fresher data on the
# SECOND strategy's completion), so the cost is a wasted/premature
# intermediate report, not corruption -- but worth knowing before relying on
# "append a whole new ticker" specifically.
#
# Usage:
#   ./scripts/run_inmemory_sweep_queue.sh
# Overrides via env: TICKERS, STRATEGIES, FIXED_SL_VALUES, Z_THRESHOLDS,
#   WINDOWS, N_ISLANDS, WORKERS, WINDOW_START, WINDOW_END.
#
# Date-window override (2026-08-31, Task #10): WINDOW_START/WINDOW_END, both-
# or-neither (validated below, before resolve_campaign runs, matching window_
# version_suffix's own both-or-neither contract in run_optimization_sweep.py).
# Unset (the default) leaves bench_phase1_phase2_inmemory.py's own module-level
# START/END untouched, exactly today's behavior. When set, threaded through
# BOTH resolve_campaign() (so the registered campaign row's window_start/
# window_end -- and therefore its version string's -w<start>_<end> suffix,
# via campaign_registry.build_version_string -> window_version_suffix -- match
# the real window) AND every per-job bench invocation (--start-date/--end-date,
# so bench's OWN version-string computation, done independently inside its own
# process, resolves to the identical string). These are two genuinely separate
# processes computing the same version string from what must be the same
# inputs -- exactly the class of drift this project's PROMOTION_ALGO_VERSION
# incident hit (see this file's header above) and Task #8's version-string
# unification was built to close. No NEW naming scheme was needed for this:
# window_version_suffix already makes each date window collision-safe via its
# own -w<start>_<end> suffix (confirmed present today for every existing
# campaign, since bench.START/END are always concrete, never None) -- a
# 5-window KORU campaign gets 5 fully distinct version strings for free just
# by setting WINDOW_START/WINDOW_END differently per script invocation, no
# separate per-window numbering suffix required. (Investigated a possible
# "v6.x-pN" per-window numbering scheme per the dispatching session's question
# -- found no such scheme in the design doc or committed code; the only "-p"
# suffix that exists is `-pv<promotion_algo_version>`, PROMOTION_ALGO_VERSION,
# which is unrelated to windowing. Also checked the deferred params_json work
# (docs/backlog_cache.md, paused 2026-08-29) for a connection -- found none;
# params_json is about candidate-node identity representation, orthogonal to
# date-window scoping, and explicitly not wired into any production call path
# today.)
if [ -n "${WINDOW_START:-}" ] || [ -n "${WINDOW_END:-}" ]; then
  if [ -z "${WINDOW_START:-}" ] || [ -z "${WINDOW_END:-}" ]; then
    echo "FATAL: WINDOW_START and WINDOW_END must both be set, or neither" \
         "(got WINDOW_START='${WINDOW_START:-}' WINDOW_END='${WINDOW_END:-}')" \
         "-- a partial override would silently leave bench's own version-string" \
         "computation using its module default for the missing side, splitting" \
         "this campaign's rows across two different -w<start>_<end> version" \
         "strings with no error raised anywhere." >&2
    exit 1
  fi
fi
#
# Deliberately NOT launched by this task -- building/committing this script is
# the full scope; a peer session launches it once the paired review clears AND
# the currently-running queue (started under the pre-Task-#8 script) finishes.

cd "$(dirname "$0")/.."

# Unbuffered stdout (2026-08-29, real bug found): without this, Python buffers
# stdout when writing to a pipe/file (not a TTY) -- print() output including
# tqdm's progress bar and all PROGRESS: markers can sit invisibly in an
# internal buffer for a long time even while the process is genuinely
# computing. Confirmed via ps/CPU that a prior run WAS working -- this was a
# visibility bug (nothing appeared to tail -f), not a hang.
export PYTHONUNBUFFERED=1

PYTHON=".venv/bin/python"
TICKERS="${TICKERS:-AGQ ETHU OILU GDXU UGL WEBL}"
STRATEGIES="${STRATEGIES:-TrailingBothZScoreBreakout TrailingExitZScoreBreakout}"
FIXED_SL_VALUES="${FIXED_SL_VALUES:-1 2 3 4 5 6 7 8}"
Z_THRESHOLDS="${Z_THRESHOLDS:-0.5 1.0 1.5 2.0}"
WINDOWS="${WINDOWS:-5 10 15 20}"
# Reverted 10 -> 3 (2026-08-30, planner dispatch, reversing the widen-to-10 decision
# made earlier the same night): real-data checks against actual campaign output (GDXU/
# ETHU) found the true best candidate on the metric that matters (core_both_cagr, i.e.
# overlay CAGR) was ALREADY found at island rank #1-#3 -- evidence did not support the
# ~2-3x Phase2 cost of N=10 (confirmed ~linear: 3.33x islands -> ~3.3x Phase2 cost,
# measured directly). The one real gap N=10 was covering (a distinct arm_pct region that
# never becomes its own TP/SL island) is now handled directly by the arm_pct backfill
# below instead -- see find_missing_arm_top_n in bench_phase1_phase2_inmemory.py -- which
# is targeted at the actual evidenced gap rather than paying for a blanket 3x-wider
# island search.
N_ISLANDS="${N_ISLANDS:-3}"
WORKERS="${WORKERS:-8}"
# ENTRY_TIMING/CAMPAIGN_LABEL (2026-09-02): pass-through to bench's own --entry-timing/
# --campaign-label CLI overrides (added same night for the v6.6 close-entry campaign).
# Empty by default -- an unset ENTRY_TIMING omits --entry-timing entirely, matching
# bench's own module default 'open_check'; an unset CAMPAIGN_LABEL omits --campaign-
# label, matching bench's own module default 'v6.5'. Must match whatever config the
# target campaign was actually registered under (see ATTACH_CAMPAIGN_ID below) -- a
# mismatch here would make this script's per-job bench invocations compute a DIFFERENT
# version string than the one Phase4/5 below queries, the exact split-brain class
# ATTACH_CAMPAIGN_ID's own docstring warns about.
ENTRY_TIMING="${ENTRY_TIMING:-}"
CAMPAIGN_LABEL="${CAMPAIGN_LABEL:-}"
# USE_CHECKPOINT/CHECKPOINT_FILE (2026-09-03): pass-through to bench's own --use-checkpoint/
# --checkpoint-file opt-in flags (added same day, commit 249f178, closing a real live
# incident -- checkpoint load defaulted on whenever the auto-computed default path
# happened to have a stale file from an earlier, differently-configured run). Empty/unset
# by default -- this drain loop's crash-restart path (a job killed mid-fixed_sl, then this
# script re-run) previously recovered Phase1+Phase2's cost for free off that default path;
# since bench's own opt-in fix, that recovery is gone unless explicitly requested here.
# Set USE_CHECKPOINT=1 to opt back into the SAME default-path convenience bench's own
# --use-checkpoint provides (auto-computed filename, no path management needed) -- the
# common case for "just let a restart resume where it left off." CHECKPOINT_FILE is the
# rarer explicit-path form (mirrors bench's own --checkpoint-file), for a caller managing
# its own checkpoint location outside the default convention. Harmless to set both (bench
# ORs them, `args.checkpoint_file or args.use_checkpoint`) -- neither conflicts with the
# other, only with --resume-from-top100/--seed-watch-list-id (bench's own real mutual-
# exclusion validators, unrelated to this script's own job-queue axes).
USE_CHECKPOINT="${USE_CHECKPOINT:-}"
CHECKPOINT_FILE="${CHECKPOINT_FILE:-}"
# ATTACH_CAMPAIGN_ID (2026-09-02, real gap found live): resolve_campaign() below always
# self-CREATEs a campaign from bench's own on-disk module constants -- there was no way
# to point this script at an ALREADY-EXISTING campaign_id (e.g. one created ad hoc via
# `campaign_registry.py create` for a non-default config like v6.6's close-entry sweep)
# without it either creating a stray duplicate campaign or silently resolving back to
# a DIFFERENT, wrong one. When set, resolve_campaign() skips the create/module-const-
# reading path entirely and just looks up the existing campaign's real version_string
# via `campaign_registry.py status`. The CALLER is responsible for ensuring TICKERS/
# STRATEGIES/FIXED_SL_VALUES/Z_THRESHOLDS/WINDOWS/N_ISLANDS/ENTRY_TIMING/CAMPAIGN_LABEL
# above match what that campaign was actually registered with (this script has no way
# to verify that automatically) -- get this wrong and Phase4/5's --version query
# resolves zero scopes for whatever this drain loop just computed under a silently
# different version string.
ATTACH_CAMPAIGN_ID="${ATTACH_CAMPAIGN_ID:-}"

Z_THRESHOLDS_CSV=$(echo "$Z_THRESHOLDS" | tr ' ' ',')

# resolve_campaign() -- sets $CAMPAIGN_ID/$VERSION fresh from bench_phase1_phase2_
# inmemory.py's OWN on-disk constants (2026-08-31, paired-review CONFIRMED HIGH
# fixup -- round 2, see this file's header comment for what round 1 got wrong and
# why this is still not a FULL fix). Deliberately no hardcoded PROMOTION_ALGO_
# VERSION/CAMPAIGN_LABEL/DATA_SOURCE/window-date literals in this script at all --
# every one of those is read fresh, every call, from the actual Python source.
# Called once up front (for the enqueue banner) and again before EACH ticker's
# Phase4/5 step below, so a mid-campaign code change is picked up between tickers
# rather than only at script start.
resolve_campaign() {
  if [ -n "$ATTACH_CAMPAIGN_ID" ]; then
    local status_out
    if ! status_out=$($PYTHON scripts/campaign_registry.py status --campaign-id "$ATTACH_CAMPAIGN_ID"); then
      echo "FATAL: campaign_registry.py status --campaign-id $ATTACH_CAMPAIGN_ID failed -- aborting:"
      echo "$status_out"
      exit 1
    fi
    VERSION=$(echo "$status_out" | sed -n 's/^campaign id=.* version=//p')
    if [ -z "$VERSION" ]; then
      echo "FATAL: no campaign found for ATTACH_CAMPAIGN_ID=$ATTACH_CAMPAIGN_ID -- aborting:"
      echo "$status_out"
      exit 1
    fi
    CAMPAIGN_ID="$ATTACH_CAMPAIGN_ID"
    echo "Attached to existing campaign_id=$CAMPAIGN_ID, version=$VERSION (not creating a new campaign row)"
    return
  fi
  local bench_consts
  if ! bench_consts=$($PYTHON -c "
import sys; sys.path.insert(0, '.')
from scripts import bench_phase1_phase2_inmemory as b
print(b.CAMPAIGN_LABEL)
print(b.PROMOTION_ALGO_VERSION)
print(b.DATA_SOURCE)
print(b.START)
print(b.END)
"); then
    echo "FATAL: could not read bench_phase1_phase2_inmemory.py's own module constants -- aborting:"
    echo "$bench_consts"
    exit 1
  fi
  local label pv data_source wstart wend
  label=$(echo "$bench_consts" | sed -n '1p')
  pv=$(echo "$bench_consts" | sed -n '2p')
  data_source=$(echo "$bench_consts" | sed -n '3p')
  wstart=$(echo "$bench_consts" | sed -n '4p')
  wend=$(echo "$bench_consts" | sed -n '5p')
  # WINDOW_START/WINDOW_END override (Task #10, both-or-neither already
  # validated at script start) -- takes precedence over bench's module
  # default so the registered campaign row's window matches what every
  # per-job bench invocation below is actually told to run via --start-date/
  # --end-date.
  if [ -n "${WINDOW_START:-}" ]; then
    wstart="$WINDOW_START"
    wend="$WINDOW_END"
  fi
  # CAMPAIGN_LABEL/ENTRY_TIMING override (2026-09-03, real incident: launching
  # with CAMPAIGN_LABEL=v6.5.1 silently registered/enqueued against the
  # EXISTING v6.5 campaign, because `label` above was read from bench's own
  # HARDCODED module constant via the fresh python -c subprocess two lines up
  # -- that subprocess never sees this shell's $CAMPAIGN_LABEL env var at all.
  # The per-job bench.py invocations below DO correctly receive --campaign-
  # label/--entry-timing (see CAMPAIGN_LABEL_ARGS/ENTRY_TIMING_ARGS further
  # down), so bench itself would compute a genuinely different version string
  # per job -- but this function's own $CAMPAIGN_ID/$VERSION (used for
  # campaign_jobs bookkeeping AND Phase4/5's --version query below) stayed
  # pinned to whatever campaign bench's stale module default resolved to --
  # the exact split-brain class this file's own header comments already
  # document for other axes, just never closed on THIS one because nobody
  # had exercised CAMPAIGN_LABEL through the auto-create path before (v6.6
  # was created via a separate manual campaign_registry.py create + attach,
  # not this env-var path). Same override pattern as WINDOW_START/WINDOW_END
  # immediately above -- shell env takes precedence over bench's module
  # default, applied BEFORE the `create` call, not after.
  if [ -n "${CAMPAIGN_LABEL:-}" ]; then
    label="$CAMPAIGN_LABEL"
  fi
  local entry_timing_create_args=()
  if [ -n "${ENTRY_TIMING:-}" ]; then
    entry_timing_create_args=(--entry-timing "$ENTRY_TIMING")
  fi

  local create_out
  if ! create_out=$($PYTHON scripts/campaign_registry.py create \
      --label "$label" --promotion-algo-version "$pv" \
      --data-source "$data_source" --window-start "$wstart" --window-end "$wend" \
      --z-thresholds "$Z_THRESHOLDS_CSV" --n-islands "$N_ISLANDS" \
      "${entry_timing_create_args[@]}" \
      --workers-budget "$WORKERS" --created-by run_inmemory_sweep_queue.sh); then
    echo "FATAL: campaign_registry.py create failed -- aborting before any real work:"
    echo "$create_out"
    exit 1
  fi
  CAMPAIGN_ID=$(echo "$create_out" | sed -n 's/^campaign_id=\([0-9]*\) .*/\1/p')
  VERSION=$(echo "$create_out" | sed -n 's/.*version=//p')
  if [ -z "$CAMPAIGN_ID" ] || [ -z "$VERSION" ]; then
    echo "FATAL: could not parse campaign_id/version from campaign_registry.py create output:"
    echo "$create_out"
    exit 1
  fi
}

resolve_campaign

# Ticker-transition banner (2026-08-30, user feedback: this is the bigger unit of
# progress -- one per ticker vs. one per fixed_sl/window scope inside it -- so it
# must read as visually heavier than the inner per-scope '-'*80 banners
# (candidate_summary_report.py/phase5_second_level_overlay_check.py), not lighter
# as the old single-line "=== ... ===" was.
ticker_banner() {
  echo ""
  echo "================================================================================"
  echo "=== $1 — $(date)"
  echo "================================================================================"
}

mkdir -p logs
LOG="logs/inmemory_sweep_queue_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to $LOG (console + file via tee)"

# `exec > >(tee "$LOG") 2>&1`, NOT `{ ... } | tee "$LOG"` (2026-08-31, round-4 same-day
# fixup, paired-review CONFIRMED HIGH-equivalent x2 -- the cold reviewer reproduced both
# empirically): a `{ ... } | tee` pipeline runs the WHOLE drain loop -- including the
# INT/TERM/HUP trap -- inside `tee`'s pipeline SUBSHELL, a genuinely different process
# from the pid an operator/supervisor/`timeout`/`nohup ... &`-then-`kill` sees. A plain
# `kill -TERM <that pid>` (the natural way to stop an unattended overnight campaign, NOT
# just an interactive Ctrl-C) never reaches the subshell at all -- the trap never fires,
# reopening the exact orphaned-pool-plus-stuck-`running`-job bug this whole signal-
# handling block exists to close. Separately, that pipeline shape also swallows the
# trap's own `exit 130`: a pipeline's overall exit status is its LAST command's (`tee`'s,
# always 0 here, no `pipefail` was set), not the subshell's -- so an interrupted campaign
# used to report clean success (`$?`=0) to any wrapper/cron watching this script's own
# exit code. `exec > >(tee ...) 2>&1` makes the redirection apply to THIS shell process
# directly (no subshell at all) -- the trap is reachable by a plain `kill` on the real
# script pid, and `exit 130` is genuinely this script's own exit status.
exec > >(tee "$LOG") 2>&1
echo "======================================================"
echo " In-memory sweep queue start — $(date)"
  echo " Tickers: $TICKERS"
  echo " Strategies: $STRATEGIES"
  echo " Fixed SLs: $FIXED_SL_VALUES"
  echo " Z-thresholds: $Z_THRESHOLDS"
  echo " Windows: $WINDOWS"
  echo " N-islands: $N_ISLANDS"
  echo " campaign_id=$CAMPAIGN_ID  version=$VERSION"
  # Effective workers_budget (2026-08-31, paired-review CONFIRMED MEDIUM fixup): surfaced
  # explicitly here because register_campaign() is idempotent-on-version_string and never
  # updates an EXISTING row's workers_budget -- relaunching a previously-throttled campaign
  # (same version_string) silently keeps whatever value a prior `set-workers-budget` last
  # left it at, even though this script always passes --workers-budget "$WORKERS" to
  # `create`. Printing it here means a stale throttle is visible at a glance instead of
  # only discoverable via a separate `status` call at the end of a run.
  EFFECTIVE_BUDGET=$($PYTHON scripts/campaign_registry.py status --campaign-id "$CAMPAIGN_ID" \
      | sed -n 's/.*workers_budget=\([^ ]*\).*/\1/p')
  echo " Effective workers_budget=$EFFECTIVE_BUDGET (requested --workers-budget=$WORKERS -- differs if this campaign was relaunched after a live set-workers-budget change)"
  echo " Append work while this runs:"
  echo "   $PYTHON scripts/campaign_registry.py enqueue --campaign-id $CAMPAIGN_ID \\"
  echo "       --ticker TICKER --strategy STRATEGY --fixed-sl-values 1,2,3,4,5,6,7,8"
  echo " Check status from another terminal:"
  echo "   $PYTHON scripts/campaign_registry.py status --campaign-id $CAMPAIGN_ID"
  echo " Raise/lower in-flight worker cap live (no restart -- takes effect at each bench"
  echo " process's next dispatch call, typically seconds to a few minutes, NOT a fixed poll):"
  echo "   $PYTHON scripts/campaign_registry.py set-workers-budget --campaign-id $CAMPAIGN_ID --workers-budget N"
  echo " Pause/resume (takes effect at the next job/phase boundary -- real memory reclaimed,"
  echo " unlike workers_budget -- see campaign_registry.py's own docstring):"
  echo "   $PYTHON scripts/campaign_registry.py pause --campaign-id $CAMPAIGN_ID"
  echo "   $PYTHON scripts/campaign_registry.py resume --campaign-id $CAMPAIGN_ID"
  echo "======================================================"

  FIXED_SL_CSV=$(echo "$FIXED_SL_VALUES" | tr ' ' ',')
  NUM_STRATEGIES=$(echo "$STRATEGIES" | wc -w)
  declare -A REMAINING
  declare -A FAILED_TICKER
  N_ENQUEUED=0
  for ticker in $TICKERS; do
    # REMAINING[$ticker] tracking (used below to trigger Phase4/5 once both this
    # ticker's strategy jobs are terminal) must ALWAYS populate from this script's
    # own TICKERS/STRATEGIES, independent of ATTACH_CAMPAIGN_ID -- the caller in
    # attach mode is responsible for making TICKERS/STRATEGIES describe exactly
    # what it already enqueued via `campaign_registry.py enqueue` itself, so this
    # count still matches reality even though the actual enqueue call below is
    # skipped.
    REMAINING[$ticker]=$NUM_STRATEGIES
    for strategy in $STRATEGIES; do
      # ATTACH_CAMPAIGN_ID mode (2026-09-02): the target campaign's queue was
      # already populated by the caller before this script ran (e.g. `campaign_
      # registry.py enqueue` calls made directly) -- re-enqueueing here would
      # silently duplicate every job (real incident, found live: a first attach-
      # mode run enqueued its own default 6-ticker TICKERS list on top of an
      # already-enqueued 12-ticker real queue, including 5 tickers appearing
      # twice and one -- AGQ -- that had ALREADY fully completed, which would
      # have silently redone real finished work). Skip enqueue, not the
      # REMAINING/N_ENQUEUED bookkeeping.
      if [ -z "$ATTACH_CAMPAIGN_ID" ]; then
        $PYTHON scripts/campaign_registry.py enqueue --campaign-id "$CAMPAIGN_ID" \
            --ticker "$ticker" --strategy "$strategy" --fixed-sl-values "$FIXED_SL_CSV" > /dev/null
      fi
      N_ENQUEUED=$((N_ENQUEUED + 1))
    done
  done
  if [ -n "$ATTACH_CAMPAIGN_ID" ]; then
    echo "Attach mode: skipped enqueueing $N_ENQUEUED job(s) for campaign_id=$CAMPAIGN_ID (caller's own responsibility)."
  else
    echo "Enqueued $N_ENQUEUED job(s) for campaign_id=$CAMPAIGN_ID."
  fi

  # Real inter-phase/inter-job pause (2026-08-31, real user ask, folded in while this
  # commit was already on hold -- see campaign_registry.py's own module docstring for
  # why this is deliberately SEPARATE from workers_budget, not a reused signal). Only
  # ever called at a point where the PRIOR phase's subprocess has already fully exited
  # (before claiming the next job, before launching Phase4, before launching Phase5) --
  # never mid-phase, since only then has that phase's own pool already torn down and
  # its memory already been reclaimed.
  wait_while_paused() {
    local announced=0
    while $PYTHON scripts/campaign_registry.py is-paused --campaign-id "$CAMPAIGN_ID"; do
      if [ "$announced" = "0" ]; then
        echo "PAUSED: campaign_id=$CAMPAIGN_ID — $(date). Waiting for "
        echo "  $PYTHON scripts/campaign_registry.py resume --campaign-id $CAMPAIGN_ID"
        echo "Checking every 30s."
        announced=1
      fi
      sleep 30
    done
  }

  while true; do
    wait_while_paused
    CLAIMED=$($PYTHON scripts/campaign_registry.py claim-next --campaign-id "$CAMPAIGN_ID")
    claim_rc=$?
    if [ $claim_rc -eq 2 ]; then
      break  # genuinely empty queue
    elif [ $claim_rc -eq 3 ]; then
      # Paused between the wait_while_paused check above and this claim-next call
      # (a real, if narrow, race -- e.g. `pause` ran in that window) -- loop back to
      # wait_while_paused rather than treating this as any kind of error or done.
      continue
    elif [ $claim_rc -ne 0 ]; then
      # 2026-08-31, paired-review CONFIRMED HIGH fixup: exit 2 (empty) and any
      # other nonzero (a real error -- e.g. a DB-lock timeout on this campaign's
      # own BEGIN IMMEDIATE) used to be indistinguishable to a bare `|| break`,
      # which would silently truncate an unattended multi-hour campaign and
      # still print "All done" afterward. Abort loudly instead of guessing.
      echo "FATAL: claim-next failed (exit $claim_rc), NOT an empty queue -- aborting rather than silently treating this as complete. Check DB connectivity."
      exit 1
    fi
    read -r JOB_ID JOB_TICKER JOB_STRATEGY JOB_FIXED_SL <<< "$CLAIMED"

    if [ "${FAILED_TICKER[$JOB_TICKER]:-0}" = "1" ]; then
      # This ticker already failed earlier in this drain (either an already-
      # queued sibling job, or one appended after the failure) -- honor the
      # same "abandon the rest of a failed ticker" behavior as before.
      $PYTHON scripts/campaign_registry.py mark-finished --job-id "$JOB_ID" --rc 1 > /dev/null
      REMAINING[$JOB_TICKER]=$(( ${REMAINING[$JOB_TICKER]:-1} - 1 ))
      continue
    fi

    ticker_banner "$JOB_TICKER | $JOB_STRATEGY: Phase1-2.5 start (job_id=$JOB_ID)"
    # JOB_FIXED_SL/$WINDOWS/$Z_THRESHOLDS deliberately unquoted below for
    # nargs="+" word-splitting, matching this script's pre-existing convention.
    #
    # Backgrounded + `wait`-ed, not run in the foreground (2026-08-31, Task #9, real
    # bug found live testing against DPST): claim-next's own os.getpid() used to record
    # ITS OWN (already-exited by the time the real work starts) pid on the job row --
    # claim-next is a short-lived CLI subprocess, never the process that does the real
    # work. Backgrounding here and capturing the REAL bench_phase1_phase2_inmemory.py
    # pid via `$!` lets update-job-pid record the actual running process, so `status`
    # shows something real instead of a stale/wrong pid for the job's entire duration.
    # `wait` still blocks this loop iteration exactly as the foreground call did, and
    # still yields the real exit code via $? -- no change to the queue's own serial,
    # one-job-at-a-time semantics.
    #
    # SIGINT/SIGTERM handling (2026-08-31, two same-day fixup rounds, paired-review
    # CONFIRMED MEDIUM x3, all independently reproduced empirically by both reviewers):
    # an asynchronously-launched (`&`) child in a non-interactive shell -- which this
    # script always is, whether the USER's own launch was interactive or not -- inherits
    # SIG_IGN for SIGINT/SIGQUIT. Before backgrounding, Ctrl-C on this script correctly
    # killed the foreground bench_phase1_phase2_inmemory.py child (and, since job
    # control was off, everything in the script's own process group, including the
    # child's own forked ProcessPoolExecutor workers) too.
    #
    # Round 1 (INT/TERM trap forwarding SIGTERM to $BENCH_PID alone) fixed the
    # kill-the-child-at-all problem but introduced two NEW regressions, both
    # reproduced: (a) a bash trap handler RETURNS by default -- forwarding the signal
    # and letting the loop continue silently turned "Ctrl-C stops the whole campaign"
    # into "skip only this one job, keep draining the queue," which also writes a
    # real-looking-but-fake `failed` rc indistinguishable from a genuine crash; (b)
    # `kill -TERM $BENCH_PID` signals ONLY the bench parent process, not its forked
    # pool -- Python's default SIGTERM handling kills the parent immediately with no
    # atexit/pool-join, so every ProcessPoolExecutor worker survives as an orphan.
    #
    # Round 2 (this version) fixes both: `set -m` (job control) below gives each
    # backgrounded job its OWN process group, so `kill -TERM -- -"$BENCH_PID"`
    # (negative pid = whole group) reaches every forked pool worker too, restoring
    # the original all-or-nothing kill behavior. The trap now does its own complete
    # cleanup (kill the group, block via a kill-0-guarded wait loop until it's
    # ACTUALLY dead -- not a bare `if`, which a second signal arriving mid-wait could
    # interrupt again and reproduce the same orphan, also reproduced) then marks the
    # job with a clearly-recognizable rc=130 (not a value a real bench crash could
    # produce) and `exit`s the WHOLE script -- an operator's Ctrl-C aborts the entire
    # campaign again, not just the current job.
    # WINDOW_START/WINDOW_END pass-through (Task #10) -- an array, not a bare
    # string interpolation, so the "unset" case contributes zero args instead
    # of two empty-string args (which bench's own --start-date/--end-date
    # argparse would treat as a real, wrong override value '' rather than
    # "not passed"). Must match resolve_campaign()'s wstart/wend exactly --
    # both read from the same WINDOW_START/WINDOW_END env vars, validated
    # both-or-neither at script start above.
    WINDOW_ARGS=()
    if [ -n "${WINDOW_START:-}" ]; then
      WINDOW_ARGS=(--start-date "$WINDOW_START" --end-date "$WINDOW_END")
    fi
    # ENTRY_TIMING/CAMPAIGN_LABEL pass-through (2026-09-02, see their own env-var
    # docstrings above) -- same conditional-array pattern as WINDOW_ARGS, omitted
    # entirely when unset so this script's behavior is byte-identical to before
    # for every caller that doesn't set them.
    ENTRY_TIMING_ARGS=()
    if [ -n "$ENTRY_TIMING" ]; then
      ENTRY_TIMING_ARGS=(--entry-timing "$ENTRY_TIMING")
    fi
    CAMPAIGN_LABEL_ARGS=()
    if [ -n "$CAMPAIGN_LABEL" ]; then
      CAMPAIGN_LABEL_ARGS=(--campaign-label "$CAMPAIGN_LABEL")
    fi
    # USE_CHECKPOINT/CHECKPOINT_FILE pass-through (2026-09-03, see their own env-var
    # docstrings above) -- same conditional-array pattern as WINDOW_ARGS/ENTRY_TIMING_ARGS/
    # CAMPAIGN_LABEL_ARGS, omitted entirely when unset so this script's behavior is
    # byte-identical to before (i.e. still never loads a checkpoint by default, matching
    # bench's own opt-in-required default) for every caller that doesn't set them.
    USE_CHECKPOINT_ARGS=()
    if [ -n "$USE_CHECKPOINT" ]; then
      USE_CHECKPOINT_ARGS=(--use-checkpoint)
    fi
    CHECKPOINT_FILE_ARGS=()
    if [ -n "$CHECKPOINT_FILE" ]; then
      CHECKPOINT_FILE_ARGS=(--checkpoint-file "$CHECKPOINT_FILE")
    fi
    set -m
    $PYTHON scripts/bench_phase1_phase2_inmemory.py \
        --ticker "$JOB_TICKER" \
        --strategy "$JOB_STRATEGY" \
        --fixed-sl-values $(echo "$JOB_FIXED_SL" | tr ',' ' ') \
        --z-thresholds $Z_THRESHOLDS \
        --window $WINDOWS \
        --n-islands "$N_ISLANDS" \
        --workers "$WORKERS" \
        "${WINDOW_ARGS[@]}" \
        "${ENTRY_TIMING_ARGS[@]}" \
        "${CAMPAIGN_LABEL_ARGS[@]}" \
        "${USE_CHECKPOINT_ARGS[@]}" \
        "${CHECKPOINT_FILE_ARGS[@]}" &
    BENCH_PID=$!
    # Trap installed only AFTER $BENCH_PID is actually set (not before backgrounding) --
    # closes even the sub-millisecond race of a signal arriving before BENCH_PID holds
    # this iteration's real pid.
    #
    # HUP included (2026-08-31, round-3 same-day fixup, paired-review CONFIRMED MEDIUM):
    # `set -m` above puts $BENCH_PID in its OWN process group (needed for the group-kill
    # below to reach the whole pool, not just the parent) -- but that ALSO means a
    # terminal-generated SIGHUP (closing the terminal, an SSH drop, on the very real
    # multi-hour foreground run this script's own usage doc describes) no longer reaches
    # the child directly the way it did before `set -m`, reopening the exact orphan bug
    # this trap exists to close, through a signal this trap didn't originally catch.
    #
    # mark-finished BEFORE the echo is load-bearing, not incidental (found by the same
    # review round): under a real Ctrl-C, `tee` (same foreground process group) dies on
    # the identical SIGINT, so anything printed to stdout from inside this trap after
    # that point can be lost to a broken pipe / SIGPIPE before `exit 130` is ever
    # reached -- the DB write must come first so the job's real fate is recorded even if
    # the human-readable log message and the clean exit code aren't. `trap "" PIPE` (round
    # 4, same-day fixup, paired-review CONFIRMED LOW, verified) + appending straight to
    # `"$LOG"` instead of stdout closes the SIGPIPE gap itself, so on a real Ctrl-C (not
    # just a directed `kill` on this script's own pid) the message AND `exit 130` both
    # still land, not just the DB write.
    #
    # nohup NOTE (round 4, same-day fixup, paired-review CONFIRMED LOW): `nohup ... &`
    # sets SIGHUP to SIG_IGN, and bash cannot install a trap over an inherited-ignored
    # signal (POSIX) -- so this trap's HUP handling is INERT under nohup specifically
    # (verified via /proc/<pid>/status SigIgn mask). Not a bug (a nohup'd campaign
    # correctly surviving a terminal close/SSH drop is the whole point of nohup), but
    # `kill -HUP` is NOT a reliable way to stop a nohup'd campaign -- use `kill -TERM`.
    trap '
        trap "" PIPE
        kill -TERM -- -"$BENCH_PID" 2>/dev/null
        while kill -0 "$BENCH_PID" 2>/dev/null; do wait "$BENCH_PID" 2>/dev/null; done
        $PYTHON scripts/campaign_registry.py mark-finished --job-id "$JOB_ID" --rc 130 > /dev/null
        echo "PROGRESS: interrupted by signal -- aborting the whole campaign (job_id=$JOB_ID marked rc=130, not a real crash)" >> "$LOG"
        exit 130
    ' INT TERM HUP
    $PYTHON scripts/campaign_registry.py update-job-pid --job-id "$JOB_ID" --pid "$BENCH_PID" > /dev/null
    # No re-wait loop needed here (round-1 fixup had one; removed in round 2): the trap
    # above now does its OWN complete cleanup and `exit`s the whole script whenever it
    # fires, so control only ever reaches this line via the trap NOT having fired --
    # either $BENCH_PID exited normally, or it was killed by something outside this
    # script's own signal handling (e.g. an external SIGKILL/OOM-kill), in which case
    # `wait`'s reported code is already the real, final one.
    wait "$BENCH_PID"
    rc=$?
    trap - INT TERM HUP
    set +m
    $PYTHON scripts/campaign_registry.py mark-finished --job-id "$JOB_ID" --rc "$rc"

    if [ $rc -ne 0 ]; then
      echo "PROGRESS: Phase1-2.5 FAILED ticker=$JOB_TICKER strategy=$JOB_STRATEGY: exit code $rc -- skipping rest of $JOB_TICKER, continuing queue"
      FAILED_TICKER[$JOB_TICKER]=1
      $PYTHON scripts/campaign_registry.py skip-remaining --campaign-id "$CAMPAIGN_ID" --ticker "$JOB_TICKER" > /dev/null
      continue
    fi

    REMAINING[$JOB_TICKER]=$(( ${REMAINING[$JOB_TICKER]:-1} - 1 ))
    if [ "${REMAINING[$JOB_TICKER]}" -gt 0 ]; then
      continue
    fi
    # Every strategy job enqueued for this ticker (at the time it was last
    # decremented) is now terminal -- run Phase4/5 once. A job for this same
    # ticker appended AFTER this point (REMAINING already <= 0) will decrement
    # it further negative and re-trigger this block again on its own
    # completion -- deliberately harmless (Phase4/5 re-run against fresh data
    # for that ticker), not treated as a bug.

    # Deliberately uses the SAME $CAMPAIGN_ID/$VERSION captured once at script start --
    # NOT re-resolved here. A round-2 attempt at this (paired review, 2026-08-31) called
    # resolve_campaign() again right before Phase4/5, which reassigned the loop's own
    # $CAMPAIGN_ID/$VERSION -- both independent reviewers proved by simulation this made
    # things WORSE, not better: a mid-campaign commit made resolve_campaign() `create` a
    # NEW campaign row and overwrite $CAMPAIGN_ID, so the next claim-next silently found
    # an empty queue and the script printed "All done" while every remaining ticker sat
    # stranded `queued` forever -- AND Phase4/5 for the CURRENT ticker got invoked with a
    # version matching NONE of that ticker's own just-written rows (the original incident,
    # inverted). Reverted. See this file's header comment for what's honestly fixed here
    # (no more hardcoded duplicate literals -- one on-disk source, read once) versus what
    # is NOT (a commit landing while this queue is actively draining still isn't handled;
    # true elimination needs commit-pinning a whole campaign to one git commit, e.g. a
    # worktree per campaign -- not built here).
    wait_while_paused  # Phase1-2.5's own pool for this ticker has already exited by here
    ticker_banner "$JOB_TICKER: Phase4 (candidate_summary_report.py --kernel gt) start"
    $PYTHON scripts/candidate_summary_report.py --kernel gt "$JOB_TICKER" --version "$VERSION"
    rc4=$?
    if [ $rc4 -ne 0 ]; then
      echo "PROGRESS: Phase4 FAILED ticker=$JOB_TICKER: exit code $rc4 -- continuing queue"
      continue
    fi

    # ---------------------------------------------------------------------------
    # Phase5 DISABLED for new campaigns (2026-09-08, user decision).
    #
    # Why: Phase5's only remaining unique output was the core+addon / core+drought /
    # gated core_both overlay CAGRs (candidate_verification_results.addon_cagr_1s /
    # drought_cagr_1s / core_both_cagr_1s), and Phase4 now computes exactly those
    # itself, off the SAME 1s-preferred trade list its own checks 4/8/11/13 already
    # run against (run_optimization_sweep._stacked_overlay_cagrs_gt ->
    # phase4_results.core_addon_cagr_ungated_pct / core_drought_cagr_ungated_pct /
    # core_both_cagr_pct -- the `_ungated` suffix is load-bearing, candidate_full_review.py
    # already uses the unsuffixed names for the GATED flavor of the same concept).
    #
    # The report generators (scripts/candidate_full_review_two_tab.py, scripts/
    # candidate_report_inmemory.py) read Phase4 FIRST but fall back per-field to Phase5's
    # candidate_verification_results, so the ~22k historical rows that only ever went
    # through Phase5 still render -- disabling Phase5 here only stops NEW writes.
    # Two parallel computations of the same number is precisely the shape that produced
    # both the node-mislabeling bug and the drought_factor_gated bug (fixed in 10f1945)
    # -- one side kept going stale while attention was on the other. Removing the
    # duplicate also saves a full extra resimulation pass over the whole candidate
    # population per campaign (real wall-clock cost).
    #
    # DELIBERATELY NOT DELETED: scripts/phase5_second_level_overlay_check.py, the
    # `phase5_trades` table, and `candidate_verification_results` all stay -- they hold
    # real historical computation, Phase4 still READS phase5_trades as its preferred 1s
    # trade source (candidate_verification_store.get_phase5_1s_trades), and Phase5
    # remains runnable by hand for a genuine 1m-vs-1s granularity investigation, which
    # is the one question it answers that Phase4 does not.
    #
    #   $PYTHON scripts/phase5_second_level_overlay_check.py --ticker "$JOB_TICKER" --version "$VERSION"
    #   $PYTHON scripts/append_phase5_combined.py --version "$VERSION"
    # ---------------------------------------------------------------------------

    ticker_banner "$JOB_TICKER: full Phase1->4 pipeline complete"
  done

  echo ""
  echo "All done — $(date)"
  $PYTHON scripts/campaign_registry.py status --campaign-id "$CAMPAIGN_ID"

  # Phase 10 report (2026-09-03, docs/backlog_cache.md's "wire Phase 10 report generation
  # into the sweep pipeline" entry) -- run ONCE here, after the drain loop exits cleanly
  # (every ticker's Phase1-5 done, not on an early-exit/FATAL abort above), never per-
  # ticker: the report is a cross-ticker comparison table (Best Core/Add On/Drought/
  # Best-Both category winners), it only makes sense once the whole campaign's real
  # candidates exist. --version "$VERSION" with no --tickers discovers every real ticker
  # this campaign actually produced candidate_nodes rows for -- correct scope for "the
  # whole campaign," not just the $TICKERS this particular invocation's env started with
  # (a later `enqueue`-appended ticker, per this script's own documented use case above,
  # would otherwise be silently missing from the report). Best-effort: a report-generation
  # failure here must not read as "the campaign itself failed" -- the real sweep/Phase4/5
  # work is already done and persisted by this point regardless of this call's outcome.
  ticker_banner "Phase 10 report (candidate_full_review_two_tab.py) start"
  $PYTHON scripts/candidate_full_review_two_tab.py --version "$VERSION"
  rc10=$?
  if [ $rc10 -ne 0 ]; then
    echo "WARNING: Phase 10 report generation failed (exit code $rc10) -- campaign's real "
    echo "Phase1-5 work above is unaffected; re-run scripts/candidate_full_review_two_tab.py "
    echo "--version \"$VERSION\" manually once the underlying issue is fixed."
  fi
