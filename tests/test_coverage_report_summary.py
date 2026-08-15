"""Tests for scripts/coverage_report_summary.py -- the composition layer for the
nightly Slack Coverage Report (2026-08-14 redesign: the report had grown to ~200
lines of canary/test-infrastructure status with zero real-money content).

Reporting-only module, so these assert what a human sees, not what counts as a
deviation (that's tests/test_coverage_check.py's job and is unchanged).
"""
import os
import sys
import tempfile
from datetime import datetime

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db
from scripts.coverage_check import run_check
from scripts import coverage_report_summary as crs

CHECK_DATE = '2026-07-24'  # a real Friday -- run_check refuses non-trading days


@pytest.fixture
def isolated_db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    db.ensure_tables()
    yield db
    os.unlink(tmp_db.name)


def _add_node(ticker, notional=500.0, account='soxl_ira'):
    db.add_node(ticker, 'TrailingBothZScoreBreakout', 'canary', window=5,
                take_profit=0.1, stop_loss=0, max_hold_hours=48, account=account,
                starting_notional=notional)
    return [x for x in db.get_watchlist() if x['ticker'] == ticker][0]


def _closed_trade(ticker, exit_reason, entry_price, exit_price, shares, when,
                  is_dry_run_sim=False):
    node = _add_node(ticker)
    db.open_position(node, signal_price=entry_price, signal_time=when, entry_price=entry_price,
                      entry_time=when, shares=shares, is_dry_run_sim=is_dry_run_sim)
    pos = db.get_open_position(ticker)
    db.close_position(pos['id'], exit_signal_price=exit_price, exit_price=exit_price,
                       exit_time=when, exit_reason=exit_reason)
    return node


def _open_position(ticker, entry_price, shares, when, is_dry_run_sim=False):
    node = _add_node(ticker)
    db.open_position(node, signal_price=entry_price, signal_time=when, entry_price=entry_price,
                      entry_time=when, shares=shares, is_dry_run_sim=is_dry_run_sim)
    return node


# --------------------------------------------------------------------------
# (a) the collapsed rollup's counts match a full per-scenario render
# --------------------------------------------------------------------------

def _render_full_breakdown(results, reasons):
    """The exact per-scenario block send_coverage_report used to emit, kept here
    as the reference the collapsed rollup is checked against -- so the rollup
    can't silently drift from the real full-detail view."""
    out = []
    for r in results:
        if r['status'] == 'met':
            status = 'met'
        elif r['status'] == 'skipped':
            status = 'skipped'
        elif not r.get('ticket_eligible', True):
            status = 'informational'
        elif reasons.get(crs._key(r)):
            status = 'explained'
        else:
            status = 'unexplained'
        out.append(status)
    return out


