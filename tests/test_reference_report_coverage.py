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
import signals_compute
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


def test_build_reference_table_shows_a_held_paper_position(env, monkeypatch):
    # Real 2026-07-28 bug: build_reference_table only read open_positions/
    # pending_buys (real+dry_run), so a research-mode node with a real open
    # paper position (SOXL, 463 shares) rendered as flat/all-grey Phase
    # bubbles -- the report claimed nothing was happening while a real paper
    # trade sat open for hours. Fixed by merging in get_open_positions(paper=
    # True)/get_paper_pending_buys(). No test previously created a paper
    # position and checked the resulting row, which is exactly why this went
    # unnoticed for days (test_paper_trading.py tests the fill logic in
    # isolation; this file tested report delivery, never their intersection).
    monkeypatch.setattr(signals_compute, '_current_price', lambda ticker: (100.0, None))
    monkeypatch.setattr(signals_compute, 'compute_buy_signal', lambda node: {
        'ticker': node['ticker'], 'current_price': 100.0, 'z_score': -2.5,
        'lower_band': 95.0, 'prev_close': 98.0, 'last_daily_bar': '2026-07-28',
    })
    signals_db.add_node(
        ticker='PAPERTEST', strategy='TrailingBothZScoreBreakout', version='v5', window=10,
        take_profit=30.0, stop_loss=2, max_hold_hours=70, state='paper', account='ira',
        trail_buy_pct=3.0, trail_pct=1.0,
    )
    node = signals_db.get_watch_list_node(ticker='PAPERTEST')
    opened = signals_db.open_position(
        node, signal_price=105.0, signal_time='2026-07-27 14:30:00',
        entry_price=100.0, entry_time='2026-07-28 11:00:00', shares=100, paper=True,
    )
    assert opened

    rows = signals_notify.build_reference_table([node])
    assert len(rows) == 1
    assert rows[0]['Held'] is True
    assert rows[0]['Phase'] != '⚪⚪⚪⚪'
