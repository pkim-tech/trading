"""Flags docs/backlog_cache.md entries that exceed the file's own 1-2-line
pointer convention (see the reminder block at its top). Detection only --
never rewrites anything. The actual fix (relocate the full writeup verbatim
to docs/deep_backlog.md, leave a one-line pointer here) needs an agent doing
it deliberately, not an unattended script -- see docs/backlog_cache.md's
2026-08-01 (evening) bulk-migration entry for why: a first automated pass at
sentence-boundary trimming cut several entries off mid-sentence. Safe to run
on a cron/pre-commit hook as a linter; not safe to auto-apply the fix.
Usage: .venv/bin/python scripts/check_backlog_cache_lean.py [--max-lines N]
"""
import argparse
import re
import sys
from pathlib import Path

BACKLOG_PATH = Path(__file__).parent.parent / "docs" / "backlog_cache.md"


def find_violations(text, max_lines=2):
    parts = re.split(r"\n(?=## \[)", text)
    violations = []
    for entry in parts[1:]:
        lines = entry.rstrip("\n").split("\n")
        header = lines[0]
        body_lines = [l for l in lines[1:] if l.strip()]
        if len(body_lines) > max_lines:
            violations.append((header, len(body_lines)))
    return violations


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-lines", type=int, default=2)
    args = ap.parse_args()

    text = BACKLOG_PATH.read_text()
    violations = find_violations(text, args.max_lines)

    if not violations:
        print(f"{BACKLOG_PATH}: all entries within {args.max_lines}-line convention.")
        return 0

    print(f"{BACKLOG_PATH}: {len(violations)} entries exceed {args.max_lines} lines:\n")
    for header, n in sorted(violations, key=lambda x: -x[1]):
        print(f"  {n:3d} lines  {header}")
    print(
        "\nFix: relocate each entry's full body verbatim to docs/deep_backlog.md "
        "(never delete/summarize), leave a one-line pointer here. Do this "
        "deliberately, not via an unattended script."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
