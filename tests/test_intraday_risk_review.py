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


def _log_n_concerning(n, ticker='SOXL', detail_prefix='broker rejected order'):
    for i in range(n):
        signals_db.log_coverage_event("sl_placement", "live", ticker=ticker, result="failed_unexpectedly",
                                       detail=f"{detail_prefix} {i}")


def test_watermark_advances_past_all_new_events_even_when_message_truncated(env, monkeypatch):
    # More concerning events than the render cap -- the Slack message must
    # truncate its display, but the watermark must still cover every event
    # found this cycle, not just the rendered subset, so none of the
    # truncated-but-real ones get silently re-examined on the next poll.
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    _log_n_concerning(150)
    all_events = signals_db.get_coverage_events(scenario_key='sl_placement')
    max_id = max(e['id'] for e in all_events)

    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1
    assert f"... and {150 - signals_notify._MAX_RENDERED_COVERAGE_EVENTS} more" in posted[0][0]

    state = json.loads(signals_config.INTRADAY_RISK_REVIEW_STATE_PATH.read_text())
    assert state['last_seen_coverage_event_id'] == max_id

    # Next poll: nothing new left to find, no re-post of the "truncated" ones.
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1


def test_truncated_message_stays_under_slack_length_limit(env, monkeypatch):
    # 2026-08-18 rework (finding #4): the original version used ~24-char
    # details, too short to exercise a per-line-length regression -- the
    # review measured real worst-case coverage-event lines at 294-316 chars.
    # Use a long detail (well past the truncation cap) plus long
    # scenario_key/result/ticker strings so this test actually renders lines
    # near that real worst case and would catch a regression in either the
    # per-field truncation or the render cap.
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    long_detail = "x" * 400
    long_ticker = "VERYLONGTICKERSYM"
    for i in range(500):
        signals_db.log_coverage_event(
            "some_unusually_long_scenario_key_name_for_worst_case", "live",
            ticker=long_ticker, result=f"failed_unexpectedly_with_a_long_reason_code_{i}",
            detail=long_detail)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1
    msg = posted[0][0]
    assert len(msg) < 40000
    omitted = 500 - signals_notify._MAX_RENDERED_COVERAGE_EVENTS
    assert f"... and {omitted} more (see coverage_events directly)" in msg
    # Confirm the per-line truncation is actually engaged, not just the count.
    longest_line = max(msg.split("\n"), key=len)
    assert len(longest_line) < 400


def test_non_concerning_flood_between_two_concerning_events_does_not_lose_the_second(env, monkeypatch):
    # Real repro from the 2026-08-18 review (HIGH finding #1): a concerning
    # event, then a large run of NON-concerning events (e.g. a
    # reconciliation_mismatch-adjacent flood of routine blocked_* results),
    # then a second concerning event. The old watermark logic only advanced
    # past the CONCERNING subset's max id, and only on a successful post --
    # so a poll cycle that found zero concerning events (all mid-flood) never
    # advanced the watermark at all, and a large enough flood could wedge the
    # since_id-capped fetch on the same stale rows forever, permanently
    # hiding the second concerning event behind it.
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap

    signals_db.log_coverage_event("sl_placement", "live", ticker="SOXL", result="failed_unexpectedly",
                                   detail="first real failure")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1  # first concerning event alerted

    # A flood of non-concerning events -- must not itself alert, but must
    # still let the watermark progress.
    for i in range(50):
        signals_db.log_coverage_event("same_day_block", "live", ticker="SOXL", result="blocked_same_ticker",
                                       detail=f"routine guard {i}")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1  # still no new alert -- nothing concerning in the flood

    signals_db.log_coverage_event("sl_placement", "live", ticker="SOXL", result="failed_unexpectedly",
                                   detail="second real failure, must not be lost")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 2, "the second concerning event was silently dropped behind the non-concerning flood"
    assert "second real failure" in posted[1][0]


def test_drain_loop_pages_through_a_backlog_exceeding_one_fetch_limit(env, monkeypatch):
    # Finding #1's optional drain-loop addition: if a single since_id fetch
    # comes back exactly at the per-call cap, there may be more behind it --
    # one poll cycle should keep paging until it genuinely catches up, not
    # just advance by one cap's worth per poll. Monkeypatch the cap down so
    # this is cheap to exercise without logging thousands of real rows.
    monkeypatch.setattr(signals_notify, '_COVERAGE_EVENT_FETCH_LIMIT', 5)
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    _log_n_concerning(12)  # more than 2x the artificially small cap of 5
    all_events = signals_db.get_coverage_events(scenario_key='sl_placement')
    max_id = max(e['id'] for e in all_events)

    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1
    assert "12 concerning coverage event(s)" in posted[0][0]  # all 12 found in ONE poll cycle

    state = json.loads(signals_config.INTRADAY_RISK_REVIEW_STATE_PATH.read_text())
    assert state['last_seen_coverage_event_id'] == max_id


def test_incident_overflow_stays_under_budget_and_caps_count(env, monkeypatch):
    # Finding #2: incident rendering was completely uncapped -- the review
    # measured 200 real incidents rendering to 101,386 chars. Long
    # title/detail plus a real overflow count (well past _MAX_RENDERED_INCIDENTS).
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    long_title = "A" * 250  # past the real measured 154-char worst case
    long_detail = "B" * 400
    for i in range(200):
        signals_db.log_incident(long_title, long_detail, ticker="SOXL", account="soxl_ira",
                                 real_money_impact=True)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1
    msg = posted[0][0]
    assert len(msg) < 40000
    omitted = 200 - signals_notify._MAX_RENDERED_INCIDENTS
    assert f"... and {omitted} more incident(s)" in msg


def test_since_id_pagination_used_not_plain_limit(env, monkeypatch):
    # Regression guard for the original bug: watermark bootstrap must not
    # require pulling a large fixed-limit batch just to find the max id.
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    _log_n_concerning(3)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1
    assert "3 concerning coverage event(s)" in posted[0][0]
    # Nothing new -- must stay silent (proves watermark tracks id, not count).
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1
