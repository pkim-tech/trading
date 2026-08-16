"""Fake-venue harness (Phase 2) -- durable check that the `sl_async_fallback`
scenario really does reproduce the async-fallback SL-placement path end to
end (a synchronous fast-confirm timeout on a fresh automated market-buy,
followed by the real `drain_fill_queue` stream fast path picking up the same
fill and placing the genuine protective stop that the sync path never got
to).

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
FIXED_PRICE = 199.5  # explicit override -> no network/yfinance dependency in the suite


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
        [sys.executable, str(HARNESS), "--scenario", "sl_async_fallback",
         "--price", str(FIXED_PRICE), "--db-path", str(db_path),
         "--state-dir", str(state_dir), "--keep", *extra_args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600,
        env={**os.environ},
    )
    return proc, db_path


def test_sl_async_fallback_scenario_passes_all_checks(tmp_path):
    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout[-6000:]}\n{proc.stderr[-4000:]}"
    assert db_path.exists()
    assert "PASS —" in proc.stdout


def test_sl_async_fallback_json_report_is_self_consistent(tmp_path):
    proc, _ = _run_harness(tmp_path, extra_args=("--json",))
    assert proc.returncode == 0, proc.stdout[-6000:]
    payload = json.loads([ln for ln in proc.stdout.splitlines() if ln.startswith('{"passed"')][-1])
    assert payload['passed'] is True
    assert all(c['ok'] for c in payload['checks'] if c['required'])
    # The one required=False Check (the documented, unfixed check_auto_fills
    # order_placed gap -- see the scenario module's docstring) must still be
    # PRESENT and OK -- ok=False here would mean the gap somehow stopped
    # reproducing, which is itself worth knowing, not something to hide by
    # filtering it out of this assertion.
    notes = [c for c in payload['checks'] if not c['required']]
    assert len(notes) == 1 and notes[0]['ok'] is True
    assert len(payload['proof_rows']) == 1
    assert payload['proof_rows'][0]['sync_timeout_events'] == 1
    assert payload['proof_rows'][0]['async_confirm_events'] == 1
    assert payload['proof_rows'][0]['sl_placed_events'] == 1
    assert payload['proof_rows'][0]['sl_order_id'] is not None
    assert payload['observations']['production_path_accesses'] == []
