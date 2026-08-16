"""Fake-venue harness (Phase 2) -- durable pytest wrapper for the
`entry_abandon_timeout` scenario (fake_venue/scenarios_entry_abandon_timeout.py),
proving `check_entry_abandon` (signals_notify.py:1538) end to end across its
eight branches (clean cancel, dry-run, no-order-id, unrecognized account,
cancel-failed, cancel-unconfirmed, raced-fill, gap-resize-race-guard).

Deliberately drives the harness as a SUBPROCESS, matching every other Phase 2
wrapper's rationale: isolation.configure_env()'s env vars must be set before
any project import, which an in-process test can't reproduce since pytest has
already imported signals_config/schwab_safety by then.

Built alongside the `did_cancel` fix (signals_notify.check_entry_abandon's
'abandoned' coverage_event now records did_cancel in its `detail` field,
matching every other observability convention in this file) -- this file was
the missing pytest integration point for an otherwise fully-built scenario
(unlike its sibling scenarios, no tests/test_fake_venue_entry_abandon_timeout_
scenario.py existed yet; the scenario module itself was complete)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fake_venue import scenarios_meta as meta  # noqa: E402

FIXED_PRICE = 100.0  # explicit override -> no network/yfinance dependency in the suite


@pytest.fixture(autouse=True)
def _restore_environ():
    """Same rationale as every other Phase 2 wrapper's identical fixture --
    isolation.configure_env() writes straight to os.environ."""
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _run_harness(tmp_path, extra_args=()):
    db_path = tmp_path / "fake_venue.db"
    state_dir = tmp_path / "state"
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "fake_venue_harness.py"), "--scenario",
         "entry_abandon_timeout", "--price", str(FIXED_PRICE), "--db-path", str(db_path),
         "--state-dir", str(state_dir), "--keep", *extra_args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600,
        env={**os.environ},
    )
    return proc, db_path


def test_entry_abandon_timeout_scenario_passes_all_required_checks(tmp_path):
    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout[-8000:]}\n{proc.stderr[-4000:]}"
    assert db_path.exists()
    assert "PASS —" in proc.stdout


def test_entry_abandon_timeout_json_report_is_self_consistent(tmp_path):
    proc, _ = _run_harness(tmp_path, extra_args=("--json",))
    assert proc.returncode == 0, proc.stdout[-8000:]
    payload = json.loads([ln for ln in proc.stdout.splitlines() if ln.startswith('{"passed"')][-1])
    assert payload['passed'] is True
    assert all(c['ok'] for c in payload['checks'] if c['required'])
    # Eight watch_list rows (legs A-H) -- see the scenario's own PROOF_SQL.
    assert len(payload['proof_rows']) == 8
    assert payload['observations']['production_path_accesses'] == []


def test_entry_abandon_timeout_did_cancel_recorded_in_coverage_event_detail(tmp_path):
    """Pinned regression for the fix: check_entry_abandon's 'abandoned'
    coverage_event `detail` field now records did_cancel directly (leg A:
    True, a real confirmed cancel_order round-trip; leg B: False, a dry-run
    no-op) -- read straight from the harness DB, not from the printed report
    or the posted Slack text, matching every other proof-by-query test in
    this suite."""
    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout[-8000:]}\n{proc.stderr[-4000:]}"

    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT wl.version, "
            "  (SELECT detail FROM coverage_events WHERE scenario_key='entry_abandon_timeout' "
            "    AND node_id=wl.id AND result='abandoned' LIMIT 1) AS abandon_detail "
            "FROM watch_list wl WHERE wl.ticker=? ORDER BY wl.version", (meta.TICKER,),
        ).fetchall()
    finally:
        conn.close()
    by_suffix = {r['version'].rsplit('_', 1)[-1]: r['abandon_detail'] for r in rows}
    assert by_suffix.get('a', '').endswith('did_cancel=True'), by_suffix.get('a')
    assert by_suffix.get('b', '').endswith('did_cancel=False'), by_suffix.get('b')
