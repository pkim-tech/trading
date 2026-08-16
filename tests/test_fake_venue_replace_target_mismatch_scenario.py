"""Fake-venue harness (Phase 2) -- durable check that the
`replace_target_mismatch` scenario reproduces the real 2-broker-round-trip
verify-then-replace sequence end to end (docs/backlog_cache.md item 8, SOXS
2026-08-14 incident), including the TOCTOU-race leg (leg B) that no existing
unit test (tests/test_replace_target_mismatch.py, which mocks
schwab_safety._open_orders to a single fixed return value) can reach.

Deliberately drives the harness as a SUBPROCESS, matching every other Phase 2
wrapper's rationale: isolation.configure_env()'s env vars must be set before
any project import, which an in-process test can't reproduce since pytest has
already imported signals_config/schwab_safety by then."""
import json
import os
import shutil
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
    """Same rationale as every other Phase 2 wrapper's identical fixture --
    isolation.configure_env() writes straight to os.environ."""
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _run_harness(tmp_path, extra_args=(), repo_root=REPO_ROOT):
    db_path = tmp_path / "fake_venue.db"
    state_dir = tmp_path / "state"
    proc = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "fake_venue_harness.py"), "--scenario",
         "replace_target_mismatch", "--price", str(FIXED_PRICE), "--db-path", str(db_path),
         "--state-dir", str(state_dir), "--keep", *extra_args],
        cwd=str(repo_root), capture_output=True, text=True, timeout=600,
        env={**os.environ},
    )
    return proc, db_path


def test_replace_target_mismatch_scenario_passes_all_checks(tmp_path):
    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout[-8000:]}\n{proc.stderr[-4000:]}"
    assert db_path.exists()
    assert "PASS —" in proc.stdout


def test_replace_target_mismatch_json_report_is_self_consistent(tmp_path):
    proc, _ = _run_harness(tmp_path, extra_args=("--json",))
    assert proc.returncode == 0, proc.stdout[-8000:]
    payload = json.loads([ln for ln in proc.stdout.splitlines() if ln.startswith('{"passed"')][-1])
    assert payload['passed'] is True
    assert all(c['ok'] for c in payload['checks'] if c['required'])
    # Two watch_list rows (node A, node B) -- see the scenario's own PROOF_SQL.
    assert len(payload['proof_rows']) == 2
    assert payload['observations']['production_path_accesses'] == []


def test_replace_target_mismatch_asserts_node_a_detected_and_node_b_did_not(tmp_path):
    """Pinned regression for the specific asymmetry this scenario exists to
    prove: node A's mismatch is caught BEFORE round-trip 1 runs (2 real
    replace_target_mismatch events), while node B's identical-shaped mismatch
    lands in the genuine TOCTOU gap between round-trip 1 and round-trip 2 (0
    events) -- yet BOTH end up with the replace correctly blocked and no
    orphan order, proven by reading the harness DB directly rather than
    trusting the harness's own printed report."""
    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout[-8000:]}\n{proc.stderr[-4000:]}"

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT wl.id AS wl_id, wl.account, "
            "  (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='replace_target_mismatch' "
            "    AND node_id=wl.id) AS mismatch_events "
            "FROM watch_list wl WHERE wl.ticker=? ORDER BY wl.id", (meta.TICKER,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2, rows
    node_a, node_b = dict(rows[0]), dict(rows[1])
    assert node_a['account'] == meta.CASH_ALIAS and node_a['mismatch_events'] == 2, node_a
    assert node_b['account'] == meta.MARGIN_ALIAS and node_b['mismatch_events'] == 0, node_b


# ---------------------------------------------------------------------------
# Real check that removing the pre-replace advisory check (round-trip 1)
# entirely still leaves the scenario able to tell something changed --
# specifically, that leg A's detection (results captured while the check
# still ran) would go missing. Patches a temp copy of the repo, never the
# real project source.
# ---------------------------------------------------------------------------

def test_replace_target_mismatch_scenario_fails_if_the_advisory_check_is_removed(tmp_path):
    repo_copy = tmp_path / "repo_copy"
    shutil.copytree(REPO_ROOT, repo_copy, symlinks=True,
                     ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc",
                                                    ".venv", "cache", "logs", "output"))
    target = repo_copy / "signals_notify.py"
    text = target.read_text()
    needle = ('        try:\n'
              '            _verify_resting_before_replace(pos, node, account, ticker, resting_order_id,\n'
              '                                            resting_order_label)\n'
              '        except Exception as e:\n')
    assert needle in text, "_attempt_automated_exit_sell's pre-replace check call moved -- update this test"
    reverted = text.replace(
        needle,
        '        try:\n'
        '            pass  # advisory check removed for this test\n'
        '        except Exception as e:\n',
        1,
    )
    assert reverted != text
    target.write_text(reverted)

    proc, db_path = _run_harness(tmp_path, repo_root=repo_copy)
    assert proc.returncode != 0, (
        "expected the scenario to FAIL with leg A's advisory check disabled, but it passed:\n"
        f"{proc.stdout[-8000:]}\n{proc.stderr[-4000:]}"
    )
    assert "leg A" in proc.stdout
