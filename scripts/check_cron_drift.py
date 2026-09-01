#!/usr/bin/env python
"""Diff the real `crontab -l` against docs/expected_crontab.md's fenced cron block.

Exits non-zero and prints missing/extra lines on drift. Whitespace-insensitive,
ignores blank lines and '#' comments on both sides.
"""
import re
import subprocess
import sys
from pathlib import Path

EXPECTED_DOC = Path(__file__).resolve().parent.parent / "docs" / "expected_crontab.md"


def parse_expected(doc_path):
    text = doc_path.read_text()
    m = re.search(r"```cron\n(.*?)```", text, re.DOTALL)
    if not m:
        raise RuntimeError(f"No ```cron fenced block found in {doc_path}")
    lines = [l.rstrip() for l in m.group(1).splitlines()]
    return [l for l in lines if l.strip() and not l.strip().startswith("#")]


def get_live_crontab():
    out = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=True).stdout
    lines = [l.rstrip() for l in out.splitlines()]
    return [l for l in lines if l.strip() and not l.strip().startswith("#")]


def main():
    expected = parse_expected(EXPECTED_DOC)
    live = get_live_crontab()

    expected_set = set(expected)
    live_set = set(live)

    missing = [l for l in expected if l not in live_set]  # documented but not installed
    extra = [l for l in live if l not in expected_set]    # installed but not documented

    if not missing and not extra:
        print(f"OK: live crontab matches {EXPECTED_DOC} ({len(live)} lines)")
        return 0

    if missing:
        print(f"MISSING from live crontab ({len(missing)}):")
        for l in missing:
            print(f"  - {l}")
    if extra:
        print(f"UNDOCUMENTED in live crontab ({len(extra)}):")
        for l in extra:
            print(f"  + {l}")
    return 1


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    sys.exit(main())
