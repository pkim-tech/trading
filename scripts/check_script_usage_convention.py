"""Invariant check: every scripts/*.py file with an `if __name__ ==
'__main__':` block must call script_usage.record_invocation() as part of
it, so the usage-recency log (logs/script_usage.log) can't silently drift
out of sync as new scripts get added. Run manually after adding a script,
or wire into a pre-commit/CI step.

Usage:
  .venv/bin/python scripts/check_script_usage_convention.py
"""
import ast
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_MARKER = "script_usage.record_invocation()"

# Files exempt from the convention (e.g. this file and its bulk-insertion
# sibling don't need to self-track; add a real reason here if a genuine
# exemption is ever needed for a normal script).
_EXEMPT = {'list_scripts.py'}  # meta/introspection tool that reads the usage log itself


def _find_main_block(text: str):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
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


def main():
    violations = []
    for path in sorted(_SCRIPTS_DIR.glob('*.py')):
        if path.name in _EXEMPT:
            continue
        text = path.read_text()
        node = _find_main_block(text)
        if node is None:
            continue
        # Scope the marker check to the __main__ block's own source, not the
        # whole file -- a tool that mentions _MARKER as a string constant
        # (like this file and add_usage_tracking.py do) would otherwise
        # false-positive as "compliant" without actually being wired.
        lines = text.splitlines(keepends=True)
        block_text = ''.join(lines[node.lineno - 1:node.end_lineno])
        if _MARKER not in block_text:
            violations.append(path.name)

    if violations:
        print(f"{len(violations)} script(s) missing the usage-tracking convention:")
        for name in violations:
            print(f"  {name}")
        print("\nFix: .venv/bin/python scripts/add_usage_tracking.py")
        return 1

    print("All scripts with a __main__ block follow the usage-tracking convention.")
    return 0


if __name__ == '__main__':
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    sys.exit(main())
