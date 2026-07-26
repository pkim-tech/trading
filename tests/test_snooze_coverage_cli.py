"""Tests for scripts/snooze_coverage.py -- the CLI wrapper around
signals_db.snooze_coverage/get_active_snoozes. Covers the ensure_tables()
gap found by session-wrap Opus review 2026-07-28 (the CLI never created
coverage_snoozes, so it would crash against a DB predating this feature)."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db
from scripts.snooze_coverage import main


@pytest.fixture
def isolated_db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    yield tmp_db.name
    os.unlink(tmp_db.name)


def test_snooze_against_fresh_db_does_not_crash(isolated_db, monkeypatch, capsys):
    """Regression: the CLI previously never called ensure_tables(), so this
    raised sqlite3.OperationalError: no such table: coverage_snoozes against
    any DB that hadn't already had the table created by another code path."""
    monkeypatch.setattr(sys, 'argv', ['snooze_coverage.py', 'reconciliation_mismatch', '30',
                                       'test reason', '--ticker', 'UDOW', '--kind', 'shares'])
    main()
    out = capsys.readouterr().out
    assert 'Snoozed reconciliation_mismatch' in out
    snoozes = db.get_active_snoozes('reconciliation_mismatch')
    assert len(snoozes) == 1
    assert snoozes[0]['ticker'] == 'UDOW'
    assert snoozes[0]['kind'] == 'shares'


def test_list_with_no_scenario_filter(isolated_db, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['snooze_coverage.py', 'sk_a', '1', 'reason a'])
    main()
    monkeypatch.setattr(sys, 'argv', ['snooze_coverage.py', 'sk_b', '1', 'reason b'])
    main()
    monkeypatch.setattr(sys, 'argv', ['snooze_coverage.py', '--list'])
    main()
    out = capsys.readouterr().out
    assert 'sk_a' in out and 'sk_b' in out


def test_list_filtered_to_one_scenario(isolated_db, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['snooze_coverage.py', 'sk_a', '1', 'reason a'])
    main()
    monkeypatch.setattr(sys, 'argv', ['snooze_coverage.py', 'sk_b', '1', 'reason b'])
    main()
    capsys.readouterr()  # discard the two "Snoozed ..." confirmation prints above
    monkeypatch.setattr(sys, 'argv', ['snooze_coverage.py', '--list', 'sk_a'])
    main()
    out = capsys.readouterr().out
    assert 'sk_a' in out and 'sk_b' not in out
