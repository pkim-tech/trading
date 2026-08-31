#!/bin/bash
# Resumable full-pipeline (Phase1->2->2.5->4->5) queue runner for the in-memory
# GT sweep pipeline (scripts/bench_phase1_phase2_inmemory.py), NOT the legacy
# run_optimization_sweep.py path run_sweep_queue.sh already covers -- see that
# file for the old-pipeline equivalent, do not merge the two.
#
# Per ticker, in order: run bench_phase1_phase2_inmemory.py once per strategy
# (--fixed-sl-values loops fixed_sl 1-8 within that one process/pool, see that
# script's own --fixed-sl-values help), then Phase4 (candidate_summary_report.py
# --kernel gt) then Phase5 (phase5_second_level_overlay_check.py) against the
# SAME campaign just produced, before moving to the next ticker. A ticker that
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
# Version-string convention (must match bench_phase1_phase2_inmemory.py's own
# construction exactly, see that file's main(), or Phase5's --version match
# will find zero scopes): "v6.5-" (2026-08-31, name-reservation tag for this
# PROMOTION_ALGO_VERSION=3 campaign, see docs/plans/ground_truth_kernel_rebuild.md --
# paired-review CONFIRMED HIGH fixup: this literal was originally missed here when
# the prefix was added to the Python side, which would have made this queue's
# Phase4/Phase5 --version calls resolve zero scopes) + "bench-inmemory-v6" +
# "-massive" (DATA_SOURCE default)
# + window_version_suffix(START, END) (module defaults "2021-08-23"/"2026-08-21"
# -- NOT overridden by this queue, so left out of the CLI calls below) + a
# "-z<v1>-<v2>-..." suffix whenever --z-thresholds is passed (it always is,
# here), + a "-isl<N>" suffix whenever --n-islands is passed (it always is,
# here, added 2026-08-29 fixing a paired-review CONFIRMED HIGH finding on the
# --n-islands diff -- a run without this suffix would be indistinguishable in
# sweep_run_log/candidate_nodes from a default-N_ISLANDS run at the same
# scope), + an UNCONDITIONAL "-pv<N>" pipeline-algorithm-version suffix (added
# 2026-08-30, planner dispatch item 3 -- see PROMOTION_ALGO_VERSION's own
# comment below and bench_phase1_phase2_inmemory.py's matching module
# constant) -- unlike every other suffix here, this one always fires
# regardless of CLI flags, since it marks the PROMOTION ALGORITHM itself, not
# a sweep-parameter override. NOTE: --window is deliberately NOT part of the
# version string (only --z-thresholds, --seed-watch-list-id, --n-islands, and
# now the pipeline-version marker are) -- confirmed by reading main() -- so
# the widened window grid below doesn't need (and must not get) its own
# suffix here.
#
# Phase4 (--kernel gt) now takes --version too (added 2026-08-30, planner
# dispatch): phase4_candidate_nodes_resolver.discover_all_candidate_nodes_scopes
# is still version-agnostic by design (matching prune_backtest_cache_
# ground_truth's own ticker-agnostic-of-version discovery pattern), but without
# a filter it re-processes EVERY historical candidate_nodes version for the
# ticker on every run -- confirmed real (SOXL alone has 20 distinct versions,
# 472 rows) and wasteful, not just noisy. Passing --version "$VERSION" here
# restricts run_gt_mode's candidate_nodes fallback to this run's own version
# string, same scoping Phase5 already applies via its own --version.
#
# Usage:
#   ./scripts/run_inmemory_sweep_queue.sh
# Overrides via env: TICKERS, STRATEGIES, FIXED_SL_VALUES, Z_THRESHOLDS,
#   WINDOWS, N_ISLANDS, WORKERS.
#
# Deliberately NOT launched by this task -- building/committing this script is
# the full scope; a peer session launches it once the separate --n-islands
# paired review clears.

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

