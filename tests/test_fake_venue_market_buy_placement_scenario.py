"""Fake-venue harness (Phase 2) -- durable check that the `market_buy_
placement` scenario really does reproduce `_attempt_automated_market_buy` +
`_sync_confirm_and_protect` succeeding end to end on the ordinary, no-delay
path (see fake_venue/scenarios_market_buy_placement.py's module docstring
for the full gap writeup and how this differs from the closely-related
`sl_async_fallback`/`sl_sync_placement` siblings built the same night).

This file is real evidence for registry id 'market_buy_placement'
(scripts/coverage_registry.py) -- second, independent evidence for that row
alongside tests/test_fake_broker_entry_scenario.py's existing marker (that
one drives the same entrypoint via the pytest-fixture-based fake_broker;
this one drives it via the standalone fake-venue harness's stronger
isolation/proof-by-fresh-connection discipline). See that registry row's
check_mechanism='scenario_expectations' note for why this self-declared
marker convention exists instead of a get_coverage_events() assertion.

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
FIXED_PRICE = 62.75  # explicit override -> no network/yfinance dependency in the suite


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
        [sys.executable, str(HARNESS), "--scenario", "market_buy_placement",
         "--price", str(FIXED_PRICE), "--db-path", str(db_path),
         "--state-dir", str(state_dir), "--keep", *extra_args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600,
        env={**os.environ},
    )
    return proc, db_path


def test_market_buy_placement_scenario_passes_all_checks(tmp_path):
    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout[-6000:]}\n{proc.stderr[-4000:]}"
    assert db_path.exists()
    assert "PASS —" in proc.stdout


def test_market_buy_placement_json_report_is_self_consistent(tmp_path):
    proc, _ = _run_harness(tmp_path, extra_args=("--json",))
    assert proc.returncode == 0, proc.stdout[-6000:]
    payload = json.loads([ln for ln in proc.stdout.splitlines() if ln.startswith('{"passed"')][-1])
    assert payload['passed'] is True
    assert all(c['ok'] for c in payload['checks'] if c['required'])
    assert len(payload['proof_rows']) == 1
    assert payload['proof_rows'][0]['sl_placed_events'] == 1
    assert payload['proof_rows'][0]['async_fast_path_events'] == 0
    assert payload['proof_rows'][0]['sync_timeout_events'] == 0
    assert payload['proof_rows'][0]['sl_order_id'] is not None
    assert payload['observations']['production_path_accesses'] == []