def test_canary_rollup_counts_match_a_full_per_scenario_render(isolated_db):
    """The one-line rollup must be a lossless summary of the breakdown it
    replaced -- counted, not hand-waved."""
    ts = f'{CHECK_DATE} 10:30:00'
    # met: a canary whose closed trade has the expected exit_reason
    _closed_trade('CANARY_MET', 'TRAIL', 10.0, 11.0, 5, ts, is_dry_run_sim=True)
    db.add_scenario_expectation('canary_full_lifecycle', 'closes TRAIL', 'daily',
                                 'trade_lifecycle', ticker='CANARY_MET',
                                 check_params='{"expect_exit_reason": ["TRAIL"]}')
    # deviated (ticket-eligible): no trade at all today
    db.add_scenario_expectation('canary_early_sl', 'closes SL', 'daily', 'trade_lifecycle',
                                 ticker='CANARY_MISS',
                                 check_params='{"expect_exit_reason": ["SL"]}')
    # deviated, second one -- explained below
    db.add_scenario_expectation('canary_time_exit', 'closes TIME', 'daily', 'trade_lifecycle',
                                 ticker='CANARY_EXPLAINED',
                                 check_params='{"expect_exit_reason": ["TIME"]}')
    # informational tier: a miss here never mints a ticket
    db.add_scenario_expectation('canary_pinned_entry', 'pinned entry closes TRAIL',
                                 'informational', 'trade_lifecycle', ticker='CANARY_INFO',
                                 check_params='{"expect_exit_reason": ["TRAIL"]}')
    # non-canary group (the real reconciliation_mismatch shape: informational)
    db.add_scenario_expectation('reconciliation_mismatch', 'no mismatch', 'informational',
                                 'coverage_event', ticker='RECON_A',
                                 check_params='{"bad_results": ["mismatch"]}')

    run_check(CHECK_DATE)  # first pass records the deviation rows
    dev = [d for d in db.get_deviations(check_date=CHECK_DATE)
           if d['scenario_key'] == 'canary_time_exit'][0]
    db.explain_deviation(dev['id'], 'known -- no signal today')

    results = run_check(CHECK_DATE)
    reasons = {crs._key(d): d['reason'] for d in db.get_deviations(check_date=CHECK_DATE)}

    full = _render_full_breakdown(results, reasons)
    canary_full = [s for s, r in zip(full, results) if r['scenario_key'].startswith('canary_')]
    other_full = [s for s, r in zip(full, results) if not r['scenario_key'].startswith('canary_')]

    groups = crs.classify(results, reasons)
    for group, reference in (('canary', canary_full), ('other', other_full)):
        counts = groups[group]
        assert counts['total'] == len(reference)
        for bucket in ('met', 'skipped', 'informational', 'explained', 'unexplained'):
            assert counts[bucket] == reference.count(bucket), (group, bucket, reference)

    # and the rendered lines are actually one per group, not per scenario
    lines = crs.rollup_lines(results, reasons)
    assert len(lines) == 2
    canary_line = [l for l in lines if l.startswith('Canary:')][0]
    assert f"{canary_full.count('met')}/{len(canary_full)} met" in canary_line
    assert canary_line.startswith(f"Canary: {canary_full.count('unexplained')} UNEXPLAINED")
    assert 'reconciliation_mismatch:' in lines[1]  # single-key group keeps its real name


def test_all_informational_group_omits_a_misleading_zero_met_fraction(isolated_db):
    """reconciliation_mismatch's real daily shape: every row is an informational
    'no mismatch event today', which is the GOOD outcome -- rendering it as
    '0/20 met' would read as a failure on a phone."""
    results = [dict(scenario_key='reconciliation_mismatch', ticker=f'T{i}', node_id=i,
                    mode=None, status='deviated', ticket_eligible=False,
                    summary='no coverage_events') for i in range(20)]
    line = crs.rollup_lines(results, {})[0]
    assert line == 'reconciliation_mismatch: 20 informational (no ticket)'


def test_full_detail_remains_available_from_the_standalone_tool(isolated_db):
    """Backward-compatible access: collapsing the push message must not remove
    the per-scenario view, which run_check still returns in full."""
    db.add_scenario_expectation('canary_full_lifecycle', 'closes TRAIL', 'daily',
                                 'trade_lifecycle', ticker='CANARY_A',
                                 check_params='{"expect_exit_reason": ["TRAIL"]}')
    db.add_scenario_expectation('canary_early_sl', 'closes SL', 'daily', 'trade_lifecycle',
                                 ticker='CANARY_B', check_params='{"expect_exit_reason": ["SL"]}')
    results = run_check(CHECK_DATE)
    assert len(results) == 2  # the standalone tool still sees every scenario
    body = "\n".join(crs.compose(CHECK_DATE, results, {}, price_fn=lambda t: None))
    assert 'scripts/coverage_check.py --date 2026-07-24' in body


# --------------------------------------------------------------------------
# (b) the new real-money section
# --------------------------------------------------------------------------

