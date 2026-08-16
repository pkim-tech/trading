"""Fake-venue harness (Phase 2) -- durable check that the `addon_lifecycle`
scenario really does reproduce the margin add-on-at-arm leg's real entry
(double-buy exemption + real MARKET BUY placement, despite the parent's own
resting protective SELL) and real lockstep exit (a still-resting unfilled
leg cancelled, never sold) end-to-end via the real production call chain
(signals_notify.notify_trailing_activated -> check_addon_trigger_real ->
schwab_safety.check_order's is_addon_leg exemption -> schwab_client.
place_equity_buy, and signals_notify.close_addon_leg_real_if_open).

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
        [sys.executable, str(HARNESS), "--scenario", "addon_lifecycle",
         "--price", str(FIXED_PRICE), "--db-path", str(db_path),
         "--state-dir", str(state_dir), "--keep", *extra_args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600,
        env={**os.environ},
    )
    return proc, db_path


def test_addon_lifecycle_scenario_passes_all_checks(tmp_path):
    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout[-6000:]}\n{proc.stderr[-4000:]}"
    assert db_path.exists()
    assert "PASS —" in proc.stdout


def test_addon_lifecycle_json_report_is_self_consistent(tmp_path):
    proc, _ = _run_harness(tmp_path, extra_args=("--json",))
    assert proc.returncode == 0, proc.stdout[-6000:]
    payload = json.loads([ln for ln in proc.stdout.splitlines() if ln.startswith('{"passed"')][-1])
    assert payload['passed'] is True
    assert all(c['ok'] for c in payload['checks'] if c['required'])
    assert len(payload['proof_rows']) == 1
    row = payload['proof_rows'][0]
    # exactly the shape the scenario's two cycles produce: one real
    # double-buy exemption, one real entry placement, one real
    # cancel-unfilled-leg exit, zero legs left open, both legs closed.
    assert row['exemption_events'] == 1
    assert row['entry_placement_events'] == 1
    assert row['exit_cancel_events'] == 1
    assert row['still_open_legs'] == 0
    assert row['closed_legs'] == 2
    assert payload['observations']['production_path_accesses'] == []
