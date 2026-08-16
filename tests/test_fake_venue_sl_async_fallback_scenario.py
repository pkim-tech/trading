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
    # Every check is required=True now (2026-08-16 fix): check_auto_fills'
    # buy-side loop genuinely recovers a market-buy node's timed-out fill
    # (Leg 1.5, node_a), and the pre-existing drain_fill_queue stream path
    # still independently recovers a second node's timed-out fill (Leg 2,
    # node_b) -- no documented-not-fixed gap remains in this scenario.
    assert all(c['ok'] for c in payload['checks'] if c['required'])
    assert all(c['required'] for c in payload['checks'])
    # Two independent nodes/fills (node_a/CASH_ALIAS via check_auto_fills,
    # node_b/MARGIN_ALIAS via drain_fill_queue) -- see verify_proof's
    # docstring for exactly what distinguishes each row.
    assert len(payload['proof_rows']) == 2
    by_account = {r['account']: r for r in payload['proof_rows']}
    assert by_account['fv_cash']['sync_timeout_events'] == 1
    assert by_account['fv_cash']['async_confirm_events'] == 0
    assert by_account['fv_cash']['sl_placed_events'] == 1
    assert by_account['fv_cash']['sl_order_id'] is not None
    assert by_account['fv_margin']['sync_timeout_events'] == 1
    assert by_account['fv_margin']['async_confirm_events'] == 1
    assert by_account['fv_margin']['sl_placed_events'] == 1
    assert by_account['fv_margin']['sl_order_id'] is not None
    assert payload['observations']['production_path_accesses'] == []
