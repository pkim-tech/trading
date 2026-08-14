#!/bin/bash
# Runs a real sweep restricted to a trailing N-year window, by temporarily
# truncating the target ticker's cached hourly CSV in place and restoring it
# afterward -- the kernel has no native date-window parameter (confirmed
# 2026-08-13), so this is the safe way to get a real bounded-window sweep
# without modifying backtester.py/run_optimization_sweep.py.
#
# Safety: backs up the real CSV first, restores it via `trap ... EXIT` so a
# crash mid-sweep can never leave the truncated version stuck at the real
# shared path -- same pattern as this project's config.json-patch-then-restore
# scripts (e.g. run_v5_gdxd_test.sh). Only the ONE ticker's CSV is touched;
# every other cached ticker is untouched throughout.
#
# Real risk (accepted, per user 2026-08-13): a concurrent research process
# reading this exact ticker's full history during the sweep window would see
# truncated data. The live daemon is NOT at risk (only reads recent bars).
# User's own operational discipline: only run one research process at a time
# during a run of this script.
#
# Usage:
#   ./scripts/run_1y_window_sweep.sh TICKER YEARS END_DATE VERSION_TAG [--skip-cache-refresh]
#   ./scripts/run_1y_window_sweep.sh JNUG 1 2026-08-13 v5.1-1y
#
# END_DATE is a real, explicit, fixed date (not "today") -- per the
# provenance discussion this session, a sweep window must be pinned to a
# known date, not implicitly drift with whenever the script happens to run.
#
# VERSION_TAG must be a real, distinct, mintable version string extending the
# existing v5 lineage (e.g. v5.1-1y, v5.1-max) -- v6 means a genuinely new
# kernel/strategy generation in this project's history, not just a different
# sweep window. Per the same discussion, `version` doesn't need a schema change
# to support this, just a new distinct tag per campaign, same pattern as
# v5.1 already is its own distinct value from v5. Don't reuse an existing tag.

set -e
cd "$(dirname "$0")/.."

TICKER="$1"
YEARS="$2"
END_DATE="$3"
VERSION_TAG="$4"
shift 4 || true

if [ -z "$TICKER" ] || [ -z "$YEARS" ] || [ -z "$END_DATE" ] || [ -z "$VERSION_TAG" ]; then
    echo "Usage: $0 TICKER YEARS END_DATE VERSION_TAG [--skip-cache-refresh]"
    exit 1
fi

PYTHON=".venv/bin/python"
CSV="cache/research/${TICKER}_1h.csv"
BACKUP="cache/research/${TICKER}_1h.csv.pre_window_sweep_$(date +%Y%m%d_%H%M%S)"

if [ ! -f "$CSV" ]; then
    echo "No cached CSV for $TICKER at $CSV"
    exit 1
fi

cp "$CSV" "$BACKUP"
trap 'cp "$BACKUP" "$CSV" && echo "Restored real $TICKER CSV from $BACKUP" || echo "!!! RESTORE FAILED -- $CSV may still be truncated, manually restore from $BACKUP !!!"' EXIT

$PYTHON - "$TICKER" "$YEARS" "$END_DATE" "$CSV" <<'PYEOF'
import sys
import pandas as pd
from datetime import timedelta

ticker, years, end_date, csv_path = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4]
end = pd.Timestamp(end_date) + pd.Timedelta(hours=23, minutes=59)
start = end - timedelta(days=int(365.25 * years))

df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
before = len(df)
df = df[(df.index >= start) & (df.index <= end)]
after = len(df)
if after == 0:
    print(f"ERROR: window {start} -> {end} produced zero rows for {ticker}, aborting before overwrite")
    sys.exit(1)
df.to_csv(csv_path)
print(f"{ticker}: truncated {before} -> {after} rows, window {start.date()} -> {end.date()}")
PYEOF

echo "Running sweep on truncated window, version=$VERSION_TAG..."
refresh_flag=""
[ "$1" = "--skip-cache-refresh" ] && refresh_flag="--skip-cache-refresh"
$PYTHON run_optimization_sweep.py --version "$VERSION_TAG" --tickers "$TICKER" $refresh_flag

echo "Sweep done -- CSV will be restored on exit via trap."
