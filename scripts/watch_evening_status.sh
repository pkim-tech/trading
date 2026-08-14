#!/bin/bash
# Loops scripts/evening_status.py, clearing the screen each pass.
# Usage: scripts/watch_evening_status.sh [part] [interval_seconds]
#   scripts/watch_evening_status.sh          # all parts, every 60s
#   scripts/watch_evening_status.sh 2 30     # part 2 only, every 30s
cd "$(dirname "$0")/.." || exit 1
PART="${1:-all}"
INTERVAL="${2:-60}"
while true; do
    clear
    date
    .venv/bin/python scripts/evening_status.py "$PART"
    sleep "$INTERVAL"
done
