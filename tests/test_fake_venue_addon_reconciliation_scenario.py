"""Fake-venue harness (Phase 2) -- durable check that the
`addon_reconciliation` scenario really does reproduce
signals_notify.check_addon_leg_reconciliation's two independent real-world
drift cases end-to-end: (1) a still-resting, unfilled real add-on entry
order past its timeout gets cancelled and the leg marked abandoned, and (2)
an add-on leg whose parent core position already closed WITHOUT the real
lockstep close ever running is alerted loudly and left open, never
auto-closed at a guessed price.

Deliberately drives the harness as a SUBPROCESS, matching
test_fake_venue_harness_scenario.py's Phase 1 wrapper exactly (same isolation
rationale: the env vars must be set before any project import, which an
in-process test can't reproduce since pytest has already imported
signals_config/schwab_safety by then)."""
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

HARNESS = REPO_ROOT / "scripts" / "fake_venue_harness.py"
FIXED_PRICE = 100.0  # explicit override -> no network/yfinance dependency in the suite


@pytest.fixture(autouse=True)
def _restore_environ():
    """Same rationale as the Phase 1 wrapper's identical fixture --
    isolation.configure_env() writes straight to os.environ."""
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _run_harness(tmp_path, extra_args=()):
    import subprocess

    db_path = tmp_path / "fake_venue.db"
    state_dir = tmp_path / "state"
    proc = subprocess.run(
        [sys.executable, str(HARNESS), "--scenario", "addon_reconciliation",
         "--price", str(FIXED_PRICE), "--db-path", str(db_path),
         "--state-dir", str(state_dir), "--keep", *extra_args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600,
        env={**os.environ},
    )
    return proc, db_path


def test_addon_reconciliation_scenario_passes_all_checks(tmp_path):
    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout[-6000:]}\n{proc.stderr[-4000:]}"
    assert db_path.exists()
    assert "PASS —" in proc.stdout


def test_addon_reconciliation_json_report_is_self_consistent(tmp_path):
    proc, _ = _run_harness(tmp_path, extra_args=("--json",))
    assert proc.returncode == 0, proc.stdout[-6000:]
    payload = json.loads([ln for ln in proc.stdout.splitlines() if ln.startswith('{"passed"')][-1])
    assert payload['passed'] is True
    assert all(c['ok'] for c in payload['checks'] if c['required'])
    assert len(payload['proof_rows']) == 1
    row = payload['proof_rows'][0]
    # cycle A's timeout->cancel->abandoned, cycle B's orphaned-leg alert --
    # the orphaned leg deliberately stays open (never auto-closed).
    assert row['abandoned_events'] == 1
    assert row['orphaned_events'] == 1
    assert row['still_open_legs'] == 1
    assert row['closed_legs'] == 1
    assert payload['observations']['production_path_accesses'] == []
