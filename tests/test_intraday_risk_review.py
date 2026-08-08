"""Tests for signals_notify.check_intraday_risk_review -- built 2026-08-08 as
the direct consequence of muting routine/anomaly Slack alerts for
sub-capital-at-stake nodes: those events are still fully logged to
trading_incidents/coverage_events, this is what reviews them. Isolated DB,
no real Slack posts, isolated state file (tmp_path)."""
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_blocks
import signals_config
import signals_db
import signals_notify


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'INTRADAY_RISK_REVIEW_STATE_PATH', tmp_path / "state.json")
    monkeypatch.setattr(signals_notify, '_coverage_is_trading_day', lambda date_str: True)
    signals_db.ensure_tables()
    yield
    Path(tmp_db.name).unlink()


TRADING_HOURS_NOON = datetime(2026, 8, 10, 12, 0, 0)  # a Monday, well inside the window


def _posted(monkeypatch):
    calls = []
    monkeypatch.setattr(signals_blocks, '_post_message', lambda *a, **kw: calls.append(a) or ("C1", "1.0"))
    monkeypatch.setattr(signals_notify, '_post_message', signals_blocks._post_message)
    return calls


def test_first_run_bootstraps_silently_even_with_existing_incidents(env, monkeypatch):
    # A missing state file must not dump pre-existing (possibly stale/
    # resolved) incidents as "new" -- it seeds the watermark to the current
    # max and stays silent.
    signals_db.log_incident("Pre-existing incident", "detail", ticker="AGQ")
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert posted == []


def test_no_new_incidents_stays_silent_after_bootstrap(env, monkeypatch):
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # nothing new
    assert posted == []


def test_new_incident_after_bootstrap_triggers_one_alert(env, monkeypatch):
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    signals_db.log_incident("Real incident", "something bad happened", ticker="SOXL",
                             account="soxl_ira", real_money_impact=True)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1
    assert "Real incident" in posted[0][0]
    assert "SOXL" in posted[0][0]


def test_already_alerted_incident_does_not_re_fire(env, monkeypatch):
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    signals_db.log_incident("Real incident", "detail")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1  # not re-posted


def test_failed_post_does_not_advance_watermark(env, monkeypatch):
    monkeypatch.setattr(signals_blocks, '_post_message', lambda *a, **kw: (None, None))
    monkeypatch.setattr(signals_notify, '_post_message', signals_blocks._post_message)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    signals_db.log_incident("Missed incident", "detail")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    state = json.loads(signals_config.INTRADAY_RISK_REVIEW_STATE_PATH.read_text())
    assert state['last_seen_incident_id'] == 0  # unchanged -- the post never confirmed


def test_concerning_coverage_event_triggers_alert(env, monkeypatch):
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    signals_db.log_coverage_event("sl_placement", "live", ticker="SOXL", result="failed_unexpectedly",
                                   detail="broker rejected the order")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1
    assert "failed_unexpectedly" in posted[0][0]


def test_routine_blocked_coverage_event_does_not_trigger_alert(env, monkeypatch):
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    signals_db.log_coverage_event("same_day_block", "live", ticker="SOXL", result="blocked_same_ticker",
                                   detail="a working guard, not a failure")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert posted == []


def test_outside_window_is_a_no_op(env, monkeypatch):
    posted = _posted(monkeypatch)
    signals_db.log_incident("Overnight incident", "detail")
    signals_notify.check_intraday_risk_review(now=datetime(2026, 8, 10, 20, 0, 0))
    assert posted == []


def test_non_trading_day_is_a_no_op(env, monkeypatch):
    monkeypatch.setattr(signals_notify, '_coverage_is_trading_day', lambda date_str: False)
    posted = _posted(monkeypatch)
    signals_db.log_incident("Weekend incident", "detail")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert posted == []
