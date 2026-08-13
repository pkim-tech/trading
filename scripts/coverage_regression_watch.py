"""Full-Grid regression watch -- broader than capital_scaling_gate.py's 13
money-relevant rows: this watches every one of scripts/coverage_registry.py's
67 REGISTRY rows for a status that got WORSE since the last run, regardless
of whether the underlying control has anything to do with real capital.
Built 2026-08-13, same session, after the user's explicit correction: "it's
not just capital scaling... if a control is failing that isn't capital
specific, i'd like to know about it."

Distinct from scripts/coverage_grid_summary.py (the existing nightly EOD
day-over-day diff): that tool keys snapshots by CALENDAR DATE, so calling it
twice in the same session silently overwrites the "before this change"
baseline before any diff can happen (INSERT OR REPLACE on
(snapshot_date, scenario_key), diffed against `snapshot_date < today` --
strictly less than, never same-day). This tool keys snapshots by RUN instead
(one row per invocation, git commit attached), so it's safe to run multiple
times per session -- e.g. once before a change, once after -- and still get
a real diff. Meant to be run after every session that touches live-trading
code, per the project's session-wrap convention, not just a one-shot read.

Usage: .venv/bin/python scripts/coverage_regression_watch.py [--all]
  (default: only prints rows that changed since the last run, plus any
  currently-red row; --all prints the full 67-row grid every time)
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db
from scripts.coverage_registry import REGISTRY, compute_status, STATUS_ORDER
from scripts.capital_scaling_gate import GATE_ROWS, _git_state

# Lower STATUS_ORDER rank = worse. Anything at or below this rank is "red"
# for the purposes of --all's "currently failing" highlight.
_RED_RANK_CEILING = 1.5  # covers deviation-unexplained, wired-never-fired,
                          # *-attempt-failed, structural-gap

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALL_PY_FILES = {p.name for p in _REPO_ROOT.glob("*.py")} | \
                {f"scripts/{p.name}" for p in (_REPO_ROOT / "scripts").glob("*.py")}
_EVIDENCE_TS_RE = re.compile(r"last (\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")


def _files_in_code_path(code_path):
    """Best-effort: pull real .py filenames referenced in a REGISTRY row's
    free-text code_path (e.g. 'signals_notify._attempt_automated_sell mode
    check' -> signals_notify.py). Matches a leading dotted identifier or any
    bare mention of a known module name against the real files on disk --
    deliberately conservative (returns [] rather than guessing) since a
    wrong file would produce a false staleness verdict."""
    found = []
    for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", code_path):
        candidate = f"{word}.py"
        if candidate in _ALL_PY_FILES and candidate not in found:
            found.append(candidate)
    return found


def _last_git_mtime(filename):
    """Last commit timestamp touching this file, or None if untracked/no
    history. filename may be 'scripts/foo.py' or a bare root-level name."""
    path = filename if '/' in filename else filename
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", path],
            cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
        return datetime.fromisoformat(out) if out else None
    except Exception:
        return None


def _evidence_timestamp(detail):
    m = _EVIDENCE_TS_RE.search(detail or "")
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1).replace(' ', 'T'))
    except ValueError:
        return None


def staleness_for(row, status, detail):
    """Returns None if not applicable/can't be determined, else
    (is_stale: bool, code_file: str, code_mtime, evidence_ts) -- the real
    "has this control been re-proven since the code implementing it last
    changed" question, distinct from regression detection (which only
    catches a status that got WORSE, not one that's silently gone unverified
    against changed code while still reading green from an old event)."""
    if STATUS_ORDER.get(status, 99) < 2:  # already red/unproven -- staleness is moot
        return None
    files = _files_in_code_path(row.get('code_path', ''))
    if not files:
        return None
    evidence_ts = _evidence_timestamp(detail)
    if evidence_ts is None:
        return None
    mtimes = [(f, _last_git_mtime(f)) for f in files]
    mtimes = [(f, t) for f, t in mtimes if t is not None]
    if not mtimes:
        return None
    newest_file, newest_mtime = max(mtimes, key=lambda ft: ft[1])
    is_stale = newest_mtime.replace(tzinfo=None) > evidence_ts
    return is_stale, newest_file, newest_mtime, evidence_ts


def ensure_table():
    with db._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS coverage_run_snapshot (
            run_id      INTEGER,
            ts          TEXT NOT NULL DEFAULT (datetime('now')),
            git_commit  TEXT,
            scenario_id TEXT NOT NULL,
            status      TEXT NOT NULL,
            detail      TEXT
        )""")
        c.commit()


def _next_run_id():
    with db._conn() as c:
        row = c.execute("SELECT MAX(run_id) FROM coverage_run_snapshot").fetchone()
    return (row[0] or 0) + 1


