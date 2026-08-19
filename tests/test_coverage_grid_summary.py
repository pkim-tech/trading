"""Tests for scripts/coverage_grid_summary.py's --date guard.

Real gap found 2026-08-19: compute_status() (scripts/coverage_registry.py) has no
date parameter -- it always computes CURRENT/live status. compute_today() wrote
that live status under whatever snapshot_date --date supplied, so
`--date 2026-08-01` silently wrote TODAY's live status mislabeled as a
2026-08-01 snapshot. Fixed by refusing to proceed (loud error, non-zero exit)
whenever --date is not today, rather than quietly writing mislabeled data.
"""
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db
from scripts import coverage_grid_summary as cgs


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    # compute_today() -> compute_status() reads live state via
    # signals_db._conn(), which connects to signals_config.DB_PATH (a
    # separate module-level constant from coverage_grid_summary's own
    # DB_PATH, used only for the snapshot table) -- both must be isolated so
    # a test run never touches the real cache/live/trading_live.db.
    real_db = tmp_path / "signals_test.db"
    monkeypatch.setattr(signals_config, "DB_PATH", real_db)
    db.ensure_tables()

    snapshot_db = tmp_path / "coverage_grid_summary_test.db"
    monkeypatch.setattr(cgs, "DB_PATH", snapshot_db)
    return snapshot_db


def test_no_date_flag_computes_and_writes_today(isolated_db, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["coverage_grid_summary.py"])
    cgs.main()  # should not raise
    out = capsys.readouterr().out
    assert str(date.today()) in out

    import sqlite3
    conn = sqlite3.connect(isolated_db)
    rows = conn.execute(
        "SELECT snapshot_date FROM coverage_grid_snapshot"
    ).fetchall()
    conn.close()
    assert rows, "expected today's run to write snapshot rows"
    assert all(r[0] == str(date.today()) for r in rows)


def test_date_equal_to_today_still_works(isolated_db, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["coverage_grid_summary.py", "--date", str(date.today())])
    cgs.main()  # should not raise
    out = capsys.readouterr().out
    assert str(date.today()) in out


def test_past_date_refuses_to_write_mislabeled_snapshot(isolated_db, monkeypatch, capsys):
    past = str(date.today() - timedelta(days=5))
    monkeypatch.setattr(sys, "argv", ["coverage_grid_summary.py", "--date", past])

    with pytest.raises(SystemExit) as exc_info:
        cgs.main()

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "not today" in err
    assert "compute_status()" in err

    # No DB write should have happened at all -- the guard must fire before
    # ensure_table()/compute_today() ever touch the (isolated) DB file.
    assert not isolated_db.exists()


def test_future_date_also_refused(isolated_db, monkeypatch, capsys):
    future = str(date.today() + timedelta(days=5))
    monkeypatch.setattr(sys, "argv", ["coverage_grid_summary.py", "--date", future])

    with pytest.raises(SystemExit) as exc_info:
        cgs.main()

    assert exc_info.value.code != 0
    assert not isolated_db.exists()
