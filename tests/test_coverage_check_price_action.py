"""Tests for scripts/coverage_check.py's 2026-08-08 price-action auto-explain
(_entry_threshold_crossed + run_check's auto_reason wiring) -- landed with
zero dedicated tests (the commit's own diff to tests/test_coverage_check.py
was purely mechanical 2-tuple -> 3-tuple signature fixes). This file covers:

- _entry_threshold_crossed's core cases (never crosses / does cross / can't
  determine), including the open_check paired-review fix (checks bar Open
  too, not just Close).
- run_check's guard against auto-explain clobbering a human-authored reason
  (the HIGH bug the paired Opus review found and fixed same session).
"""
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db
from scripts.coverage_check import _entry_threshold_crossed, run_check

TICKER = 'TEST_COVERAGE_THRESH'


@pytest.fixture
def isolated_db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    db.ensure_tables()
    yield db
    os.unlink(tmp_db.name)


def _write_price_csv(check_date='2025-01-07', check_bars=None):
    """Prior daily closes (2025-01-02..2025-01-06, business days) are fixed so a
    window=3 rolling SMA/Std as of the day before check_date is always
    sma=100, std=2 -> entry threshold at z_score_threshold=1.0 is price<=98.
    check_bars: list of (hour, close, open[, low]) tuples for check_date itself --
    low defaults to min(close, open) when omitted, matching real OHLC data where
    Low is always <= both Close and Open."""
    rows = []
    for d, close in [('2025-01-02', 100.0), ('2025-01-03', 102.0), ('2025-01-06', 98.0)]:
        rows.append((f"{d} 09:30:00", close, close, close))
    for bar in (check_bars or []):
        hour, close, open_ = bar[0], bar[1], bar[2]
        low = bar[3] if len(bar) > 3 else min(close, open_)
        rows.append((f"{check_date} {hour:02d}:30:00", close, open_, low))
    df = pd.DataFrame(rows, columns=['Datetime', 'Close', 'Open', 'Low']).set_index('Datetime')
    path = signals_config.RESEARCH_DIR / f"{TICKER}_1h.csv"
    df.to_csv(path)
    return path


@pytest.fixture(autouse=True)
def _cleanup_csv():
    yield
    path = signals_config.RESEARCH_DIR / f"{TICKER}_1h.csv"
    path.unlink(missing_ok=True)


def _node(entry_timing='close'):
    return dict(strategy='TrailingBothZScoreBreakout', window=3, z_score_threshold=1.0,
                entry_timing=entry_timing)


def test_entry_threshold_crossed_false_when_price_never_breaches(isolated_db):
    _write_price_csv(check_bars=[(9, 99.0, 99.0), (10, 99.5, 99.5), (14, 99.2, 99.2)])
    assert _entry_threshold_crossed(TICKER, _node(), '2025-01-07') is False


def test_entry_threshold_crossed_true_on_close_breach(isolated_db):
    # hour 9 and 14 are the only bars the live daemon's 2 real signal windows (10:25-10:40,
    # 15:25-15:40) actually correspond to -- a breach on any other hour was never real
    # evidence the daemon could have seen it (fixed 2026-08-14, see coverage_check.py).
    _write_price_csv(check_bars=[(9, 97.0, 97.0)])  # 97 <= 98
    assert _entry_threshold_crossed(TICKER, _node(), '2025-01-07') is True


def test_entry_threshold_crossed_true_on_low_breach_close_never_crosses(isolated_db):
    # Close stays above threshold (99 > 98) but Low dips through it (97.5 <= 98) --
    # a continuous live poll during the real signal window could have seen that dip.
    _write_price_csv(check_bars=[(9, 99.0, 99.0, 97.5)])
    assert _entry_threshold_crossed(TICKER, _node(), '2025-01-07') is True


def test_entry_threshold_crossed_checks_open_for_open_check_node(isolated_db):
    # Close never breaches (99 > 98) but the bar's Open does (97 <= 98).
    _write_price_csv(check_bars=[(9, 99.0, 97.0)])
    assert _entry_threshold_crossed(TICKER, _node(entry_timing='open_check'), '2025-01-07') is True


