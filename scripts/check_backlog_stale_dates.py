"""Flags docs/backlog_cache.md entries whose `(revisit ~YYYY-MM-DD)` tag date
has already passed. Detection only -- never rewrites anything (see
check_backlog_cache_lean.py's module docstring for why this project doesn't
auto-mutate backlog_cache.md from a script).

CLAUDE.md's Deferral convention says `go` should flag (not auto-resolve or
drop) any deferred item whose date has passed, so it surfaces again instead
of quietly aging out -- this script makes that check mechanical instead of
relying on catching it by eye during a full read.

Usage: .venv/bin/python scripts/check_backlog_stale_dates.py
"""
import re
import sys
from datetime import date
from pathlib import Path

BACKLOG_PATH = Path(__file__).parent.parent / "docs" / "backlog_cache.md"
REVISIT_RE = re.compile(r"\(revisit ~(\d{4}-\d{2}-\d{2})")


def find_stale(text, today):
    parts = re.split(r"\n(?=## \[)", text)
    stale = []
    for entry in parts[1:]:
        header = entry.split("\n", 1)[0]
        m = REVISIT_RE.search(header)
        if not m:
            continue
        revisit_date = date.fromisoformat(m.group(1))
        if revisit_date <= today:
            stale.append((revisit_date, header))
    return stale


def main():
    today = date.today()
    text = BACKLOG_PATH.read_text()
    stale = find_stale(text, today)

    if not stale:
        print(f"{BACKLOG_PATH}: no revisit-tagged entries past due (today={today}).")
        return

    stale.sort(key=lambda t: t[0])
    print(f"{BACKLOG_PATH}: {len(stale)} revisit-tagged entries past due (today={today}):\n")
    for revisit_date, header in stale:
        days_over = (today - revisit_date).days
        print(f"  [{revisit_date}, {days_over}d overdue] {header}")
    sys.exit(1)


if __name__ == "__main__":
    main()