def last_run_statuses():
    """Returns (run_id, ts, {scenario_id: status}) for the most recent
    logged run, or (None, None, {}) if this is the first run ever."""
    with db._conn() as c:
        last_run_id = c.execute("SELECT MAX(run_id) FROM coverage_run_snapshot").fetchone()[0]
        if last_run_id is None:
            return None, None, {}
        rows = c.execute(
            "SELECT scenario_id, status, ts FROM coverage_run_snapshot WHERE run_id = ?", (last_run_id,)
        ).fetchall()
    ts = rows[0]['ts'] if rows else None
    return last_run_id, ts, {r['scenario_id']: r['status'] for r in rows}


def log_run(run_id, git_commit, rows):
    with db._conn() as c:
        c.executemany(
            "INSERT INTO coverage_run_snapshot (run_id, git_commit, scenario_id, status, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            [(run_id, git_commit, k, v[0], v[1][:200]) for k, v in rows.items()],
        )
        c.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--all', action='store_true', help='Print the full grid every run, not just changes')
    args = ap.parse_args()

    ensure_table()
    today_rows = {r['id']: compute_status(r) for r in REGISTRY}
    prior_run_id, prior_ts, prior_statuses = last_run_statuses()

    print(f"=== Full-Grid regression watch ({len(today_rows)} rows) ===\n")

    if prior_run_id is None:
        print("No prior run on file -- this is the first recorded baseline, nothing to diff yet.\n")
    else:
        regressed, improved = [], []
        for k, (status, detail) in today_rows.items():
            old = prior_statuses.get(k)
            if old is None or old == status:
                continue
            old_rank = STATUS_ORDER.get(old, 99)
            new_rank = STATUS_ORDER.get(status, 99)
            (regressed if new_rank < old_rank else improved).append((k, old, status))

        if regressed:
            print(f"🔻 REGRESSED since run #{prior_run_id} ({prior_ts}) -- a control that was working "
                  f"is now worse:")
            for k, old, new in sorted(regressed):
                capital_note = "  [CAPITAL-SCALING ROW]" if k in GATE_ROWS else ""
                print(f"    {k:42s} {old} -> {new}{capital_note}")
            print()
        if improved:
            print(f"📈 Improved since run #{prior_run_id} ({prior_ts}):")
            for k, old, new in sorted(improved):
                print(f"    {k:42s} {old} -> {new}")
            print()
        if not regressed and not improved:
            print(f"No status change vs run #{prior_run_id} ({prior_ts}).\n")

    if args.all or prior_run_id is None:
        print("=== Full grid ===")
        ordered = sorted(today_rows.items(), key=lambda kv: STATUS_ORDER.get(kv[1][0], 99))
        for k, (status, detail) in ordered:
            red = STATUS_ORDER.get(status, 99) <= _RED_RANK_CEILING
            mark = '🔴' if red else '  '
            print(f"  {mark} {status:22s} {k:42s} {detail[:100]}")
    else:
        currently_red = [(k, s, d) for k, (s, d) in today_rows.items()
                          if STATUS_ORDER.get(s, 99) <= _RED_RANK_CEILING]
        if currently_red:
            print(f"🔴 Currently red ({len(currently_red)} rows, run --all to see the full grid):")
            for k, s, d in sorted(currently_red, key=lambda r: r[0]):
                print(f"    {s:22s} {k:42s} {d[:100]}")

    stale = []
    for k, (status, detail) in today_rows.items():
        result = staleness_for(next(r for r in REGISTRY if r['id'] == k), status, detail)
        if result and result[0]:
            stale.append((k, status, *result[1:]))
    if stale:
        print(f"\n🕰 STALE -- code changed since the evidence was captured ({len(stale)} rows). "
              f"The Grid still reads green, but that proof predates the current code -- re-verify, "
              f"don't trust it as-is:")
        for k, status, code_file, code_mtime, evidence_ts in sorted(stale, key=lambda r: r[0]):
            print(f"    {k:42s} {status:15s} {code_file} changed {code_mtime.date()}, "
                  f"evidence from {evidence_ts.date()}")
        print("    To force a fresh execution instead of waiting on an organic trigger: see "
              "docs/live_test_coverage.md's direct-bypass procedure, or restage a canary node "
              "(scripts/restage_canary_nodes.py / the live-test-node-setup skill) positioned for "
              "this specific scenario.")

    counts = {}
    for _, (status, _) in today_rows.items():
        counts[status] = counts.get(status, 0) + 1
    print(f"\n{len(today_rows)} rows total: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    run_id = _next_run_id()
    git_commit, _dirty = _git_state()
    log_run(run_id, git_commit, today_rows)
    print(f"Logged as run #{run_id} (commit={git_commit or '?'}).")


if __name__ == '__main__':
    main()
