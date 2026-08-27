"""One-time (and reusable) mechanical pass that inserts the script_usage
tracking convention into every scripts/*.py file's `if __name__ ==
'__main__':` block, so recency is instrumented from day one instead of
waiting for each script to be touched/edited naturally. Idempotent --
safe to re-run against new scripts later (skips files already wired).

The inserted snippet is deliberately self-contained (re-imports sys/
pathlib locally rather than assuming the file's existing import setup)
so it can be inserted uniformly into any file regardless of how that
file already manages imports.

Usage:
  .venv/bin/python scripts/add_usage_tracking.py           # apply
  .venv/bin/python scripts/add_usage_tracking.py --dry-run # report only
"""
import argparse
import ast
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_MARKER = "script_usage.record_invocation()"

_SNIPPET = (
    "import sys, pathlib\n"
    "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))\n"
    "import script_usage\n"
    "script_usage.record_invocation()\n"
)


def _find_main_block(tree):
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        t = node.test
        if not isinstance(t, ast.Compare) or len(t.ops) != 1 or not isinstance(t.ops[0], ast.Eq):
            continue
        sides = [t.left, t.comparators[0]]
        names = [s.id for s in sides if isinstance(s, ast.Name)]
        consts = [s.value for s in sides if isinstance(s, ast.Constant)]
        if names == ['__name__'] and consts == ['__main__']:
            return node
    return None


def process(path: Path, dry_run: bool):
    text = path.read_text()
    if _MARKER in text:
        return 'already-wired'
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return f'syntax-error: {e}'
    node = _find_main_block(tree)
    if node is None:
        return 'no-main-block'
    if not node.body:
        return 'empty-main-block'
    first = node.body[0]
    indent = ' ' * first.col_offset
    lines = text.splitlines(keepends=True)
    insert_at = first.lineno - 1  # 0-indexed
    snippet_lines = [indent + l for l in _SNIPPET.splitlines(keepends=True)]
    new_lines = lines[:insert_at] + snippet_lines + lines[insert_at:]
    new_text = ''.join(new_lines)
    try:
        ast.parse(new_text)
    except SyntaxError as e:
        return f'insertion-broke-syntax: {e}'
    if not dry_run:
        path.write_text(new_text)
    return 'wired'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    _SKIP = {
        'add_usage_tracking.py', 'check_script_usage_convention.py', 'list_scripts.py',
    }
    results = {}
    for path in sorted(_SCRIPTS_DIR.glob('*.py')):
        if path.name in _SKIP:
            continue
        results[path.name] = process(path, args.dry_run)

    from collections import Counter
    counts = Counter(results.values())
    for status, n in counts.most_common():
        print(f"{n:4d}  {status}")

    interesting = {k: v for k, v in results.items() if v not in ('wired', 'already-wired', 'no-main-block')}
    if interesting:
        print("\nNeeds manual attention:")
        for name, status in sorted(interesting.items()):
            print(f"  {name}: {status}")

    return 0


if __name__ == '__main__':
    sys.exit(main())