def test_entry_threshold_crossed_ignores_open_for_close_only_node(isolated_db):
    # Same bar as above, but entry_timing='close' -- the live daemon never
    # evaluates this node's Open, so a bare Open-side breach must not count.
    # Low pinned to Close (99, non-breaching) so this isolates Open specifically,
    # not just relying on the min(close,open) default.
    _write_price_csv(check_bars=[(9, 99.0, 97.0, 99.0)])
    assert _entry_threshold_crossed(TICKER, _node(entry_timing='close'), '2025-01-07') is False


def test_entry_threshold_crossed_none_when_no_price_cache(isolated_db):
    assert _entry_threshold_crossed('TEST_COVERAGE_THRESH_NO_CSV', _node(), '2025-01-07') is None


def test_entry_threshold_crossed_none_for_unsupported_strategy(isolated_db):
    _write_price_csv(check_bars=[(9, 97.0, 97.0)])
    node = dict(strategy='NotARealStrategy', window=3, z_score_threshold=1.0, entry_timing='close')
    assert _entry_threshold_crossed(TICKER, node, '2025-01-07') is None


def test_entry_threshold_crossed_none_when_no_prior_history(isolated_db):
    # check_date itself is the only day with data -- no prior day to derive sma/std from.
    _write_price_csv(check_bars=[])  # no prior rows written at all
    df = pd.DataFrame([('2025-01-07 09:30:00', 97.0, 97.0)], columns=['Datetime', 'Close', 'Open']).set_index('Datetime')
    df.to_csv(signals_config.RESEARCH_DIR / f"{TICKER}_1h.csv")
    assert _entry_threshold_crossed(TICKER, _node(), '2025-01-07') is None


def _make_scenario(node_id):
    db.add_scenario_expectation(
        'sk_price_action', 'expected happy path', 'daily', 'trade_lifecycle',
        ticker=TICKER, node_id=node_id, check_params='{"expect_exit_reason": ["WIN", "LOSS"]}',
    )


def _add_real_node():
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'canary', window=3, take_profit=1.0,
                stop_loss=1.0, max_hold_hours=48, state='live', account='ira',
                z_score_threshold=1.0, entry_timing='close')
    with db._conn() as c:
        # added_at set well before this file's fixed check_date (2025-01-07) -- run_check's
        # node-predates-check_date guard (2026-08-13, closes the FAS/FAZ backfill-artifact bug
        # shape) would otherwise skip every scenario here, since add_node's real default
        # added_at is "now" (test run time), which is always after any 2025 fixture date.
        c.execute("UPDATE watch_list SET added_at='2025-01-01 00:00:00' WHERE ticker=?", (TICKER,))
        c.commit()
        row = c.execute("SELECT * FROM watch_list WHERE ticker=?", (TICKER,)).fetchone()
    return dict(row)


def test_run_check_auto_explains_no_activity_when_threshold_never_crossed(isolated_db):
    node = _add_real_node()
    _make_scenario(node['id'])
    _write_price_csv(check_bars=[(9, 99.0, 99.0), (14, 99.5, 99.5)])  # never breaches 98

    results = run_check('2025-01-07')

    dev = db.get_deviations()
    assert len(dev) == 1
    assert dev[0]['reason_by'] == 'system'
    assert 'never crossed its entry threshold' in dev[0]['reason']
    assert db.get_deviations(unexplained_only=True) == []
    assert results[0]['auto_explained'] is True


def test_run_check_does_not_auto_explain_when_threshold_actually_crossed(isolated_db):
    """A real bug shape: price DID cross but no trade resulted -- must stay an
    unexplained ticket, never auto-excused."""
    node = _add_real_node()
    _make_scenario(node['id'])
    _write_price_csv(check_bars=[(9, 97.0, 97.0)])  # breaches 98 -- crossed=True

    run_check('2025-01-07')

    dev = db.get_deviations()
    assert len(dev) == 1
    assert dev[0]['reason'] is None
    assert len(db.get_deviations(unexplained_only=True)) == 1