def test_pnl_line_sums_real_realized_and_unrealized_and_ignores_synthetic(isolated_db):
    ts = f'{CHECK_DATE} 10:30:00'
    _closed_trade('REALWIN', 'TRAIL', 10.0, 11.0, 20, ts)          # +$20 real
    _closed_trade('REALLOSS', 'SL', 50.0, 49.5, 10, ts)            # -$5 real
    _closed_trade('CANARYTRD', 'SL', 30.0, 20.0, 100, ts, is_dry_run_sim=True)  # ignored
    _open_position('REALOPEN', 25.0, 4, ts)                        # +$8 at 27.0
    _open_position('SIMOPEN', 25.0, 400, ts, is_dry_run_sim=True)  # ignored

    pnl = crs.todays_pnl(CHECK_DATE, price_fn=lambda t: 27.0)
    assert round(pnl['realized'], 2) == 15.0
    assert round(pnl['unrealized'], 2) == 8.0
    assert pnl['closed'] == 2 and pnl['open_priced'] == 1 and pnl['open_unpriced'] == 0

    line = crs.pnl_line(CHECK_DATE, price_fn=lambda t: 27.0)
    assert line == ("*Portfolio: +$23.00 realized+open* (realized +$15.00 on 2 closed today "
                    "— each trade's full lifetime P&L, not today's move; "
                    "unrealized +$8.00 on 1 open (total since entry, not today's move))")


def test_pnl_line_never_claims_the_figures_are_todays_move(isolated_db):
    """The correctness fix (Opus review 2026-08-14): realized books a trade's
    ENTIRE lifetime P&L if it merely exited today, and unrealized is total
    since entry -- neither is a same-day mark. The dollar amounts were always
    right; the word 'today' attached to them was not."""
    entered = '2026-07-21 10:30:00'   # 3 days before CHECK_DATE
    node = _add_node('OLDTRADE')
    db.open_position(node, signal_price=10.0, signal_time=entered, entry_price=10.0,
                     entry_time=entered, shares=10)
    pos = db.get_open_position('OLDTRADE')
    db.close_position(pos['id'], exit_signal_price=13.0, exit_price=13.0,
                      exit_time=f'{CHECK_DATE} 15:30:00', exit_reason='TRAIL')
    _open_position('OLDOPEN', 20.0, 5, entered)

    line = crs.pnl_line(CHECK_DATE, price_fn=lambda t: 24.0)
    # the full 3-day move is what's booked, so the label must not say "today"
    assert '+$30.00 on 1 closed today' in line
    assert "full lifetime P&L, not today's move" in line
    assert "total since entry, not today's move" in line
    assert 'today*' not in line          # the old "*Portfolio: X today*" claim is gone
    assert '*Portfolio: +$50.00 realized+open*' in line


def test_pnl_line_flags_a_trade_whose_share_count_may_be_stale_after_a_topup(isolated_db):
    """trade_log.shares is known to go stale after a same-day post-fill top-up
    (fixed for new trades, historical rows like RETL's not backfilled), so
    realized P&L off it can be understated. Name the ticker, don't silently
    trust it."""
    ts = f'{CHECK_DATE} 10:30:00'
    _closed_trade('TOPPEDUP', 'TRAIL', 10.0, 11.0, 20, ts)
    _closed_trade('CLEAN', 'TRAIL', 10.0, 11.0, 20, ts)
    with db._conn() as c:
        c.execute("INSERT INTO coverage_events (ts, scenario_key, mode, ticker, result) "
                  "VALUES (?, 'top_up', 'live', 'TOPPEDUP', 'placed')", (ts,))
        c.commit()

    assert crs.stale_share_count_tickers(CHECK_DATE) == ['TOPPEDUP']
    line = crs.pnl_line(CHECK_DATE, price_fn=lambda t: None)
    assert 'realized may be understated for TOPPEDUP' in line
    assert 'CLEAN' not in line


def test_no_topup_means_no_stale_share_caveat(isolated_db):
    ts = f'{CHECK_DATE} 10:30:00'
    _closed_trade('CLEAN', 'TRAIL', 10.0, 11.0, 20, ts)
    assert crs.stale_share_count_tickers(CHECK_DATE) == []
    assert 'understated' not in crs.pnl_line(CHECK_DATE, price_fn=lambda t: None)


