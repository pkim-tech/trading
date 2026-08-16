"""Fake-venue harness (Phase 2) -- durable check that the `post_fill_topup`
scenario really does reproduce the top-up mechanism end-to-end, including the
entry_price-blending fix (signals_notify._reconcile_fill, 2026-08-16) --
built alongside a paired-Opus review finding that the scenario itself never
asserted entry_price anywhere, so it kept passing 17/17 even with that fix
reverted.

Deliberately drives the harness as a SUBPROCESS, matching
test_fake_venue_harness_scenario.py's Phase 1 wrapper exactly (same isolation
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
    """Same rationale as the Phase 1 wrapper's identical fixture --
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
        [sys.executable, str(HARNESS), "--scenario", "post_fill_topup",
         "--price", str(FIXED_PRICE), "--db-path", str(db_path),
         "--state-dir", str(state_dir), "--keep", *extra_args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600,
        env={**os.environ},
    )
    return proc, db_path


def test_post_fill_topup_scenario_passes_all_checks(tmp_path):
    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout[-6000:]}\n{proc.stderr[-4000:]}"
    assert db_path.exists()
    assert "PASS —" in proc.stdout


def test_post_fill_topup_json_report_is_self_consistent(tmp_path):
    proc, _ = _run_harness(tmp_path, extra_args=("--json",))
    assert proc.returncode == 0, proc.stdout[-6000:]
    payload = json.loads([ln for ln in proc.stdout.splitlines() if ln.startswith('{"passed"')][-1])
    assert payload['passed'] is True
    assert all(c['ok'] for c in payload['checks'] if c['required'])
    assert len(payload['proof_rows']) == 1
    assert payload['observations']['production_path_accesses'] == []


def test_post_fill_topup_asserts_blended_entry_price(tmp_path):
    """Pinned regression for the real defect this scenario's first run found
    (signals_notify._reconcile_fill recording the top-up leg's entry_price
    using the ORIGINAL fill_price, not the top-up order's own confirmed fill
    price) -- and for the review finding that the scenario itself never
    actually asserted on entry_price. Reads the real DB row directly, not the
    harness's own report."""
    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout[-6000:]}\n{proc.stderr[-4000:]}"

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        pos = conn.execute(
            "SELECT op.entry_price, op.shares FROM open_positions op "
            "JOIN watch_list wl ON wl.id = op.wl_id WHERE wl.ticker = ?",
            (meta.TICKER,),
        ).fetchone()
    finally:
        conn.close()
    assert pos is not None
    # The scenario force-fills the original leg at price*0.99 and the top-up
    # fills at the seeded quote (price) -- entry_price must land strictly
    # between the two (a real share-weighted blend), not equal either alone.
    assert FIXED_PRICE * 0.99 < pos['entry_price'] < FIXED_PRICE, dict(pos)


# ---------------------------------------------------------------------------
# Real check that reverting the entry_price fix in _reconcile_fill makes the
# scenario FAIL -- proving the new check above actually catches the bug it
# was built for, not just that it passes when the fix is in place (findings
# #1, paired Opus review of commit 908a6f0). Patches the source on disk in a
# temp copy of the repo rather than the real signals_notify.py -- this test
# must never mutate real project source, even transiently.
# ---------------------------------------------------------------------------

def test_post_fill_topup_scenario_fails_if_entry_price_fix_is_reverted(tmp_path):
    import shutil

    repo_copy = tmp_path / "repo_copy"
    shutil.copytree(REPO_ROOT, repo_copy, symlinks=True,
                     ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc",
                                                    ".venv", "cache", "logs", "output"))
    target = repo_copy / "signals_notify.py"
    text = target.read_text()
    needle = "if top_up_order_id is not None:  # None for dry_run -- nothing to poll"
    assert needle in text, "signals_notify._reconcile_fill's poll-gate line moved -- update this test"
    reverted = text.replace(needle, "if False:", 1)
    assert reverted != text
    target.write_text(reverted)

    db_path = tmp_path / "fv_reverted.db"
    state_dir = tmp_path / "state_reverted"
    proc = subprocess.run(
        [sys.executable, str(repo_copy / "scripts" / "fake_venue_harness.py"),
         "--scenario", "post_fill_topup", "--price", str(FIXED_PRICE),
         "--db-path", str(db_path), "--state-dir", str(state_dir), "--keep"],
        cwd=str(repo_copy), capture_output=True, text=True, timeout=600,
        env={**os.environ},
    )
    assert proc.returncode != 0, (
        "expected the scenario to FAIL with the entry_price fix reverted, but it passed:\n"
        f"{proc.stdout[-6000:]}\n{proc.stderr[-4000:]}"
    )
    assert "entry_price" in proc.stdout