def test_run_check_price_action_auto_explain_survives_a_rerun_that_still_qualifies(isolated_db):
    """trading_incidents id=9, round 1 (2026-08-15): record_deviation's reset-on-
    'system'-reason branch used to fire for ANY reason_by='system' row, not just
    clear_deviation_if_resolved's 'Auto-resolved:' ones -- so a second same-day
    run_check() call for the SAME still-not-crossed price data would blank out a
    price-action auto-explain ('Auto-verified: ... never crossed entry threshold')
    back to reason=NULL, and since record_deviation can't see no_activity/crossed
    itself, nothing re-explains a rerun whose own auto-explain condition happens to
    land differently that pass (real scenario: intraday price data updates between
    a 7am readiness check and a 4pm EOD outcome check, per CLAUDE.md's coverage-
    report split). Real production symptom: coverage_deviations rows 156/165
    (VOO/FAS, canary_market_buy_exit) stuck at reason=NULL after a rerun, confirmed
    via direct DB query, not inferred from log text."""
    node = _add_real_node()
    _make_scenario(node['id'])
    _write_price_csv(check_bars=[(9, 99.0, 99.0), (14, 99.5, 99.5)])  # never breaches 98

    run_check('2025-01-07')  # first pass: auto-explained ('Auto-verified: ...')
    dev_id = db.get_deviations()[0]['id']
    row = db.get_deviations()[0]
    assert row['reason_by'] == 'system' and row['reason'].startswith('Auto-verified:')

    run_check('2025-01-07')  # second pass, SAME price data -- still doesn't cross

    row = db.get_deviations()[0]
    assert row['id'] == dev_id
    assert row['reason_by'] == 'system' and row['reason'].startswith('Auto-verified:'), (
        f"a same-day rerun that still doesn't qualify must not strand the row at reason=NULL, got {row}")


def test_run_check_price_action_auto_explain_clears_when_rerun_finds_a_real_problem(isolated_db):
    """trading_incidents id=9, round 2 (2026-08-15, paired review): the round-1 fix
    above over-corrected into ALWAYS preserving a stale 'Auto-verified:' reason on
    rerun -- which masks a later pass that finds a real problem (the threshold now
    genuinely crossed, per coverage_check.py's own comment: 'it DID cross but still
    no trade -- a real bug') behind a stale 'nothing to see' explanation, hiding a
    genuine deviation from get_deviations(unexplained_only=True) and the daily
    report. The row must re-open as unexplained once the fact that justified the
    explanation is no longer true -- coverage_check.py owns this (it has the actual
    no_activity/crossed facts; record_deviation structurally can't judge it)."""
    node = _add_real_node()
    _make_scenario(node['id'])
    _write_price_csv(check_bars=[(9, 99.0, 99.0), (14, 99.5, 99.5)])  # never breaches 98

    run_check('2025-01-07')  # first pass: auto-explained ('Auto-verified: ...')
    dev_id = db.get_deviations()[0]['id']
    row = db.get_deviations()[0]
    assert row['reason_by'] == 'system' and row['reason'].startswith('Auto-verified:')

    # Second same-day rerun where price data now genuinely crosses the threshold --
    # the FIRST pass's "never crossed" explanation is no longer true.
    _write_price_csv(check_bars=[(9, 97.0, 97.0)])  # now breaches 98 -- crossed=True
    run_check('2025-01-07')

    row = db.get_deviations()[0]
    assert row['id'] == dev_id
    assert row['reason'] is None and row['reason_by'] is None, (
        f"a rerun that finds the threshold now genuinely crossed must re-open the "
        f"ticket, not leave it masked behind the stale explanation, got {row}")
    assert len(db.get_deviations(unexplained_only=True)) == 1


def test_run_check_never_clobbers_a_human_authored_reason(isolated_db):
    """The HIGH bug the paired Opus review found 2026-08-08: a rerun of the
    daily check for a past date must not silently replace a human's real
    testimony with the generic auto-verified string."""
    node = _add_real_node()
    _make_scenario(node['id'])
    _write_price_csv(check_bars=[(9, 99.0, 99.0)])  # never crosses -- auto-explain eligible

    run_check('2025-01-07')  # first pass: auto-explained by the system
    dev_id = db.get_deviations()[0]['id']
    db.explain_deviation(dev_id, 'confirmed real bug, entry branch never fired')  # human overrides, reason_by='user'

    run_check('2025-01-07')  # rerun same date -- must not clobber the human reason

    row = db.get_deviations()[0]
    assert row['reason'] == 'confirmed real bug, entry branch never fired'
    assert row['reason_by'] == 'user'
