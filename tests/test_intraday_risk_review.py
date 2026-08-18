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
    # Distinct tickers (2026-08-18, burst-grouping rework): 150 IDENTICAL
    # (scenario_key, ticker, result) events would now correctly collapse to
    # ONE rendered line via _group_event_bursts instead of exercising the
    # render-cap path this test is actually about -- use a distinct ticker
    # per event so none of them group, same as before grouping existed.
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    for i in range(150):
        _log_n_concerning(1, ticker=f"T{i}")
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

    # Distinct ticker (2026-08-18, cross-cycle burst-cooldown rework): the
    # first and second concerning events must be a genuinely DIFFERENT key
    # now that repeat events sharing (scenario_key, ticker, result, mode)
    # across cycles are deliberately cooldown-suppressed (see
    # test_repeat_burst_across_poll_cycles_is_cooldown_suppressed below) --
    # reusing SOXL/failed_unexpectedly here would test that mechanism
    # instead of this test's actual point (a flood must not wedge the
    # watermark and hide a later DIFFERENT real event).
    signals_db.log_coverage_event("sl_placement", "live", ticker="AGQ", result="failed_unexpectedly",
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


def test_fixture_sourced_event_excluded_from_render_and_count(env, monkeypatch):
    # Gap 1 (2026-08-18): scripts/stage_check_order_guard_scenarios.py's
    # synthetic runs tag source='fixture:...' -- these must not render or
    # count toward the header total, mirroring
    # test_coverage_check.py::test_compute_status_excludes_fixture_sourced_events.
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    # mode='dry_run' (2026-08-18 fix), not 'live' -- matches the real shape
    # of scripts/stage_check_order_guard_scenarios.py's actual fixture rows
    # (confirmed against the live DB: all 42 real STAGE_GUARD_TEST rows are
    # mode='dry_run') and avoids tripping signals_db.log_coverage_event's own
    # printed warning for a fixture-sourced mode='live' row ("a fixture
    # should never produce a real trade" -- structurally invalid, found by
    # paired review).
    signals_db.log_coverage_event("check_order", "dry_run", ticker="SOXL", result="rejected",
                                   detail="synthetic guard-scenario run",
                                   source="fixture:stage_check_order_guard_scenarios")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert posted == []  # nothing real happened, no incidents -- must stay silent


def test_fixture_sourced_event_does_not_suppress_a_real_concerning_event(env, monkeypatch):
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    # mode='dry_run' (2026-08-18 fix), not 'live' -- matches the real shape
    # of scripts/stage_check_order_guard_scenarios.py's actual fixture rows
    # (confirmed against the live DB: all 42 real STAGE_GUARD_TEST rows are
    # mode='dry_run') and avoids tripping signals_db.log_coverage_event's own
    # printed warning for a fixture-sourced mode='live' row ("a fixture
    # should never produce a real trade" -- structurally invalid, found by
    # paired review).
    signals_db.log_coverage_event("check_order", "dry_run", ticker="SOXL", result="rejected",
                                   detail="synthetic guard-scenario run",
                                   source="fixture:stage_check_order_guard_scenarios")
    signals_db.log_coverage_event("sl_placement", "live", ticker="AGQ", result="failed_unexpectedly",
                                   detail="real broker rejection")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1
    msg = posted[0][0]
    assert "1 concerning coverage event(s)" in msg  # only the real one counted
    assert "AGQ" in msg
    assert "SOXL" not in msg  # the fixture-sourced row must not render at all


def test_null_source_event_still_counts_and_renders(env, monkeypatch):
    # Regression guard against the NULL-safety trap: 9,482 of ~9,534 real
    # coverage_events rows have source IS NULL (most call sites don't pass
    # it) -- a naive `source.startswith('fixture:')` check crashes on None,
    # and a wrong truthiness check could just as easily exclude every NULL
    # row instead of only fixture ones.
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    signals_db.log_coverage_event("sl_placement", "live", ticker="SOXL", result="failed_unexpectedly",
                                   detail="real broker rejection, no source tag")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1
    assert "1 concerning coverage event(s)" in posted[0][0]
    assert "SOXL" in posted[0][0]


def test_dedicated_alert_scenario_burst_deduped_but_counted(env, monkeypatch):
    # Gap 2 -- real shape of the 2026-08-16 ERY/YINN incident: a burst of one
    # scenario_key's events (reconciliation_fetch_failed) that ALSO already
    # got its own dedicated Slack alert via a different code path
    # (_RECONCILE_FETCH_FAIL_ALERTED's throttled post in
    # check_live_state_reconciliation), alongside a genuine anomaly with no
    # dedicated alert of its own (sl_placement). The dedicated-alert burst
    # must be counted in the header but excluded from the rendered lines and
    # from the "... and N more" omission count; the genuine anomaly must
    # still render normally.
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    for i in range(24):
        signals_db.log_coverage_event("reconciliation_fetch_failed", "live", ticker="ERY",
                                       result="failed_after_retries",
                                       detail=f"outage retry {i}")
    signals_db.log_coverage_event("sl_placement", "live", ticker="SOXL", result="failed_unexpectedly",
                                   detail="genuine anomaly, no dedicated alert path")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1
    msg = posted[0][0]
    assert "25 concerning coverage event(s) (24 already had a dedicated alert)" in msg
    assert "SOXL" in msg
    assert "failed_unexpectedly" in msg
    assert "reconciliation_fetch_failed" not in msg  # deduped out of the render entirely
    assert "ERY" not in msg
    assert "... and" not in msg  # nothing was omitted from the (already-small) render


def test_dedicated_alert_scenario_alone_stays_silent(env, monkeypatch):
    # If EVERYTHING new this cycle is already covered by a dedicated alert
    # (no incidents, no other concerning events), the catch-up review itself
    # must not post at all -- posting an empty-of-new-information message
    # would recreate exactly the noise problem this function exists to avoid.
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    for i in range(5):
        signals_db.log_coverage_event("reconciliation_mismatch", "live", ticker="ERY",
                                       result="stop_price_mismatch", detail=f"mismatch {i}")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert posted == []
    # Watermark must still advance so this doesn't get re-examined forever.
    state = json.loads(signals_config.INTRADAY_RISK_REVIEW_STATE_PATH.read_text())
    all_events = signals_db.get_coverage_events(scenario_key='reconciliation_mismatch')
    assert state['last_seen_coverage_event_id'] == max(e['id'] for e in all_events)


def test_burst_of_non_dedicated_scenario_collapses_to_one_line(env, monkeypatch):
    # Gap 2 (general case): a burst on a scenario_key with NO dedicated alert
    # of its own (unlike reconciliation_fetch_failed/reconciliation_mismatch)
    # must still collapse to one line, not render N raw duplicates -- the
    # real shape named in _group_event_bursts' own comment (addon_leg_merge's
    # cancel_failed branch, etc.).
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    for i in range(10):
        signals_db.log_coverage_event("addon_leg_merge", "live", ticker="SOXL",
                                       result="cancel_failed", detail=f"attempt {i}")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1
    msg = posted[0][0]
    assert "10 concerning coverage event(s)" in msg
    assert msg.count("addon_leg_merge") == 1  # collapsed to exactly one line
    assert "(10x)" in msg
    assert "... and" not in msg  # nothing was actually omitted, just grouped


def test_burst_grouping_leaves_distinct_scenarios_separately_rendered(env, monkeypatch):
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    for i in range(5):
        signals_db.log_coverage_event("addon_leg_merge", "live", ticker="SOXL",
                                       result="cancel_failed", detail=f"attempt {i}")
    signals_db.log_coverage_event("sl_placement", "live", ticker="AGQ",
                                   result="failed_unexpectedly", detail="lone anomaly")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1
    msg = posted[0][0]
    assert "6 concerning coverage event(s)" in msg
    assert "(5x)" in msg
    assert "AGQ" in msg
    assert "failed_unexpectedly" in msg
    assert " (1x)" not in msg  # a lone event must not get a burst tag


def test_burst_grouping_does_not_collapse_differing_results(env, monkeypatch):
    # Direct guard for _group_event_bursts' central claim: `result` IS part
    # of the grouping key. Two events sharing scenario_key+ticker but with
    # genuinely different `result` values must render as two separate lines,
    # not collapse into one misleading "(2x)" summary.
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    signals_db.log_coverage_event("sl_placement", "live", ticker="SOXL",
                                   result="failed_unexpectedly", detail="broker timeout")
    signals_db.log_coverage_event("sl_placement", "live", ticker="SOXL",
                                   result="rejected", detail="insufficient buying power")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1
    msg = posted[0][0]
    assert "2 concerning coverage event(s)" in msg
    assert "broker timeout" in msg
    assert "insufficient buying power" in msg
    assert "(2x)" not in msg


def test_burst_group_representative_is_the_newest_event(env, monkeypatch):
    # Gap-2 review finding: the docstring claims the newest event in a group
    # is kept as the representative -- pin that directly instead of only
    # inferring it from an unrelated test.
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    for i in range(10):
        signals_db.log_coverage_event("addon_leg_merge", "live", ticker="SOXL",
                                       result="cancel_failed", detail=f"attempt {i}")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    msg = posted[0][0]
    assert "attempt 9" in msg  # the newest (last-logged) event's detail
    assert "attempt 0" not in msg


def test_grouping_key_includes_mode_live_and_dry_run_do_not_collapse(env, monkeypatch):
    # Paired-review finding (2026-08-18): the grouping key must include
    # `mode`, not just (scenario_key, ticker, result) -- otherwise a real
    # (live) event sharing the other 3 fields with a synthetic (dry_run)
    # event collapses into one line, and since the representative is the
    # newest of the group, the real event's own detail could be silently
    # replaced by the synthetic one's. Confirmed not hypothetical: this
    # project runs real same-ticker live+dry_run node pairs (e.g. JNUG,
    # SOXL across soxl_ira/ira/brokerage).
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    signals_db.log_coverage_event("pre_action_state_verification", "live", ticker="FAZ",
                                   result="fetch_failed", detail="REAL account, real capital at risk")
    signals_db.log_coverage_event("pre_action_state_verification", "dry_run", ticker="FAZ",
                                   result="fetch_failed", detail="synthetic dry_run retry")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1
    msg = posted[0][0]
    assert "2 concerning coverage event(s)" in msg
    assert "REAL account, real capital at risk" in msg
    assert "synthetic dry_run retry" in msg
    assert "(2x)" not in msg  # each mode renders its own line, not one collapsed pair


def test_repeat_burst_across_poll_cycles_is_cooldown_suppressed(env, monkeypatch):
    # Paired-review finding (2026-08-18): _group_event_bursts only collapses
    # duplicates WITHIN one poll cycle's batch -- a real incident spanning
    # multiple 5-minute cycles (confirmed against the live DB: a real
    # addon_buying_power_drift_check storm spanned 51 distinct review-window
    # cycles) would otherwise still post one Slack message PER CYCLE. The
    # SAME still-ongoing condition recurring in a later cycle, within the
    # cooldown window, must not re-post a redundant message.
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    signals_db.log_coverage_event("addon_buying_power_drift_check", "live", ticker="brokerage",
                                   result="fetch_failed", detail="cycle 1")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1
    assert "cycle 1" in posted[0][0]

    # Same condition, 5 minutes later (still well within the 15-min cooldown)
    # -- same shape as consecutive 5-minute polls hitting an ongoing outage.
    still_cooling = TRADING_HOURS_NOON + timedelta(minutes=5)
    signals_db.log_coverage_event("addon_buying_power_drift_check", "live", ticker="brokerage",
                                   result="fetch_failed", detail="cycle 2")
    signals_notify.check_intraday_risk_review(now=still_cooling)
    assert len(posted) == 1, "a still-cooling-down repeat must not post a new message"


def test_burst_repeat_cooldown_expires_and_realerts(env, monkeypatch):
    # A genuinely still-ongoing problem must not go silent forever after its
    # first mention -- once the cooldown window elapses, a recurrence of the
    # same condition re-alerts (matches the existing _RECONCILE_COOLDOWN_SECS
    # convention used elsewhere in this file for repeated-condition alerts).
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap
    signals_db.log_coverage_event("addon_buying_power_drift_check", "live", ticker="brokerage",
                                   result="fetch_failed", detail="cycle 1")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1

    past_cooldown = TRADING_HOURS_NOON + timedelta(
        seconds=signals_notify._INTRADAY_BURST_REPEAT_COOLDOWN_SECS + 1)
    signals_db.log_coverage_event("addon_buying_power_drift_check", "live", ticker="brokerage",
                                   result="fetch_failed", detail="cycle N, still happening")
    signals_notify.check_intraday_risk_review(now=past_cooldown)
    assert len(posted) == 2, "a genuinely still-ongoing problem must re-alert after the cooldown expires"
    assert "cycle N, still happening" in posted[1][0]


def test_cap_loser_does_not_wrongly_enter_cooldown(env, monkeypatch):
    # Review finding (test gap, 2026-08-18): the cooldown-recording loop
    # iterates `rendered` (what _select_diverse_events actually returned),
    # not `fresh_groups` (every group eligible to render before the cap was
    # applied) -- a regression swapping the two would silently start a
    # cooldown for a group that was never actually shown to the user, making
    # it wrongly suppressed the next time it recurs. Pin this directly.
    monkeypatch.setattr(signals_notify, '_MAX_RENDERED_COVERAGE_EVENTS', 1)
    posted = _posted(monkeypatch)
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)  # bootstrap

    # Two distinct groups (different ticker), cap=1 -- only one can render.
    signals_db.log_coverage_event("sl_placement", "live", ticker="AGQ",
                                   result="failed_unexpectedly", detail="AGQ cap-loser")
    signals_db.log_coverage_event("sl_placement", "live", ticker="DPST",
                                   result="failed_unexpectedly", detail="DPST cap-winner")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 1
    assert "DPST cap-winner" in posted[0][0]
    assert "AGQ cap-loser" not in posted[0][0]

    # A NEW event on the cap-loser's exact same key, same `now` (0 seconds
    # later) -- if the cap-loser had wrongly entered cooldown in the cycle
    # above, this would be silently suppressed for 15 minutes instead of
    # rendering normally.
    signals_db.log_coverage_event("sl_placement", "live", ticker="AGQ",
                                   result="failed_unexpectedly", detail="AGQ second real event")
    signals_notify.check_intraday_risk_review(now=TRADING_HOURS_NOON)
    assert len(posted) == 2, "the cap-loser must not have wrongly entered cooldown"
    assert "AGQ second real event" in posted[1][0]


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
