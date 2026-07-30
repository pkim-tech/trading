"""CLI wrapper for signals_notify.build_phased_monitors_report -- see that
function's docstring for what it reports (pre_action_state_verification and
node_circuit_breaker_tripped coverage_events for the given date, plus the
current live breaker streak state). The daemon calls the same function
automatically at 16:05 ET daily (active_signals.py's _EOD_REPORT_TIME slot,
log-only, no Slack post) -- this script is for on-demand/manual review.

Usage:
  .venv/bin/python scripts/phased_monitors_report.py [--date YYYY-MM-DD]
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_notify


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, defaults to today")
    args = parser.parse_args()
    check_date = args.date or date.today().isoformat()
    print(signals_notify.build_phased_monitors_report(check_date))


if __name__ == "__main__":
    main()