def test_pnl_line_reports_unpriced_positions_instead_of_understating(isolated_db):
    ts = f'{CHECK_DATE} 10:30:00'
    _open_position('REALOPEN', 25.0, 4, ts)

    def boom(ticker):
        raise RuntimeError('broker down')

    line = crs.pnl_line(CHECK_DATE, price_fn=boom)
    assert '1 unpriced' in line and 'on 0 open' in line


def test_stale_share_caveat_wording_reflects_whether_the_fix_is_actually_present(isolated_db, monkeypatch):
    """2026-08-15 review finding (F1): the caveat used to always claim 'the fix'
    exists, which is only true once Tranche 1's log_trade_exit(shares=...) param
    is actually merged into this tree -- this module can run in a worktree
    where it isn't. The wording must reflect real introspected state, not a
    hardcoded assumption in either direction."""
    ts = f'{CHECK_DATE} 10:30:00'
    _closed_trade('TOPPEDUP', 'TRAIL', 10.0, 11.0, 20, ts)
    with db._conn() as c:
        c.execute("INSERT INTO coverage_events (ts, scenario_key, mode, ticker, result) "
                  "VALUES (?, 'top_up', 'live', 'TOPPEDUP', 'placed')", (ts,))
        c.commit()

    monkeypatch.setattr(crs, '_trade_log_shares_fix_landed', lambda: True)
    line_fixed = crs.pnl_line(CHECK_DATE, price_fn=lambda t: None)
    assert 'predates the fix' in line_fixed
    assert 'not fixed in this tree' not in line_fixed

    monkeypatch.setattr(crs, '_trade_log_shares_fix_landed', lambda: False)
    line_unfixed = crs.pnl_line(CHECK_DATE, price_fn=lambda t: None)
    assert 'not fixed in this tree yet' in line_unfixed
    assert 'predates the fix' not in line_unfixed


def test_fix_landed_introspection_matches_real_log_trade_exit_signature(isolated_db):
    """Sanity check on the introspection mechanism itself, against whatever
    signals_db.log_trade_exit actually looks like in THIS tree right now --
    not a hardcoded True/False, since that's exactly the kind of assumption
    that went stale before."""
    import inspect
    expected = 'shares' in inspect.signature(db.log_trade_exit).parameters
    assert crs._trade_log_shares_fix_landed() == expected


def test_digest_discloses_unpriced_positions_excluded_from_the_total(isolated_db):
    """2026-08-15 review finding (F2): the digest (line 1, the only thing
    Slack's mobile preview reliably shows) used to state a clean total even
    when real open positions had no price and were silently excluded -- the
    disclosure only existed 3 lines down in the detail line. The digest must
    say so at the point it states the number."""
    ts = f'{CHECK_DATE} 10:30:00'
    _open_position('REALOPEN', 25.0, 4, ts)

    body = "\n".join(crs.compose(CHECK_DATE, [], {}, price_fn=lambda t: None))
    assert '1 unpriced, excluded' in body.splitlines()[0]


def test_digest_omits_unpriced_disclosure_when_everything_priced(isolated_db):
    ts = f'{CHECK_DATE} 10:30:00'
    _open_position('REALOPEN', 25.0, 4, ts)

    body = "\n".join(crs.compose(CHECK_DATE, [], {}, price_fn=lambda t: 30.0))
    assert 'unpriced' not in body.splitlines()[0]


def test_slack_volume_counts_only_sub_threshold_real_node_tickers(isolated_db):
    """Raw count, no judgment -- and scoped to the small-but-real tier, so a
    dry_run canary's ticker and a big real node's ticker are both excluded."""
    _add_node('SMALLREAL', notional=500.0)
    _add_node('BIGREAL', notional=10000.0)
    with db._conn() as c:
        c.execute("UPDATE watch_list SET state='live' WHERE ticker IN ('SMALLREAL','BIGREAL')")
        c.execute("UPDATE watch_list SET state='dry_run' WHERE ticker NOT IN ('SMALLREAL','BIGREAL')")
        c.commit()

    db.log_slack_message('live', 'SMALLREAL trailing buy — still pending (reminder #3)')
    db.log_slack_message('live', 'SMALLREAL SELL signal fired')
    db.log_slack_message('live', 'BIGREAL SELL signal fired')          # over threshold
    db.log_slack_message('live', 'Morning Report — nothing to see')    # no ticker

    line = crs.slack_volume_line()
    assert line.startswith('Slack volume: 2 message(s) in 7d for the 1 real node(s) under $5,000')
    assert 'review trigger only' in line


