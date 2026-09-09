"""Fake-venue harness (Phase 2) -- durable check that the
`addon_leg_independent_sl_fill_detection` scenario really does reproduce an
add-on leg's own protective stop filling independently at the broker
(zero involvement from any lockstep-exit/bar-close code) and being
detected/closed by the standalone `signals_notify.check_addon_leg_reconciliation`
poll -- one level down from the `sl_order_fills_independent_detection`/LABD
sibling above, for an add-on leg instead of the core position.

Added 2026-09-08 (paired-review MEDIUM finding, contextual review of the
addon_leg_sl_fill_detected scenario_key split): without this wrapper,
scripts/coverage_proof_matrix.py's offline_proof_for/fake_broker_proof_for
(which only scan tests/test_*.py, never fake_venue/ directly) can't see that
this scenario exists and passes -- the row's proof tier reads NONE despite
real fake_venue proof existing, same gap this file's sibling closes for its
own scenario.

Deliberately drives the harness as a SUBPROCESS, matching
test_fake_venue_sl_order_fills_independent_detection_scenario.py exactly
(same isolation rationale: the env vars must be set before any project
import, which an in-process test can't reproduce since pytest has already
imported signals_config/schwab_safety by then)."""
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

HARNESS = REPO_ROOT / "scripts" / "fake_venue_harness.py"
FIXED_PRICE = 187.87  # matches the scenario module's own default -- no network/yfinance dependency


@pytest.fixture(autouse=True)
def _restore_environ():
    """Same rationale as the sibling wrapper's identical fixture --
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
        [sys.executable, str(HARNESS), "--scenario", "addon_leg_independent_sl_fill_detection",
         "--price", str(FIXED_PRICE), "--db-path", str(db_path),
         "--state-dir", str(state_dir), "--keep", *extra_args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600,
        env={**os.environ},
    )
    return proc, db_path


def test_addon_leg_independent_sl_fill_detection_scenario_passes_all_checks(tmp_path):
    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout[-6000:]}\n{proc.stderr[-4000:]}"
    assert db_path.exists()
    assert "PASS —" in proc.stdout


def test_addon_leg_independent_sl_fill_detection_json_report_is_self_consistent(tmp_path):
    proc, _ = _run_harness(tmp_path, extra_args=("--json",))
    assert proc.returncode == 0, proc.stdout[-6000:]
    payload = json.loads([ln for ln in proc.stdout.splitlines() if ln.startswith('{"passed"')][-1])
    assert payload['passed'] is True
    assert all(c['ok'] for c in payload['checks'] if c['required'])
    assert payload['observations']['production_path_accesses'] == []


def test_addon_leg_independent_sl_fill_detection_logs_its_own_scenario_key(tmp_path, monkeypatch):
    """Explicit get_coverage_events(scenario_key=...) assertion against the harness's
    real isolated DB -- not just a harness pass/fail check -- so scripts/coverage_
    registry.py's offline_proof_for() scanner (which greps tests/test_*.py for this
    exact call pattern, never fake_venue/*.py) can credit this row with real proof.
    Confirmed necessary, 2026-09-08: the harness-wrapper tests above alone (matching
    the sl_order_fills_independent_detection sibling's own pattern) do NOT provide
    this credit -- that sibling's own scanner credit actually comes from a separate
    fake_broker test (test_fake_broker_sl_order_fills_scenario.py) asserting its
    scenario_key directly via signals_db.get_coverage_events, not from its
    fake_venue wrapper file. Reusing the real production get_coverage_events (via a
    DB_PATH monkeypatch onto the harness's already-populated isolated DB) rather
    than a raw SQL query, so this is a genuine assertion in the same idiom the
    scanner is built to recognize, not a query built just to satisfy the grep."""
    import signals_config
    import signals_db

    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, proc.stdout[-6000:]

    monkeypatch.setattr(signals_config, "DB_PATH", db_path)
    events = signals_db.get_coverage_events(scenario_key="addon_leg_sl_fill_detected")
    assert any(e["result"] == "sl_closed_reconcile" for e in events), (
        f"expected a 'sl_closed_reconcile' event under scenario_key="
        f"'addon_leg_sl_fill_detected', got: {[dict(e) for e in events]}")
