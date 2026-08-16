"""Fake-venue harness (Phase 2) -- durable check that the `drought_handoff`
scenario reproduces the real cancel/replace chain `signals_notify.
check_drought_handoff` drives end to end, covering 2 Grid rows
(`drought_handoff_cancel`, `drought_handoff_exit_placement`), including the
explicitly-requested fill-race (node B) and the unconfirmed-fill window
(node C) that no fake_broker-tier test chains into `check_own_sell_fills`.

Deliberately drives the harness as a SUBPROCESS, matching every other Phase 2
wrapper's rationale: isolation.configure_env()'s env vars must be set before
any project import, which an in-process test can't reproduce since pytest has
already imported signals_config/schwab_safety by then.

Node C's leg used to intentionally surface a REAL production gap (see
fake_venue/scenarios_drought_handoff.py's module docstring's "FIXED" note,
formerly "FOUND, NOT FIXED": signals_notify.check_drought_handoff's
placed_unconfirmed exit_pending write omitted 'current_price', which
check_own_sell_fills unconditionally reads). That gap is now fixed
(paired-reviewed) and the scenario module's Check flipped to `required=True`.
test_drought_handoff_known_gap_is_visible_not_hidden below is the pinned
regression for that fact -- updated to assert the fix holds (required=True,
ok=True) instead of pinning the old broken behavior."""
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
         "drought_handoff", "--price", str(FIXED_PRICE), "--db-path", str(db_path),
         "--state-dir", str(state_dir), "--keep", *extra_args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600,
        env={**os.environ},
    )
    return proc, db_path


def test_drought_handoff_scenario_passes_all_required_checks(tmp_path):
    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout[-8000:]}\n{proc.stderr[-4000:]}"
    assert db_path.exists()
    assert "PASS —" in proc.stdout


def test_drought_handoff_json_report_is_self_consistent(tmp_path):
    proc, _ = _run_harness(tmp_path, extra_args=("--json",))
    assert proc.returncode == 0, proc.stdout[-8000:]
    payload = json.loads([ln for ln in proc.stdout.splitlines() if ln.startswith('{"passed"')][-1])
    assert payload['passed'] is True
    assert all(c['ok'] for c in payload['checks'] if c['required'])
    # Three watch_list rows (node A, B, C) -- see the scenario's own PROOF_SQL.
    assert len(payload['proof_rows']) == 3
    assert payload['observations']['production_path_accesses'] == []


def test_drought_handoff_cancel_and_fill_race_proven_directly(tmp_path):
    """Pinned regression, read straight from the harness DB rather than
    trusting the printed report: node A's clean cancel, node B's fill-race
    (the sub-case explicitly requested -- the cancel_order round-trip itself
    reports FILLED), and node C's unconfirmed-fill persistence all leave
    real, distinct coverage_events rows behind."""
    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout[-8000:]}\n{proc.stderr[-4000:]}"

    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT wl.id AS wl_id, wl.account, "
            "  (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='drought_handoff_cancel' "
            "    AND result='cancelled_resting_entry' AND node_id=wl.id) AS clean_cancels, "
            "  (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='drought_handoff_cancel' "
            "    AND result='raced_fill' AND node_id=wl.id) AS raced_fills, "
            "  (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='drought_handoff_exit_placement' "
            "    AND result='placed_unconfirmed' AND node_id=wl.id) AS unconfirmed "
            "FROM watch_list wl WHERE wl.ticker=? ORDER BY wl.id", (meta.TICKER,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 3, rows
    node_a, node_b, node_c = (dict(r) for r in rows)
    assert node_a['clean_cancels'] == 1, node_a
    assert node_b['raced_fills'] == 1, node_b
    assert node_c['unconfirmed'] == 1, node_c


def test_drought_handoff_known_gap_is_visible_not_hidden(tmp_path):
    """Pinned regression for the real gap this scenario's first run found
    (see the scenario module's "FIXED" docstring note, formerly "FOUND, NOT
    FIXED"): signals_notify.check_drought_handoff's placed_unconfirmed
    exit_pending write (~line 2237) used to omit 'current_price', so the real
    standalone poll check_own_sell_fills (~line 3236, which unconditionally
    reads exit_pending['current_price']) raised KeyError trying to close a
    HANDOFF position whose fill confirmation didn't land inline.

    Now that the fix has landed (paired-reviewed) and the scenario's own
    Check is `required=True`, this test asserts the FIXED behavior holds:
    the Check is required and passes. If this ever regresses (required flips
    back to False, or ok flips to False), that's a real reintroduction of
    the KeyError bug, not a scenario artifact."""
    proc, _ = _run_harness(tmp_path, extra_args=("--json",))
    assert proc.returncode == 0, proc.stdout[-8000:]
    payload = json.loads([ln for ln in proc.stdout.splitlines() if ln.startswith('{"passed"')][-1])
    gap_checks = [c for c in payload['checks'] if 'check_own_sell_fills closes the HANDOFF position' in c['name']]
    assert len(gap_checks) == 1, payload['checks']
    assert gap_checks[0]['required'] is True, "the fix landed -- this should now be real, required regression coverage"
    assert gap_checks[0]['ok'] is True, (
        "check_own_sell_fills failed to close the HANDOFF position cleanly -- the "
        "exit_pending['current_price'] fix in signals_notify.check_drought_handoff may have regressed"
    )
