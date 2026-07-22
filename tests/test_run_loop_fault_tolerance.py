"""Tests for active_signals._guarded, the per-section fault-isolation helper
that lets run_loop survive an exception in any one loop-body section
(automation_principles.md #3/#4). No real Schwab/Slack calls -- _post_message
is stubbed, no daemon loop is actually started."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import active_signals


@pytest.fixture(autouse=True)
def _reset_alert_cooldown(monkeypatch):
    # Each test gets a clean cooldown state -- otherwise test order could
    # suppress an alert that a given test expects to see.
    monkeypatch.setattr(active_signals, '_LAST_SECTION_ALERT', {})


def test_guarded_returns_result_on_success():
    result = active_signals._guarded("ok_section", lambda x: x * 2, 5)
    assert result == 10


def test_guarded_catches_exception_and_returns_none(monkeypatch):
    monkeypatch.setattr(active_signals, '_post_message', lambda *a, **kw: None)

    def _raise():
        raise RuntimeError("boom")

    result = active_signals._guarded("failing_section", _raise)
    assert result is None  # swallowed, not propagated


def test_guarded_posts_slack_alert_on_failure(monkeypatch):
    posted = []
    monkeypatch.setattr(active_signals, '_post_message', lambda msg: posted.append(msg))

    def _raise():
        raise RuntimeError("boom")

    active_signals._guarded("failing_section", _raise)
    assert len(posted) == 1
    assert "failing_section" in posted[0]
    assert "boom" in posted[0]


def test_guarded_suppresses_repeat_alerts_within_cooldown(monkeypatch):
    posted = []
    monkeypatch.setattr(active_signals, '_post_message', lambda msg: posted.append(msg))

    def _raise():
        raise RuntimeError("boom")

    active_signals._guarded("flaky_section", _raise)
    active_signals._guarded("flaky_section", _raise)
    active_signals._guarded("flaky_section", _raise)
    assert len(posted) == 1  # repeat failures within the cooldown window don't spam Slack


def test_guarded_alert_failure_does_not_raise(monkeypatch):
    # A Slack posting failure on top of the original exception must not
    # itself propagate -- that would defeat the whole point of _guarded.
    def _post_raises(msg):
        raise ConnectionError("slack is down too")
    monkeypatch.setattr(active_signals, '_post_message', _post_raises)

    def _raise():
        raise RuntimeError("boom")

    result = active_signals._guarded("doubly_failing_section", _raise)
    assert result is None


def test_guarded_different_sections_each_get_their_own_alert(monkeypatch):
    posted = []
    monkeypatch.setattr(active_signals, '_post_message', lambda msg: posted.append(msg))

    def _raise():
        raise RuntimeError("boom")

    active_signals._guarded("section_a", _raise)
    active_signals._guarded("section_b", _raise)
    assert len(posted) == 2  # different sections don't share a cooldown bucket
