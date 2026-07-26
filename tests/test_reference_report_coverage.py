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

import signals_blocks
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
    monkeypatch.setattr(signals_notify, '_post_chunked', lambda *a, **kw: ("C123", "1234.5678"))
    signals_notify.send_reference_report([])
    events = signals_db.get_coverage_events(scenario_key="morning_report_delivery")
    assert len(events) == 1
    assert events[0]['result'] == "sent"


def test_send_reference_report_logs_no_delivery_confirmation_on_failed_post(env, monkeypatch):
    monkeypatch.setattr(signals_notify, '_post_chunked', lambda *a, **kw: (None, None))
    signals_notify.send_reference_report([])
    events = signals_db.get_coverage_events(scenario_key="morning_report_delivery")
    assert len(events) == 1
    assert events[0]['result'] == "no_delivery_confirmation"


def test_send_reference_report_actually_chunks_a_large_watchlist(env, monkeypatch):
    """Exercises the real build_reference_table -> _ticker_block -> _post_chunked
    path end-to-end against a synthetic 60-row watchlist -- the unit tests for
    _post_chunked itself only use synthetic block lists, so nothing previously
    proved real report rows chunk correctly at scale (Opus review, 2026-07-26)."""
    fake_rows = [
        {"Ticker": f"T{i}", "Version": "v5", "Held": False, "Next Action": "NO_DATA", "Strategy": "Test"}
        for i in range(60)
    ]
    monkeypatch.setattr(signals_notify, 'build_reference_table', lambda watchlist: fake_rows)

    calls = []
    def fake_post(text, blocks=None, thread_ts=None, reply_broadcast=False):
        calls.append(blocks)
        return (f"C{len(calls)}", f"{len(calls)}.0")
    monkeypatch.setattr(signals_blocks, '_post_message', fake_post)

    signals_notify.send_reference_report([])

    assert len(calls) > 1  # 60 rows must not fit in a single 50-block message
    assert all(len(blocks) <= 50 for blocks in calls)
    rendered_tickers = {b["text"]["text"].split("*")[1] for blocks in calls for b in blocks
                         if b.get("type") == "section" and "T" in b.get("text", {}).get("text", "")}
    assert rendered_tickers == {f"T{i}" for i in range(60)}
