"""Mechanical index of every scripts/*.py file -- extracts each script's
module docstring (via ast, not a hand-typed catalog) and prints its first
sentence as a one-line summary, so "what does X do" doesn't require reading
every script from scratch. 78 scripts as of 2026-07-29 and growing; this is
meant to always be run fresh, never hand-maintained (same rationale as
scripts/coverage_registry.py's REGISTRY -- a hand-typed catalog goes stale
the moment a script changes and nobody updates the list).

Usage:
  .venv/bin/python scripts/list_scripts.py            # one-line summary per script
  .venv/bin/python scripts/list_scripts.py --grep foo  # filter by filename or summary text
  .venv/bin/python scripts/list_scripts.py --full bar.py  # print bar.py's whole docstring
"""
import argparse
import ast
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent


def _docstring(path: Path) -> str | None:
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return None
    return ast.get_docstring(tree)


def _first_sentence(docstring: str) -> str:
    """First sentence, collapsing whitespace/newlines -- good enough for a
    one-line index; use --full for the real thing."""
    text = ' '.join(docstring.split())
    for sep in ('. ', ' -- '):
        if sep in text:
            return text.split(sep)[0].strip() + ('.' if sep == '. ' else '')
    return text[:120]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--grep', help='filter to scripts whose filename or summary contains this (case-insensitive)')
    ap.add_argument('--full', help='print the full docstring for this one script (filename, with or without .py)')
    args = ap.parse_args()

    if args.full:
        name = args.full if args.full.endswith('.py') else args.full + '.py'
        path = _SCRIPTS_DIR / name
        if not path.exists():
            print(f"no such script: {name}")
            return 1
        doc = _docstring(path)
        print(doc or "(no module docstring)")
        return 0

    rows = []
    undocumented = []
    for path in sorted(_SCRIPTS_DIR.glob("*.py")):
        if path.name in ('list_scripts.py',):
            continue
        doc = _docstring(path)
        if not doc:
            undocumented.append(path.name)
            continue
        rows.append((path.name, _first_sentence(doc)))

    if args.grep:
        needle = args.grep.lower()
        rows = [(n, s) for n, s in rows if needle in n.lower() or needle in s.lower()]

    width = max((len(n) for n, _ in rows), default=20)
    for name, summary in rows:
        print(f"{name:<{width}}  {summary}")

    if undocumented and not args.grep:
        print(f"\n{len(undocumented)} script(s) with no module docstring: {', '.join(undocumented)}")

    return 0


if __name__ == '__main__':
    sys.exit(main())
