#!/bin/bash
# Real line/branch coverage run (Task #6, 2026-08-31, planner dispatch) --
# distinct signal from scripts/coverage_registry.py's Accountability Grid.
# That Grid answers "does this code path log a coverage_event, and has it
# fired live/paper/dry_run" -- it says nothing about whether any TEST
# actually exercises a given line. This answers the orthogonal question:
# "did the test suite touch this line at all" (or this branch, with
# branch=True in .coveragerc), independent of coverage_events entirely.
#
# Deliberately NOT wired into pytest.ini's default addopts -- coverage
# instrumentation adds real overhead to every routine test run (this repo's
# suite already takes ~5min under -n auto per that file's own comment), not
# worth paying on every local run. Opt-in via this script instead.
#
# Runs under pytest.ini's default -n auto --dist=loadscope (pytest-cov
# natively combines coverage data across xdist workers, no extra config
# needed -- confirmed working 2026-08-31, first real run).
#
# Usage:
#   ./scripts/run_coverage.sh              # term-missing summary + HTML report
#   ./scripts/run_coverage.sh tests/test_foo.py   # scope to one file

cd "$(dirname "$0")/.."

TARGET="${1:-tests/}"

.venv/bin/python -m pytest "$TARGET" \
    --cov --cov-config=.coveragerc \
    --cov-report=term-missing:skip-covered \
    --cov-report=html \
    --cov-report=json:output/coverage.json