def test_slack_volume_line_sits_at_the_very_bottom_of_the_message(isolated_db):
    """It's a weekly number posted daily and neither of the two things the user
    checks nightly, so it must sit below the money line, the incidents and the
    scenario rollup -- it used to be wedged between money and incidents."""
    _add_node('SMALLREAL', notional=500.0)
    with db._conn() as c:
        c.execute("UPDATE watch_list SET state='live' WHERE ticker='SMALLREAL'")
        c.commit()
    db.log_slack_message('live', 'SMALLREAL SELL signal fired')

    body = crs.compose(CHECK_DATE, [], {}, price_fn=lambda t: None)
    assert body[-1].startswith('Slack volume:')
    money_at = [i for i, l in enumerate(body) if l.startswith('*Portfolio:')][0]
    detail_at = [i for i, l in enumerate(body) if 'coverage_check.py --date' in l][0]
    assert money_at < detail_at < len(body) - 1


def test_slack_volume_line_is_omitted_when_no_sub_threshold_node_exists(isolated_db):
    assert crs.slack_volume_line() is None
    body = "\n".join(crs.compose(CHECK_DATE, [], {}, price_fn=lambda t: None))
    assert 'Slack volume' not in body


def test_incident_logged_today_surfaces_as_one_line(isolated_db):
    db.log_incident('SOXS trailing-buy fill invisible to daemon', 'detail here',
                    ticker='SOXS', account='ira', real_money_impact=True)
    today = datetime.now().strftime('%Y-%m-%d')
    lines = crs.incident_lines(today)
    assert len(lines) == 1
    assert 'SOXS ira' in lines[0]
    assert 'REAL MONEY' in lines[0]
    assert 'trailing-buy fill invisible' in lines[0]


def test_no_incident_today_emits_no_body_section(isolated_db):
    """The BODY section disappears entirely (not a '0 new bugs' line), per the
    user's 5-seconds-on-a-phone requirement. The digest still carries a 'no
    incidents' token, deliberately: line 1 has a fixed shape so a missing
    segment can never be confused with a section that failed to render."""
    assert crs.incident_lines(CHECK_DATE) == []
    body = crs.compose(CHECK_DATE, [], {}, price_fn=lambda t: None)
    assert 'no incidents' in body[0]
    assert not [l for l in body[1:] if 'incident' in l.lower()]


def test_incident_date_uses_local_time_not_utc(isolated_db):
    """trading_incidents.ts is stored UTC (datetime('now')); check_date is an ET
    calendar date. A late-evening ET incident must land on today's report, not
    tomorrow's."""
    with db._conn() as c:
        c.execute("INSERT INTO trading_incidents (ts, title, detail) VALUES "
                  "(datetime('now'), 'late evening bug', 'd')")
        c.commit()
        local_today = c.execute("SELECT date('now','localtime')").fetchone()[0]
        utc_today = c.execute("SELECT date('now')").fetchone()[0]
    assert len(crs.incident_lines(local_today)) == 1
    if utc_today != local_today:
        assert crs.incident_lines(utc_today) == []


