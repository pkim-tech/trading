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
# will find zero scopes): "bench-inmemory-v6" + "-massive" (DATA_SOURCE default)
# + window_version_suffix(START, END) (module defaults "2021-08-23"/"2026-08-21"
# -- NOT overridden by this queue, so left out of the CLI calls below) + a
# "-z<v1>-<v2>-..." suffix whenever --z-thresholds is passed (it always is,
# here), + a "-isl<N>" suffix whenever --n-islands is passed (it always is,
# here, added 2026-08-29 fixing a paired-review CONFIRMED HIGH finding on the
# --n-islands diff -- a run without this suffix would be indistinguishable in
# sweep_run_log/candidate_nodes from a default-N_ISLANDS run at the same
# scope). NOTE: --window is deliberately NOT part of the version string (only
# --z-thresholds, --seed-watch-list-id, and now --n-islands are) -- confirmed
# by reading main() -- so the widened window grid below doesn't need (and
# must not get) its own suffix here.
#
# Phase4 (--kernel gt) intentionally does NOT take --version -- it auto-
# discovers every real candidate_nodes scope for the given ticker across every
# version (phase4_candidate_nodes_resolver.discover_all_candidate_nodes_scopes
# is explicitly version-agnostic by design, matching prune_backtest_cache_
# ground_truth's own ticker-agnostic-of-version discovery pattern) -- so a
# bare `--kernel gt <TICKER>` already picks up this run's output with no
# version plumbing needed.
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

PYTHON=".venv/bin/python"
TICKERS="${TICKERS:-AGQ ETHU OILU GDXU UGL WEBL}"
STRATEGIES="${STRATEGIES:-TrailingBothZScoreBreakout TrailingExitZScoreBreakout}"
FIXED_SL_VALUES="${FIXED_SL_VALUES:-1 2 3 4 5 6 7 8}"
Z_THRESHOLDS="${Z_THRESHOLDS:-0.5 1.0 1.5 2.0}"
WINDOWS="${WINDOWS:-5 10 15 20}"
N_ISLANDS="${N_ISLANDS:-10}"
WORKERS="${WORKERS:-8}"

# Must match bench_phase1_phase2_inmemory.py's own version construction --
# see the header comment above. Z_THRESHOLDS values are joined with '-' exactly
# as that script's own f"-z{'-'.join(str(z) for z in Z_THRESHOLDS)}" does.
Z_SUFFIX=$(echo "$Z_THRESHOLDS" | tr ' ' '-')
VERSION="bench-inmemory-v6-massive-w2021-08-23_2026-08-21-z${Z_SUFFIX}-isl${N_ISLANDS}"

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
    echo ""
    echo "=== $ticker: Phase1-2.5 start — $(date) ==="
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

    echo ""
    echo "=== $ticker: Phase4 (candidate_summary_report.py --kernel gt) start — $(date) ==="
    $PYTHON scripts/candidate_summary_report.py --kernel gt "$ticker"
    rc=$?
    if [ $rc -ne 0 ]; then
      echo "PROGRESS: Phase4 FAILED ticker=$ticker: exit code $rc -- skipping Phase5, continuing queue"
      continue
    fi

    echo ""
    echo "=== $ticker: Phase5 (phase5_second_level_overlay_check.py) start — $(date) ==="
    $PYTHON scripts/phase5_second_level_overlay_check.py --ticker "$ticker" --version "$VERSION"
    rc=$?
    if [ $rc -ne 0 ]; then
      echo "PROGRESS: Phase5 FAILED ticker=$ticker: exit code $rc -- continuing queue"
      continue
    fi

    echo ""
    echo "=== $ticker: full Phase1->5 pipeline complete — $(date) ==="
  done

  echo ""
  echo "All done — $(date)"
} 2>&1 | tee "$LOG"
