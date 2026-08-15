"""fake_broker scenario for the abnormal-drift liquidity-signal alert
(docs/backlog_cache.md's 2026-08-14 'abnormal-drift liquidity-signal alert'
item, design settled that evening; threshold calibrated 2026-08-15 off
docs/research_log.md's real drift-distribution audit).

signals_db.check_abnormal_drift is invoked from the single real chokepoint
every entry/exit fill passes through -- open_position()/close_position() --
so these tests drive it through those exact real functions (not a direct
unit call), matching this project's fake_broker convention of exercising the
real production control flow rather than the function in isolation."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import signals_config
import signals_db
import schwab_safety

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_ABNORMAL_DRIFT_SCENARIO'
SMALL_TICKER = 'TEST_ABNORMAL_DRIFT_SMALL_SCENARIO'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    monkeypatch.setattr(schwab_safety, 'STATE_PATH', tmp_path / "schwab_order_counts.json")
    monkeypatch.setattr(schwab_safety, 'KILL_SWITCH_PATH', tmp_path / "schwab_kill_switch.json")
    monkeypatch.setattr(schwab_safety, 'TICKER_AUTOMATION_PATH', tmp_path / "schwab_ticker_automation.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTOMATION_PATH', tmp_path / "schwab_node_automation.json")
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    posted = []
    import schwab_client
    monkeypatch.setattr(schwab_client, '_post_message', lambda *a, **kw: (posted.append((a, kw)), (None, None))[1])

    signals_db.ensure_tables()
    # Real capital-at-stake node: state='live', soxl_ira (trading_enabled=1 by
    # default seed), starting_notional above CAPITAL_AT_STAKE_THRESHOLD ($5,000
    # default -- see signals_config.CAPITAL_AT_STAKE_THRESHOLD).
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=1, state='live', account='soxl_ira',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         starting_notional=10_000)
    # Below-threshold node, same account -- has_capital_at_stake must gate this out.
    signals_db.add_node(SMALL_TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=1, state='live', account='soxl_ira',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         starting_notional=500)

    yield posted

    Path(tmp_db.name).unlink(missing_ok=True)


def _node(ticker=TICKER):
    return [n for n in signals_db.get_watchlist() if n['ticker'] == ticker][0]


def test_entry_drift_above_threshold_alerts_and_logs_coverage_event(env, fake_broker):
    posted = env
    node = _node()
    entry_time = datetime(2026, 8, 15, 10, 30, 0)
    # signal_price=50, entry_price=54 -> +8% drift, well above the 3% threshold.
    opened = signals_db.open_position(node, signal_price=50.0, signal_time=entry_time,
                                       entry_price=54.0, entry_time=entry_time, shares=100)
    assert opened

    events = signals_db.get_coverage_events(scenario_key='abnormal_drift_alert')
    matches = [e for e in events if e['ticker'] == TICKER]
    assert len(matches) == 1
    assert matches[0]['result'] == 'alerted'
    assert 'side=entry' in matches[0]['detail']

    assert len(posted) == 1
    assert TICKER in posted[0][0][0]
    assert 'entry' in posted[0][0][0]


def test_exit_drift_above_threshold_alerts_and_logs_coverage_event(env, fake_broker):
    posted = env
    node = _node()
    entry_time = datetime(2026, 8, 15, 5, 0, 0)
    signals_db.open_position(node, signal_price=50.0, signal_time=entry_time,
                              entry_price=50.0, entry_time=entry_time, shares=100)
    pos = signals_db.get_open_position(TICKER)
    posted.clear()  # drop the entry-side alert (drift=0%, won't have fired, but be explicit)

    # exit_signal_price=50, exit_price=45 -> -10% drift, well above threshold.
    closed = signals_db.close_position(pos['id'], exit_signal_price=50.0, exit_price=45.0,
                                        exit_time=datetime(2026, 8, 15, 11, 0, 0), exit_reason='SL')
    assert closed

    events = signals_db.get_coverage_events(scenario_key='abnormal_drift_alert')
    matches = [e for e in events if e['ticker'] == TICKER and 'side=exit' in e['detail']]
    assert len(matches) == 1
    assert matches[0]['result'] == 'alerted'

    assert len(posted) == 1
    assert 'exit' in posted[0][0][0]


def test_drift_below_threshold_does_not_alert(env, fake_broker):
    posted = env
    node = _node()
    entry_time = datetime(2026, 8, 15, 10, 30, 0)
    # signal_price=50, entry_price=50.5 -> +1% drift, below the 3% threshold.
    signals_db.open_position(node, signal_price=50.0, signal_time=entry_time,
                              entry_price=50.5, entry_time=entry_time, shares=100)

    events = signals_db.get_coverage_events(scenario_key='abnormal_drift_alert')
    assert [e for e in events if e['ticker'] == TICKER] == []
    assert posted == []


def test_below_capital_at_stake_threshold_node_never_alerts(env, fake_broker):
    posted = env
    node = _node(SMALL_TICKER)
    entry_time = datetime(2026, 8, 15, 10, 30, 0)
    # Same +8% drift as the real-alert test, but this node's starting_notional
    # ($500) is below CAPITAL_AT_STAKE_THRESHOLD -- has_capital_at_stake must
    # gate this out before the threshold check even runs.
    signals_db.open_position(node, signal_price=50.0, signal_time=entry_time,
                              entry_price=54.0, entry_time=entry_time, shares=10)

    events = signals_db.get_coverage_events(scenario_key='abnormal_drift_alert')
    assert [e for e in events if e['ticker'] == SMALL_TICKER] == []
    assert posted == []


def test_dry_run_sim_fill_never_alerts(env, fake_broker):
    posted = env
    node = _node()
    entry_time = datetime(2026, 8, 15, 10, 30, 0)
    signals_db.open_position(node, signal_price=50.0, signal_time=entry_time,
                              entry_price=54.0, entry_time=entry_time, shares=100,
                              is_dry_run_sim=True)

    events = signals_db.get_coverage_events(scenario_key='abnormal_drift_alert')
    assert [e for e in events if e['ticker'] == TICKER] == []
    assert posted == []


def test_third_same_day_breach_suppressed_but_still_logged(env, fake_broker):
    """Escalation cap: at most 2 Slack posts per ticker per day. A 3rd same-day
    breach still writes a coverage_event (result='suppressed_daily_cap') so the
    data isn't lost -- it just doesn't re-nag Slack a 3rd time."""
    posted = env
    node = _node()

    # One real (open+close) fill with a >3% drift on BOTH sides -- entry breach
    # is alert #1, exit breach is alert #2, exhausting the 2/day cap in one round.
    entry_time = datetime(2026, 8, 15, 10, 30, 1)
    signals_db.open_position(node, signal_price=50.0, signal_time=entry_time,
                              entry_price=54.0, entry_time=entry_time, shares=100)
    pos = signals_db.get_open_position(TICKER)
    signals_db.close_position(pos['id'], exit_signal_price=50.0, exit_price=54.0,
                               exit_time=entry_time, exit_reason='TIME')

    events = signals_db.get_coverage_events(scenario_key='abnormal_drift_alert', limit=1000)
    my_events = [e for e in events if e['ticker'] == TICKER]
    assert len(my_events) == 2
    assert all(e['result'] == 'alerted' for e in my_events)
    assert len(posted) == 2

    # 3rd same-day breach: cap already exhausted -- suppressed, but still logged.
    entry_time2 = datetime(2026, 8, 15, 10, 30, 2)
    signals_db.open_position(node, signal_price=50.0, signal_time=entry_time2,
                              entry_price=54.0, entry_time=entry_time2, shares=100)

    events = signals_db.get_coverage_events(scenario_key='abnormal_drift_alert', limit=1000)
    my_events = [e for e in events if e['ticker'] == TICKER]
    assert len(my_events) == 3
    assert my_events[0]['result'] == 'suppressed_daily_cap'  # most recent, DESC order
    assert len(posted) == 2  # unchanged -- no 3rd Slack post