def test_pnl_failure_never_costs_the_unexplained_alert(isolated_db, monkeypatch):
    monkeypatch.setattr(crs, 'todays_pnl',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('db gone')))
    node = _add_node('REALX')
    with db._conn() as c:
        c.execute("UPDATE watch_list SET version='v5', state='live' WHERE id=?", (node['id'],))
        c.commit()
    results = [dict(scenario_key='reconciliation_mismatch', ticker='REALX', node_id=node['id'],
                    mode='live', status='deviated', summary='no closed trade found')]
    body = crs.compose(CHECK_DATE, results, {})
    joined = "\n".join(body)
    assert ':red_circle: 1 UNEXPLAINED on REAL node(s):' in joined
    assert 'P&L unavailable' in joined
    # the digest degrades on the money segment only -- it still carries the alert
    assert body[0] == '*07-24 · P&L unavailable · no incidents · 1 unexplained (all REAL nodes)*'


# --------------------------------------------------------------------------
# (c) the unexplained-deviation alert at top is unchanged
# --------------------------------------------------------------------------

def test_all_canary_unexplained_collapses_and_says_so_in_the_digest(isolated_db):
    """node_id values that don't resolve fall back to the scenario_key prefix --
    these are all canary_*, so nothing here should read as real-money risk."""
    results = [
        dict(scenario_key='canary_early_sl', ticker='FAS', node_id=214, mode='live',
             status='deviated', summary='no closed trade found for FAS'),
        dict(scenario_key='canary_early_sl', ticker='FAZ', node_id=215, mode='live',
             status='deviated', summary='no closed trade found for FAZ'),
        dict(scenario_key='canary_full_lifecycle', ticker='FAZ', node_id=213, mode='live',
             status='met', summary='exit_reason=TRAIL'),
        dict(scenario_key='canary_pinned_entry', ticker='IWM', node_id=139, mode='live',
             status='deviated', ticket_eligible=False, summary='no closed trade found'),
    ]
    body = crs.compose(CHECK_DATE, results, {}, price_fn=lambda t: None)
    assert body[0] == '*07-24 · +$0.00 realized+open · no incidents · 2 unexplained (all canary)*'
    assert body[1] == (':large_yellow_circle: 2 unexplained on canary/test node(s) '
                       '(expected most nights): canary_early_sl x2')
    assert ':red_circle:' not in "\n".join(body)
    # an informational miss must never render as UNEXPLAINED (unchanged rule)
    assert 'canary_pinned_entry' not in body[1]


def test_a_real_node_deviation_stands_out_from_the_canary_ones(isolated_db):
    """The case the split exists for: a real-money node's unexplained deviation
    must not be buried in an undifferentiated list next to canary noise."""
    real = _add_node('SOXL')
    with db._conn() as c:
        c.execute("UPDATE watch_list SET version='v5', state='live' WHERE id=?", (real['id'],))
        c.commit()
    results = [
        dict(scenario_key='canary_early_sl', ticker='FAS', node_id=214, mode='live',
             status='deviated', summary='no closed trade found for FAS'),
        dict(scenario_key='reconciliation_mismatch', ticker='SOXL', node_id=real['id'],
             mode='live', status='deviated', summary='broker shows 80 shares, DB shows 78'),
    ]
    body = crs.compose(CHECK_DATE, results, {}, price_fn=lambda t: None)
    assert body[0] == ('*07-24 · +$0.00 realized+open · no incidents · '
                       '2 unexplained (1 REAL, 1 canary)*')
    assert body[1] == ':red_circle: 1 UNEXPLAINED on REAL node(s):'
    assert body[2] == '  • reconciliation_mismatch (SOXL): broker shows 80 shares, DB shows 78'
    # the canary one is still reported, just collapsed and below the real one
    assert body[3].startswith(':large_yellow_circle: 1 unexplained on canary/test node(s)')


def test_a_dry_run_node_counts_as_canary_even_without_a_canary_version(isolated_db):
    """No real order reaches the broker from a dry_run node, so it belongs in
    the canary bucket regardless of what its `version` string says."""
    node = _add_node('FAZ')
    with db._conn() as c:
        c.execute("UPDATE watch_list SET version='soxl_test', state='dry_run' WHERE id=?",
                  (node['id'],))
        c.commit()
    r = dict(scenario_key='reconciliation_mismatch', ticker='FAZ', node_id=node['id'],
             mode='live', status='deviated', summary='mismatch')
    assert crs.is_canary_result(r) is True
    real, canary = crs.split_unexplained([r])
    assert (real, canary) == ([], [r])


