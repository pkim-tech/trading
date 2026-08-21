"""Regression test for scripts/coverage_ticket_table.py's check_timing_discrepancies()
UTC/ET fix (2026-08-21) -- the coverage_events branch used to compare a raw UTC
timestamp string directly against a 'localtime'-converted DATE string (the node's
watch_list.added_at), which is a silent false negative: a same-day full timestamp
string is never lexicographically less than the date-only string that's its own
prefix, so an event that really happened BEFORE node creation in ET terms could
still read as same-day-or-later and pass the check undetected. Fixed by comparing
the two raw UTC timestamps directly (both coverage_events.ts and watch_list.added_at
are full-precision UTC -- no 'localtime' conversion needed or wanted for this branch,
since it's day-level date(ts,'localtime') that caused the bug in the first place, and
converting both sides by the same offset can't change which one is earlier). See
docs/deep_backlog.md's 2026-08-21 entries for the full incident/fix writeup."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db
from scripts.coverage_ticket_table import check_timing_discrepancies

TICKER = 'TEST_TIMING_TICKET'
_TMP_DIR = '/dev/shm' if os.path.isdir('/dev/shm') else None


@pytest.fixture(scope='session')
def _schema_template_db():
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False, dir=_TMP_DIR)
    tmp_db.close()
    orig_db_path = signals_config.DB_PATH
    signals_config.DB_PATH = Path(tmp_db.name)
    try:
        db.ensure_tables()
    finally:
        signals_config.DB_PATH = orig_db_path
    yield tmp_db.name
    os.unlink(tmp_db.name)


@pytest.fixture
def isolated_db(monkeypatch, _schema_template_db):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False, dir=_TMP_DIR)
    tmp_db.close()
    shutil.copy2(_schema_template_db, tmp_db.name)
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    yield db
    os.unlink(tmp_db.name)


def _make_node(isolated_db, added_at_utc):
    """Creates a real node via add_node, then overwrites added_at directly to a
    controlled UTC timestamp string -- add_node itself only ever writes the SQL
    default (now), so this is the only way to pin a specific creation instant."""
    isolated_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'canary', window=5,
                          take_profit=0.1, stop_loss=0, max_hold_hours=48,
                          fixed_sl_override=15)
    node = [x for x in isolated_db.get_watchlist() if x['ticker'] == TICKER][0]
    with isolated_db._conn() as c:
        c.execute("UPDATE watch_list SET added_at = ? WHERE id = ?", (added_at_utc, node['id']))
        c.commit()
    return node['id']


def _insert_coverage_event(isolated_db, node_id, ts_utc):
    with isolated_db._conn() as c:
        c.execute("""
            INSERT INTO coverage_events (ts, scenario_key, mode, node_id, result, detail)
            VALUES (?, 'test_scenario', 'paper', ?, '', '')
        """, (ts_utc, node_id))
        c.commit()


def test_event_predating_node_creation_in_et_is_flagged_despite_same_utc_calendar_date(isolated_db):
    """Node created 2026-08-21 00:30 ET == 2026-08-21 04:30:00 UTC (EDT, +4h).
    Event really fired 2026-08-20 23:50 ET (40 min BEFORE node creation) ==
    2026-08-21 03:50:00 UTC -- same UTC calendar date as the node's creation, which
    is exactly the shape the old raw-full-ts-vs-localtime-date comparison could never
    catch (the ts string shares created's date as its own prefix, so it can never
    compare "less than" a date-only string). The fixed raw-UTC-vs-raw-UTC comparison
    correctly resolves '2026-08-21 03:50:00' < '2026-08-21 04:30:00' -- no timezone
    conversion needed, since the event genuinely happened first regardless of which
    calendar day either falls on."""
    node_id = _make_node(isolated_db, '2026-08-21 04:30:00')
    _insert_coverage_event(isolated_db, node_id, '2026-08-21 03:50:00')

    unexplained, historical = check_timing_discrepancies()

    flagged = [e for e in unexplained if e[0] == 'coverage_events' and e[2] == node_id]
    assert flagged, (
        "event genuinely predating node creation should be flagged as an "
        "unexplained timing discrepancy, even though its raw UTC ts shares the "
        "node's UTC calendar date"
    )
    assert not historical


def test_event_hours_before_creation_same_et_calendar_day_is_flagged(isolated_db):
    """Precision regression: an interim version of this fix (2026-08-21, superseded
    within the same session) compared date(ts,'localtime') against
    date(added_at,'localtime') -- correct in direction but day-granularity, which
    would MISS an event logged genuinely hours before its node existed if both fall
    on the same ET calendar day. Node created 2026-08-21 14:00 ET == 18:00:00 UTC;
    event logged 2026-08-21 09:00 ET == 13:00:00 UTC -- same ET calendar day
    (2026-08-21) as the node's creation, but 5 hours earlier. Only a full-timestamp
    comparison (the shipped fix) catches this."""
    node_id = _make_node(isolated_db, '2026-08-21 18:00:00')
    _insert_coverage_event(isolated_db, node_id, '2026-08-21 13:00:00')

    unexplained, historical = check_timing_discrepancies()

    flagged = [e for e in unexplained if e[0] == 'coverage_events' and e[2] == node_id]
    assert flagged, (
        "event hours before node creation, on the same ET calendar day, should "
        "still be flagged -- a date-only comparison would miss this"
    )
    assert not historical


def test_event_after_node_creation_same_utc_date_not_flagged(isolated_db):
    """Control case: an event genuinely AFTER node creation (both in ET and UTC
    terms) must not be flagged -- confirms the fix doesn't just flag everything
    sharing a UTC calendar date."""
    node_id = _make_node(isolated_db, '2026-08-21 04:30:00')
    _insert_coverage_event(isolated_db, node_id, '2026-08-21 05:00:00')

    unexplained, historical = check_timing_discrepancies()

    flagged = [e for e in unexplained if e[0] == 'coverage_events' and e[2] == node_id]
    assert not flagged
    assert not historical
