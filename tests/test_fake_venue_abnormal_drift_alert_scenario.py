"""Fake-venue harness (Phase 2) -- durable check that the
`abnormal_drift_alert` scenario really does reproduce
signals_db.check_abnormal_drift's real chokepoint behavior (entry breach,
exit breach, 2/day escalation cap) deterministically against a real,
subprocess-isolated DB.

Deliberately drives the harness as a SUBPROCESS, matching
test_fake_venue_post_fill_topup_scenario.py's pattern exactly (same isolation
rationale: the env vars must be set before any project import, which an
in-process test can't reproduce since pytest has already imported
signals_config/schwab_safety by then)."""
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
    """Same rationale as the other Phase 2 wrappers' identical fixture --
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
        [sys.executable, str(HARNESS), "--scenario", "abnormal_drift_alert",
         "--price", str(FIXED_PRICE), "--db-path", str(db_path),
         "--state-dir", str(state_dir), "--keep", *extra_args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600,
        env={**os.environ},
    )
    return proc, db_path


def test_abnormal_drift_alert_scenario_passes_all_checks(tmp_path):
    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout[-6000:]}\n{proc.stderr[-4000:]}"
    assert db_path.exists()
    assert "PASS —" in proc.stdout


def test_abnormal_drift_alert_json_report_is_self_consistent(tmp_path):
    proc, _ = _run_harness(tmp_path, extra_args=("--json",))
    assert proc.returncode == 0, proc.stdout[-6000:]
    payload = json.loads([ln for ln in proc.stdout.splitlines() if ln.startswith('{"passed"')][-1])
    assert payload['passed'] is True
    assert all(c['ok'] for c in payload['checks'] if c['required'])
    assert len(payload['proof_rows']) == 3
    assert payload['observations']['production_path_accesses'] == []
    # 2 drift-alert posts (entry, exit) -- the 3rd (suppressed) breach must
    # not add a 3rd -- plus the unrelated real STOP LOSS placement message
    # leg 1's real broker fill also triggers (see the scenario's own
    # _drift_posts() filter for why that message is excluded from this count).
    assert payload['observations']['drift_posted_count'] == 2


def test_abnormal_drift_alert_escalation_cap_rows_are_correct_and_ordered(tmp_path):
    """Reads the real DB directly, not the harness's own report -- pins the
    exact 3-row shape (alerted/entry, alerted/exit, suppressed_daily_cap/entry)
    check_abnormal_drift's 2/day escalation cap must produce."""
    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout[-6000:]}\n{proc.stderr[-4000:]}"

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT result, detail, mode FROM coverage_events "
            "WHERE scenario_key='abnormal_drift_alert' AND ticker=? ORDER BY id ASC",
            (meta.TICKER,),
        ).fetchall()]
    finally:
        conn.close()

    assert len(rows) == 3, rows
    assert rows[0]['result'] == 'alerted' and 'side=entry' in rows[0]['detail']
    assert rows[1]['result'] == 'alerted' and 'side=exit' in rows[1]['detail']
    assert rows[2]['result'] == 'suppressed_daily_cap' and 'side=entry' in rows[2]['detail']
    assert all(r['mode'] == 'live' for r in rows)