def test_an_unresolvable_non_canary_scenario_is_treated_as_real(isolated_db):
    """Conservative direction: the failure mode to avoid is a real-money
    deviation hiding inside the canary bucket, so unknown means REAL."""
    r = dict(scenario_key='some_new_scenario', ticker='ZZZ', node_id=99999, mode='live',
             status='deviated', summary='x')
    assert crs.is_canary_result(r) is False
    assert crs.split_unexplained([r]) == ([r], [])


def test_digest_line_leads_with_money_incidents_and_deviations(isolated_db):
    """Line 1 must be the whole night, not a title and a date the user already
    knows -- Slack's mobile preview truncates to roughly this much."""
    def dev(key='canary_early_sl'):
        return dict(scenario_key=key, ticker='FAS', node_id=1, mode='live',
                    status='deviated', summary='s')

    assert crs.digest_line('2026-08-14', '-$99.70 realized+open', 2, [], [dev()] * 7) == \
        '*08-14 · -$99.70 realized+open · 2 incidents · 7 unexplained (all canary)*'
    assert crs.digest_line('2026-08-14', '+$0.00 realized+open', 1, [], []) == \
        '*08-14 · +$0.00 realized+open · 1 incident · no unexplained*'
    assert crs.digest_line('2026-08-14', '+$1.00', 0, [dev()] * 2, [dev()] * 5) == \
        '*08-14 · +$1.00 · no incidents · 7 unexplained (2 REAL, 5 canary)*'
    assert crs.digest_line('2026-08-14', '+$1.00', 0, [dev()], []) == \
        '*08-14 · +$1.00 · no incidents · 1 unexplained (all REAL nodes)*'
    assert 'incidents unknown' in crs.digest_line('2026-08-14', '+$0.00', None, [], [])


def test_incident_count_reaches_the_digest(isolated_db):
    db.log_incident('SOXS trailing-buy fill invisible to daemon', 'detail', ticker='SOXS',
                    account='ira', real_money_impact=True)
    today = datetime.now().strftime('%Y-%m-%d')
    body = crs.compose(today, [], {}, price_fn=lambda t: None)
    assert '· 1 incident ·' in body[0]
    assert any('REAL MONEY' in l for l in body[1:])


def test_clean_day_reports_no_unexplained_deviations_unchanged(isolated_db):
    results = [dict(scenario_key='canary_full_lifecycle', ticker='FAZ', node_id=213,
                    mode='live', status='met', summary='exit_reason=TRAIL')]
    body = crs.compose(CHECK_DATE, results, {}, price_fn=lambda t: None)
    assert body[1] == ':white_check_mark: No unexplained deviations.'


def test_send_coverage_report_still_posts_the_unexplained_alert(isolated_db, monkeypatch):
    """End-to-end through the real Slack entry point: the redesign must not have
    changed the one part of the report that was already high-signal."""
    import signals_notify
    captured = {}
    monkeypatch.setattr(signals_notify, '_post_message',
                        lambda text, blocks=None: (captured.setdefault('text', text), ('C1', '1'))[1])
    monkeypatch.setattr(crs, 'pnl_line', lambda *a, **k: '*Portfolio: +$0.00 today*')

    db.add_scenario_expectation('canary_early_sl', 'closes SL', 'daily', 'trade_lifecycle',
                                 ticker='CANARY_MISS',
                                 check_params='{"expect_exit_reason": ["SL"]}')
    signals_notify.send_coverage_report(CHECK_DATE)

    lines = captured['text'].splitlines()
    assert lines[0].startswith('*07-24 · ')
    assert '1 unexplained (all canary)' in lines[0]
    assert ':large_yellow_circle: 1 unexplained on canary/test node(s)' in captured['text']
    assert 'canary_early_sl' in captured['text']
    # and the whole message is short now
    assert len(lines) < 12
