"""Pivot view over signals_db.coverage_events: rows = scenario_key (a control/
phase in the live-trading automation engine), columns = paper/dry_run/live.
Each cell shows the count of real firings plus the most recent date + result,
so any control x environment combination can be looked up directly instead of
hand-maintaining docs/live_test_coverage.md's status text.

Usage: .venv/bin/python scripts/coverage_matrix.py [--scenario KEY] [--ticker T]
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

MODES = ["paper", "dry_run", "live", "unattributed"]  # "unattributed": a real event
# fired but the mode couldn't be determined (e.g. check_gap_resize's missing-account
# skip) -- must stay in this list or that event silently disappears from the pivot.


def build_matrix(scenario=None, ticker=None):
    events = db.get_coverage_events(scenario_key=scenario, limit=100_000)
    if ticker:
        events = [e for e in events if e["ticker"] == ticker]

    cells = defaultdict(list)  # (scenario_key, mode) -> [events], newest first (get_coverage_events order)
    for e in events:
        cells[(e["scenario_key"], e["mode"])].append(e)

    scenario_keys = sorted({e["scenario_key"] for e in events})
    return scenario_keys, cells


def format_cell(events):
    if not events:
        return "—"
    latest = events[0]  # already newest-first
    return f"{len(events)}x last={latest['ts'][:10]} ({latest['result']})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default=None)
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--detail", action="store_true", help="print every raw event row for the matched scenario/ticker instead of the pivot")
    args = ap.parse_args()

    if args.detail:
        events = db.get_coverage_events(scenario_key=args.scenario, limit=200)
        if args.ticker:
            events = [e for e in events if e["ticker"] == args.ticker]
        for e in events:
            print(f"{e['ts']}  {e['scenario_key']:<30} {e['mode']:<8} {e['ticker'] or '':<6} "
                  f"{e['result']:<25} {e['detail']}")
        return

    scenario_keys, cells = build_matrix(args.scenario, args.ticker)
    if not scenario_keys:
        print("No coverage_events rows yet -- nothing has fired through an instrumented control "
              "path since this table was added. Run the daemon (paper/dry_run) or check back "
              "once a real signal window passes.")
        return

    rows = [[format_cell(cells.get((key, mode), [])) for mode in MODES] for key in scenario_keys]
    name_w = max(len(k) for k in scenario_keys) + 2
    col_w = [max(len(MODES[i]), max((len(r[i]) for r in rows), default=0)) + 2 for i in range(len(MODES))]

    header = f"{'scenario_key':<{name_w}}" + "".join(f"{m:<{col_w[i]}}" for i, m in enumerate(MODES))
    print(header)
    print("-" * len(header))
    for key, row in zip(scenario_keys, rows):
        line = f"{key:<{name_w}}"
        for i, cell in enumerate(row):
            line += f"{cell:<{col_w[i]}}"
        print(line)


if __name__ == "__main__":
    main()
