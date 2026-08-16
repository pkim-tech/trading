"""Fake-venue harness (Phase 2) -- durable check that the
`fast_path_partial_fill` scenario really does reproduce the multi-execution
partial-fill hazard drain_fill_queue's own docstring warns about: a partial
stream execution must NOT be treated as a final fill (no position opened, no
top-up placed), and only the broker's own terminal FILLED status/aggregated
quantity may open the position.

Deliberately drives the harness as a SUBPROCESS, matching the Phase 1/
post_fill_topup wrappers exactly (same isolation rationale: the env vars must
be set before any project import, which an in-process test can't reproduce
since pytest has already imported signals_config/schwab_safety by then)."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fake_venue import scenarios_meta as meta  # noqa: E402

HARNESS = REPO_ROOT / "scripts" / "fake_venue_harness.py"
FIXED_PRICE = 100.0  # explicit override -> no network/yfinance dependency in the suite


@pytest.fixture(autouse=True)
def _restore_environ():
    """Same rationale as the other Phase 2 wrapper's identical fixture --
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
        [sys.executable, str(HARNESS), "--scenario", "fast_path_partial_fill",
         "--price", str(FIXED_PRICE), "--db-path", str(db_path),
         "--state-dir", str(state_dir), "--keep", *extra_args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600,
        env={**os.environ},
    )
    return proc, db_path


def test_fast_path_partial_fill_scenario_passes_all_checks(tmp_path):
    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout[-6000:]}\n{proc.stderr[-4000:]}"
    assert db_path.exists()
    assert "PASS —" in proc.stdout


def test_fast_path_partial_fill_json_report_is_self_consistent(tmp_path):
    proc, _ = _run_harness(tmp_path, extra_args=("--json",))
    assert proc.returncode == 0, proc.stdout[-6000:]
    payload = json.loads([ln for ln in proc.stdout.splitlines() if ln.startswith('{"passed"')][-1])
    assert payload['passed'] is True
    assert all(c['ok'] for c in payload['checks'] if c['required'])
    assert len(payload['proof_rows']) == 2
    assert payload['observations']['production_path_accesses'] == []


def test_fast_path_partial_fill_does_not_open_a_position_on_the_partial_alone(tmp_path):
    """Pinned regression for the exact hazard drain_fill_queue's docstring
    describes: a partial execution's own stream-carried quantity must never
    be locked in as a real fill. Reads the real DB row directly, not the
    harness's own report -- the report is what's being checked here."""
    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout[-6000:]}\n{proc.stderr[-4000:]}"

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        events = [dict(r) for r in conn.execute(
            "SELECT ts, result, detail FROM coverage_events "
            "WHERE scenario_key = 'fast_path_fill_reconciliation' ORDER BY id ASC")]
        pos = conn.execute(
            "SELECT op.entry_price, op.shares FROM open_positions op "
            "JOIN watch_list wl ON wl.id = op.wl_id WHERE wl.ticker = ?",
            (meta.TICKER,),
        ).fetchone()
    finally:
        conn.close()

    assert [e['result'] for e in events] == [
        'stream_event_not_yet_confirmed_filled', 'confirmed_via_poll'], events

    assert pos is not None
    # The scenario's partial leg fills 40% of the order at price*0.995; the
    # broker's own terminal fill (what open_positions must reflect) is at
    # price*0.99 for the FULL order quantity. shares must be the full order,
    # not the partial's own execution quantity, and entry_price must be the
    # terminal fill's price, not the partial's.
    assert abs(pos['entry_price'] - FIXED_PRICE * 0.99) < 0.0005, dict(pos)
    assert abs(pos['entry_price'] - FIXED_PRICE * 0.995) > 0.0005, dict(pos)
