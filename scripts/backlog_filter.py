"""Extracts only entries carrying a given tag (anywhere in the header's
tag list, e.g. `[backtest]` matches `[backtest][live-trading]` too) from
docs/backlog_cache.md and/or docs/backlog_resolved_recent.md.

Built for `go research` (CLAUDE.md's Session Commands): that command scopes
backlog reads to `[backtest]`-tagged entries, but reading the full file and
filtering by eye still pays the full-file context cost. This does the
filtering before the content ever reaches the model.

Usage:
  .venv/bin/python scripts/backlog_filter.py --tag backtest
  .venv/bin/python scripts/backlog_filter.py --tag backtest docs/backlog_cache.md
"""
import argparse
import re
import sys
from pathlib import Path

DOCS_DIR = Path(__file__).parent.parent / "docs"
DEFAULT_FILES = [DOCS_DIR / "backlog_cache.md", DOCS_DIR / "backlog_resolved_recent.md"]

HEADER_RE = re.compile(r"^## ((?:\[[^\]]+\])+)")


def entry_tags(header_line):
    m = HEADER_RE.match(header_line)
    if not m:
        return []
    return [t.lower() for t in re.findall(r"\[([^\]]+)\]", m.group(1))]


def filter_entries(text, tag):
    tag = tag.lower()
    parts = re.split(r"\n(?=## \[)", text)
    preamble = parts[0] if not parts[0].lstrip().startswith("## [") else ""
    matched, skipped = [], 0
    for entry in parts[1:] if preamble else parts:
        header = entry.split("\n", 1)[0]
        if tag in entry_tags(header):
            matched.append(entry.rstrip("\n"))
        else:
            skipped += 1
    return preamble, matched, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, help="tag to match, e.g. backtest")
    ap.add_argument("files", nargs="*", type=Path, default=DEFAULT_FILES)
    ap.add_argument("--no-preamble", action="store_true", help="omit each file's leading note block")
    args = ap.parse_args()

    total_in_lines = total_out_lines = 0
    for path in args.files:
        if not path.exists():
            print(f"# {path}: not found, skipping", file=sys.stderr)
            continue
        text = path.read_text()
        preamble, matched, skipped = filter_entries(text, args.tag)
        in_lines = text.count("\n") + 1
        out_lines = sum(e.count("\n") + 1 for e in matched)
        total_in_lines += in_lines
        total_out_lines += out_lines

        print(f"\n{'=' * 3} {path.name} — {len(matched)} entries tagged [{args.tag}], {skipped} skipped ({in_lines}→{out_lines} lines) {'=' * 3}")
        if preamble.strip() and not args.no_preamble:
            print(preamble.rstrip())
        for entry in matched:
            print()
            print(entry)

    if total_in_lines:
        pct = 100 * (1 - total_out_lines / total_in_lines)
        print(f"\n# total: {total_in_lines}→{total_out_lines} lines ({pct:.0f}% reduction)", file=sys.stderr)


if __name__ == "__main__":
    main()
