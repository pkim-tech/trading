"""Tests for signals_db.get_coverage_events' since_id pagination (2026-08-18)
-- built for check_intraday_risk_review's watermark gap: a plain limit=N
fetch has a fixed floor, so anything older than the newest N rows falls
below it every call, silently abandoned once a watermark jumps past it.
since_id pages forward (oldest-unseen-first) instead. Covers: exclusion at/
below the watermark, inclusion of everything above it, and correct
behavior when the true row count exceeds the safety-cap limit."""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db

TICKER = 'TEST_SINCE_ID_SCENARIO'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    signals_db.ensure_tables()
    yield
    Path(tmp_db.name).unlink(missing_ok=True)


def _log_n(n, result='ok'):
    for i in range(n):
        signals_db.log_coverage_event('since_id_test', 'live', ticker=TICKER, result=result,
                                       detail=f'event {i}')


def test_since_id_excludes_rows_at_or_below_watermark(env):
    _log_n(5)
    all_events = signals_db.get_coverage_events(scenario_key='since_id_test')
    ids = sorted(e['id'] for e in all_events)
    watermark = ids[1]  # second-oldest -- exclude it and everything before it
    fetched = signals_db.get_coverage_events(scenario_key='since_id_test', since_id=watermark)
    fetched_ids = sorted(e['id'] for e in fetched)
    assert fetched_ids == [i for i in ids if i > watermark]
    assert watermark not in fetched_ids


def test_since_id_includes_everything_above_watermark(env):
    _log_n(5)
    all_events = signals_db.get_coverage_events(scenario_key='since_id_test')
    ids = sorted(e['id'] for e in all_events)
    fetched = signals_db.get_coverage_events(scenario_key='since_id_test', since_id=0)
    assert sorted(e['id'] for e in fetched) == ids


def test_since_id_returns_ascending_order_oldest_first(env):
    _log_n(5)
    fetched = signals_db.get_coverage_events(scenario_key='since_id_test', since_id=0)
    ids = [e['id'] for e in fetched]
    assert ids == sorted(ids)


def test_since_id_respects_safety_cap_when_backlog_exceeds_limit(env):
    _log_n(10)
    all_events = signals_db.get_coverage_events(scenario_key='since_id_test')
    ids = sorted(e['id'] for e in all_events)
    fetched = signals_db.get_coverage_events(scenario_key='since_id_test', since_id=0, limit=3)
    fetched_ids = [e['id'] for e in fetched]
    # Capped to 3 rows, and -- because since_id orders ASC -- the OLDEST 3
    # unseen rows, not the newest (a DESC cap would strand the oldest ones
    # forever; ASC lets a caller page forward through the backlog instead).
    assert fetched_ids == ids[:3]


def test_without_since_id_behavior_is_unchanged_most_recent_n_desc(env):
    _log_n(5)
    all_events = signals_db.get_coverage_events(scenario_key='since_id_test')
    ids = sorted(e['id'] for e in all_events)
    fetched = signals_db.get_coverage_events(scenario_key='since_id_test', limit=2)
    fetched_ids = [e['id'] for e in fetched]
    # Existing (no since_id) behavior: most-recent-N, DESC.
    assert fetched_ids == list(reversed(ids[-2:]))
