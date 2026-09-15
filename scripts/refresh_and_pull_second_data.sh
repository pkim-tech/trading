#!/bin/bash
# Per-ticker pipeline for the "new backtest candidates" batch: refreshes minute
# data (safe incremental merge, preserves existing history) into its derived
# table immediately, and separately pulls raw 1s data, compressing it right
# away WITHOUT building massive_second_derived (deliberately left "on ice" --
# build_massive_second_derived.py reads straight from the .gz+sidecar later,
# on demand, whenever a 1s-accurate Phase4 rerun is wanted).
#
# Minute side: fetch_massive_minute_incremental.py (merges new bars into full
# existing canonical, never overwrites/narrows) -> promote_market_data_pull.py
# --kind minute -> build_massive_hourly_derived.py (builds BOTH hourly and
# minute derived tables in one pass, confirmed via its own write_massive_
# hourly_derived/write_massive_minute_derived calls).
#
# Second side: fetch_massive_second_data.py -> promote_market_data_pull.py
# --kind second -> compress_raw_second_csvs.py --delete-originals (no derive
# build here, on purpose).
#
# Usage: TICKERS="A B C" bash scripts/refresh_and_pull_second_data.sh

set -uo pipefail
cd "$(dirname "$0")/.."

TICKERS="${TICKERS:?Set TICKERS="A B C" first}"
PYTHON=.venv/bin/python

for ticker in $TICKERS; do
  echo "================================================================"
  echo "=== $ticker: minute incremental refresh -- $(date '+%H:%M:%S')"
  echo "================================================================"
  if $PYTHON scripts/fetch_massive_minute_incremental.py --tickers "$ticker"; then
    if $PYTHON scripts/promote_market_data_pull.py --ticker "$ticker" --kind minute; then
      echo "=== $ticker: building hourly+minute derived -- $(date '+%H:%M:%S')"
      $PYTHON scripts/build_massive_hourly_derived.py --tickers "$ticker"
    else
      echo "  [$ticker] minute promote FAILED, skipping derived build"
    fi
  else
    echo "  [$ticker] minute incremental fetch FAILED, skipping promote/build"
  fi

  echo "=== $ticker: second-data fetch -- $(date '+%H:%M:%S')"
  if ! $PYTHON scripts/fetch_massive_second_data.py --tickers "$ticker"; then
    echo "  [$ticker] second fetch FAILED, skipping promote/compress"
    continue
  fi

  echo "=== $ticker: second-data promote -- $(date '+%H:%M:%S')"
  if ! $PYTHON scripts/promote_market_data_pull.py --ticker "$ticker" --kind second; then
    echo "  [$ticker] second promote FAILED (likely refuse-to-narrow guard or no staged pull), skipping compress"
    continue
  fi

  echo "=== $ticker: second-data compress (on ice, no derive build) -- $(date '+%H:%M:%S')"
  $PYTHON scripts/compress_raw_second_csvs.py --tickers "$ticker" --delete-originals

  echo "=== $ticker: done -- $(date '+%H:%M:%S')"
done

echo "All done: $(date '+%H:%M:%S')"
