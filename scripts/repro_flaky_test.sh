#!/usr/bin/env bash
# Built 2026-08-15 for the docs/backlog_cache.md item:
# "test_a_broker_fetch_failure_cannot_block_the_exit fails under full-suite
# runs only" -- reruns the FULL suite (real -n auto --dist=loadscope, matching
# how CI/the pre-commit checklist actually invokes it) N times and tallies
# every failing test node id across the runs, so a suspected full-suite-only
# flake can be checked for recurrence without hand-copying pytest output.
#
# Usage: scripts/repro_flaky_test.sh [N_RUNS] [TEST_NODE_ID_SUBSTRING]
#   scripts/repro_flaky_test.sh 5
#   scripts/repro_flaky_test.sh 5 test_a_broker_fetch_failure_cannot_block_the_exit
#
# With TEST_NODE_ID_SUBSTRING given, the script also prints a direct
# pass/fail/absent tally for just that test across the N runs. Without it,
# every FAILED line across all runs is tallied so any flake (not just a
# pre-named suspect) shows up.
#
# Real finding from the run that built this (2026-08-15, Saturday): the
# target test above did NOT reproduce across 3 runs, but a DIFFERENT,
# deterministic (non-flaky, calendar-dependent) failure did:
# tests/test_coverage_check.py::test_compute_status_scenario_expectations_direct_trade_proof_is_verified
# fails on any non-trading-day because it hardcodes today=datetime.now() while
# scripts/coverage_registry.py::_scenario_expectation_recent_proof filters
# `today` out of its lookback window via _is_trading_day. Not fixed here --
# flagged in docs/backlog_cache.md, separate from this script's job.
set -uo pipefail

N_RUNS="${1:-3}"
FILTER="${2:-}"
LOG_DIR="$(mktemp -d /tmp/repro_flaky_test.XXXXXX)"

echo "Running full suite ${N_RUNS}x (pytest.ini's real addopts: -n auto --dist=loadscope)"
echo "Logs: ${LOG_DIR}"

for i in $(seq 1 "$N_RUNS"); do
    echo "=== RUN $i/$N_RUNS ==="
    .venv/bin/python -m pytest -q > "${LOG_DIR}/run${i}.log" 2>&1
    tail -5 "${LOG_DIR}/run${i}.log"
done

echo
echo "=== Every FAILED line across all runs (tallied) ==="
grep -h "^FAILED" "${LOG_DIR}"/run*.log 2>/dev/null | sort | uniq -c | sort -rn

if [ -n "$FILTER" ]; then
    echo
    echo "=== Target: ${FILTER} ==="
    for i in $(seq 1 "$N_RUNS"); do
        if grep -q "FAILED.*${FILTER}" "${LOG_DIR}/run${i}.log"; then
            echo "run $i: FAILED"
        elif grep -qE "(^|::)${FILTER}( |$)" "${LOG_DIR}/run${i}.log"; then
            echo "run $i: passed (ran, no FAILED line)"
        else
            echo "run $i: not observed in output (check log directly)"
        fi
    done
fi

echo
echo "Full logs kept at ${LOG_DIR} for manual inspection."