# Pipeline-version discriminator (2026-08-30, planner dispatch item 3) -- MUST match
# bench_phase1_phase2_inmemory.py's own PROMOTION_ALGO_VERSION module constant exactly
# (see that constant's own docstring for the full reasoning: without this, a re-run of
# the exact same tickers/parameters after a real promotion-algorithm change -- backfill/
# gating/scope-detection logic, not a sweep-parameter change -- would silently produce an
# IDENTICAL version string to a prior, algorithmically different campaign). No shared
# single source of truth between this shell script and that Python module -- same
# manual-sync convention the Z_THRESHOLDS/N_ISLANDS suffixes below already rely on.
# Bumped 2 -> 3 (2026-08-30, paired-review HIGH finding): the new arm_pct backfill +
# N_ISLANDS 10->3 revert are both material promotion-algorithm changes -- MUST match
# bench_phase1_phase2_inmemory.py's own PROMOTION_ALGO_VERSION exactly.
# Bumped 3 -> 4 (2026-08-31, planner dispatch, paired-review HIGH finding): the new
# N_GENERATIONS multi-generation Phase2-island loop is a material promotion-algorithm
# change (changes which cells get explored/promoted) -- MUST match
# bench_phase1_phase2_inmemory.py's own PROMOTION_ALGO_VERSION exactly. NOTE: a queue
# process already running under the OLD pv3 code computed its own $VERSION once at
# startup and is unaffected by this file edit mid-run (same as Python not re-reading
# source mid-process) -- this bump only affects a FUTURE invocation of this script.
PROMOTION_ALGO_VERSION=4

# Must match bench_phase1_phase2_inmemory.py's own version construction --
# see the header comment above. Z_THRESHOLDS values are joined with '-' exactly
# as that script's own f"-z{'-'.join(str(z) for z in Z_THRESHOLDS)}" does.
Z_SUFFIX=$(echo "$Z_THRESHOLDS" | tr ' ' '-')
VERSION="v6.5-bench-inmemory-v6-massive-w2021-08-23_2026-08-21-z${Z_SUFFIX}-isl${N_ISLANDS}-pv${PROMOTION_ALGO_VERSION}"

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

{
  echo "======================================================"
  echo " In-memory sweep queue start — $(date)"
  echo " Tickers: $TICKERS"
  echo " Strategies: $STRATEGIES"
  echo " Fixed SLs: $FIXED_SL_VALUES"
  echo " Z-thresholds: $Z_THRESHOLDS"
  echo " Windows: $WINDOWS"
  echo " N-islands: $N_ISLANDS"
  echo " Expected version string: $VERSION"
  echo "======================================================"

  for ticker in $TICKERS; do
    ticker_banner "$ticker: Phase1-2.5 start"
    ticker_failed=0

    for strategy in $STRATEGIES; do
      echo ""
      echo "--- $ticker | $strategy | fixed_sl=$FIXED_SL_VALUES — $(date) ---"
      $PYTHON scripts/bench_phase1_phase2_inmemory.py \
          --ticker "$ticker" \
          --strategy "$strategy" \
          --fixed-sl-values $FIXED_SL_VALUES \
          --z-thresholds $Z_THRESHOLDS \
          --window $WINDOWS \
          --n-islands "$N_ISLANDS" \
          --workers "$WORKERS"
      rc=$?
      if [ $rc -ne 0 ]; then
        echo "PROGRESS: Phase1-2.5 FAILED ticker=$ticker strategy=$strategy: exit code $rc -- skipping rest of $ticker, continuing queue"
        ticker_failed=1
        break
      fi
    done

    if [ $ticker_failed -ne 0 ]; then
      continue
    fi

    ticker_banner "$ticker: Phase4 (candidate_summary_report.py --kernel gt) start"
    $PYTHON scripts/candidate_summary_report.py --kernel gt "$ticker" --version "$VERSION"
    rc=$?
    if [ $rc -ne 0 ]; then
      echo "PROGRESS: Phase4 FAILED ticker=$ticker: exit code $rc -- skipping Phase5, continuing queue"
      continue
    fi

    ticker_banner "$ticker: Phase5 (phase5_second_level_overlay_check.py) start"
    $PYTHON scripts/phase5_second_level_overlay_check.py --ticker "$ticker" --version "$VERSION"
    rc=$?
    if [ $rc -ne 0 ]; then
      echo "PROGRESS: Phase5 FAILED ticker=$ticker: exit code $rc -- continuing queue"
      continue
    fi

    # Rebuilds the single running cross-campaign phase5_second_level_overlay_
    # check_ALL_<VERSION> file from every per-ticker file that exists so far
    # (2026-08-30, planner dispatch) -- so the user never has to open per-ticker
    # files or manually re-merge as the campaign progresses.
    $PYTHON scripts/append_phase5_combined.py --version "$VERSION"

    ticker_banner "$ticker: full Phase1->5 pipeline complete"
  done

  echo ""
  echo "All done — $(date)"
} 2>&1 | tee "$LOG"
