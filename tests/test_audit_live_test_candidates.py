"""Regression test for the status_check.py/audit_live_test_candidates.py blind
spot found 2026-08-04: audit_one only ever checked open_positions, so a node
mid-entry (real signal fired, trailing-buy order resting, no fill yet) rendered
as flat/no-activity -- the same "state" ground truth that get_pending_buy_by_wl_id
fixes for coverage_check.py's carryover scenario."""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db
from scripts.audit_live_test_candidates import audit_one

TICKER = 'TEST_AUDIT_PENDING'


@pytest.fixture
def isolated_db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    db.ensure_tables()
    yield db
    os.unlink(tmp_db.name)


def test_audit_one_reports_pending_buy_instead_of_flat(isolated_db, capsys):
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'canary', window=5, take_profit=0.1,
                stop_loss=0, max_hold_hours=48)
    node = [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]
    sig = dict(current_price=100.0, last_bar=datetime.now())
    db.add_pending_buy(node, sig, channel='C123', ts='123.456')

    audit_one(TICKER)

    out = capsys.readouterr().out
    assert 'pending buy resting' in out
    assert 'entry in progress' in out
    assert 'none (flat)\n' not in out
