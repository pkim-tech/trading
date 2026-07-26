"""Test for the Morning Report delivery coverage event (signals_notify.
send_reference_report) -- added 2026-07-27 evening after the user flagged the
accountability grid had no scenario at all for whether the report actually
posts to Slack, distinct from whether it gets built correctly (it silently
posted with zero candidate rows for weeks, 2026-07-23, with nothing tracking
delivery). Isolated DB, no real Slack posts."""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import signals_notify


@pytest.fixture
def env(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    signals_db.ensure_tables()
    yield
    Path(tmp_db.name).unlink()


def test_send_reference_report_logs_sent_on_successful_post(env, monkeypatch):
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: ("C123", "1234.5678"))
    signals_notify.send_reference_report([])
    events = signals_db.get_coverage_events(scenario_key="morning_report_delivery")
    assert len(events) == 1
    assert events[0]['result'] == "sent"


def test_send_reference_report_logs_no_delivery_confirmation_on_failed_post(env, monkeypatch):
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))
    signals_notify.send_reference_report([])
    events = signals_db.get_coverage_events(scenario_key="morning_report_delivery")
    assert len(events) == 1
    assert events[0]['result'] == "no_delivery_confirmation"
