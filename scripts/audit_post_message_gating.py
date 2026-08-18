"""Audits every `_post_message(...)` call site in the live-trading modules and
reports whether it passes the per-node alert gate (`node_id=`) and, if so,
whether it uses the permissive incident gate (`incident=True`).

Built 2026-08-17 for the backlog item "5 clusters of per-position
`_post_message` calls in `signals_notify.py` still ungated (no `node_id`)" --
the follow-on sweep to that night's UNPROTECTED/reconciliation/placement-failure
gating pass. Re-runnable on purpose: new `_post_message` call sites land in this
file regularly, and "does this one have `pos`/`node` in scope but no gate" is
exactly the question that goes stale between sessions.

A site is reported UNGATED-CANDIDATE when it has no `node_id=` argument but a
per-position/per-node variable (`pos`, `node`, `leg`, `wl_id`, ...) is bound
somewhere in the enclosing function -- i.e. it *could* be gated. Genuinely
system-wide alerts (EOD/coverage reports, window alerts) show as UNGATED with no
candidate flag.

Two other real gating styles exist in this codebase and are NOT findings; both
paired reviewers (2026-08-17) independently flagged the first version of this
script for reporting them as gaps, all of them false positives:
  * FN-GATED -- the enclosing function calls `should_alert_live(...)` or
    `has_capital_at_stake(...)` itself, usually as an early return, so its
    `_post_message` calls are already behind the same policy without needing
    `node_id=` (e.g. `notify_buy_signal`, `notify_sell_signal`,
    `check_exit_reminders`). Detected structurally, not hand-listed.
  * DELIBERATE -- a specific call site (keyed by function AND the marker text
    of its message, never by function name alone, so a *new* ungated alert
    added to the same function still reports) that is ungated on purpose.

Exit code is 0 unless --strict is passed, so this can be run for information
without a failing exit; --strict makes it usable as a gate.

Usage:
    .venv/bin/python scripts/audit_post_message_gating.py [--module signals_notify.py] [--all]
    .venv/bin/python scripts/audit_post_message_gating.py --candidates-only --strict
"""
import argparse
import ast
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MODULES = ['signals_notify.py', 'signals_compute.py', 'signals_handlers.py',
                   'paper_trading.py', 'active_signals.py']

# (function name, a distinctive substring of the message) -> why it is
# deliberately ungated. Keyed on the message too, NOT the function alone: a
# function-wide exemption would silently excuse a NEW ungated alert added to
# the same function later, which is exactly the churn this file exists to
# prevent (both paired reviewers flagged the function-keyed first version).
DELIBERATE = {
    ('_throttled_entry_abandon_alert', None):
        'reads the PINNED pending_buys snapshot; gating would re-resolve the live node',
    ('check_live_state_reconciliation', 'reconciliation '):
        'fetch-failure alert is throttled per ACCOUNT, not per node',
    ('_reconcile_buy_fill', 'NO pending_buys'):
        'orphan fill -- no node to attribute it to at all',
    ('_reconcile_buy_fill', 'no matching '):
        'the wl_id hint matched nothing -- gating on a guessed node could hide a real fill',
    ('_reconcile_buy_fill', 'pending buys matched'):
        'several nodes matched -- attribution ambiguous by construction',
    ('drain_fill_queue', None):
        'orphaned-fill fast path -- fires when the fill has no attributable node',
    ('check_gap_resize', 'no account on file'):
        'fires BECAUSE the pinned node has no account; gating re-resolves the live row',
    ('check_entry_abandon', None):
        'same pinned-snapshot design as _throttled_entry_abandon_alert (see its comment)',
    ('check_addon_buying_power_drift', None):
        'alerts unconditionally by design -- see the function docstring',
}

# Variable names that indicate a per-position/per-node context is available.
# `pending`/`pb` added after a reviewer showed their absence made genuinely
# per-node sites (check_buy_reminders, drain_fill_queue) render as system-wide
# -- under-reporting is the more dangerous direction for a gap-finding tool.
POSITION_NAMES = {'pos', 'node', 'leg', 'wl_id', 'position', 'parent_pos',
                  '_leg_node', '_node', '_node_id', 'pending', 'pb'}

# A call to any of these inside the enclosing function means that function
# already applies the same policy itself.
FN_LEVEL_GATES = {'should_alert_live', 'has_capital_at_stake'}


def _names_bound_in(fn):
    out = set()
    for a in fn.args.args + fn.args.kwonlyargs:
        out.add(a.arg)
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            out.add(n.id)
    return out


def _called_names(fn):
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None))
    return out


def _call_text(src_lines, call):
    """The raw source of the call, used only to match DELIBERATE's message
    markers -- ast.get_source_segment needs the whole source, this is cheaper
    and good enough for a substring match."""
    end = getattr(call, 'end_lineno', call.lineno) or call.lineno
    return '\n'.join(src_lines[call.lineno - 1:end])


def _deliberate_reason(func, text):
    for (f, marker), reason in DELIBERATE.items():
        if f == func and (marker is None or marker in text):
            return reason
    return None


def audit(path):
    src = open(path).read()
    src_lines = src.split('\n')
    tree = ast.parse(src)
    rows = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        if fn.name == '_post_message':
            continue
        bound = _names_bound_in(fn)
        has_pos_ctx = bool(bound & POSITION_NAMES)
        fn_gated = bool(_called_names(fn) & FN_LEVEL_GATES)
        for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
            f = call.func
            name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
            if name != '_post_message':
                continue
            kw = {k.arg for k in call.keywords if k.arg}
            gated = 'node_id' in kw
            incident = any(k.arg == 'incident' and isinstance(k.value, ast.Constant) and k.value.value
                           for k in call.keywords)
            reason = None if gated else _deliberate_reason(fn.name, _call_text(src_lines, call))
            rows.append({
                'func': fn.name, 'line': call.lineno, 'gated': gated, 'incident': incident,
                'fn_gated': (not gated) and fn_gated,
                'candidate': (not gated) and has_pos_ctx and not fn_gated and reason is None,
                'deliberate': reason,
            })
    return sorted(rows, key=lambda r: r['line'])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--module', action='append')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--candidates-only', action='store_true')
    ap.add_argument('--strict', action='store_true',
                     help='exit 1 if any ungated per-position candidate remains')
    args = ap.parse_args()
    mods = args.module or (DEFAULT_MODULES if args.all else ['signals_notify.py'])
    total = gated = candidates = 0
    for m in mods:
        path = os.path.join(REPO, m)
        if not os.path.exists(path):
            print(f"  [skip] {m} not found")
            continue
        rows = audit(path)
        print(f"\n=== {m} ({len(rows)} _post_message call sites) ===")
        for r in rows:
            total += 1
            gated += bool(r['gated'])
            candidates += bool(r['candidate'])
            if args.candidates_only and not r['candidate']:
                continue
            if r['gated']:
                tag = 'GATED+INCIDENT' if r['incident'] else 'GATED         '
            elif r['deliberate']:
                tag = 'UNGATED-BY-DES'
            elif r['fn_gated']:
                tag = 'FN-GATED      '
            elif r['candidate']:
                tag = 'UNGATED-CAND. '
            else:
                tag = 'UNGATED (sys) '
            note = f"  # {r['deliberate']}" if r['deliberate'] else ''
            print(f"  {tag}  {m}:{r['line']}  {r['func']}{note}")
    print(f"\n{gated}/{total} gated; {candidates} ungated per-position candidate(s)")
    return 1 if (candidates and args.strict) else 0


if __name__ == '__main__':
    sys.exit(main())
