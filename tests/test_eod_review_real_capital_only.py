"""Task #5 (2026-08-19), user: "at EOD I'm only going to care about real
positions." build_eod_scenario_review used to mix real + paper positions +
canary scenario checks into one 16:05 ET push, burying real signal (P&L,
incidents, unexplained deviations). Scope: the canary scenario-check
section and the paper activity section are dropped entirely (fully covered
elsewhere -- Coverage Report for canary/control, paper carries zero real
capital by definition); the remaining live section and build_tomorrow_plan
are both filtered to has_capital_at_stake nodes only."""
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import signals_notify

REAL_TICKER = 'TEST_EOD_REAL'
SUB_TICKER = 'TEST_EOD_SUBTHRESHOLD'


@pytest.fixture
def env(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    signals_db.ensure_tables()
    monkeypatch.setattr(signals_notify, '_coverage_is_trading_day', lambda d: True)
    monkeypatch.setattr(signals_notify, 'has_capital_at_stake',
                         lambda node: node.get('ticker') == REAL_TICKER)
    posted = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda text, *a, **kw: posted.append(text))
    yield posted
    Path(tmp_db.name).unlink()


def _add_node(ticker, account='ira'):
    signals_db.add_node(ticker, 'TrailingBothZScoreBreakout', 'v5', window=10, take_profit=16.0,
                         stop_loss=2, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=2.0,
                         account=account, starting_notional=50_000)
    return [n for n in signals_db.get_watchlist() if n['ticker'] == ticker][0]


def test_build_tomorrow_plan_includes_only_capital_at_stake_nodes(env):
    real_node = _add_node(REAL_TICKER)
    sub_node = _add_node(SUB_TICKER)
    entry_time = (datetime.now() - timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')
    signals_db.open_position(real_node, signal_price=10.0, signal_time=entry_time,
                              entry_price=10.0, entry_time=entry_time, shares=10)
    signals_db.open_position(sub_node, signal_price=10.0, signal_time=entry_time,
                              entry_price=10.0, entry_time=entry_time, shares=10)

    text = signals_notify.build_tomorrow_plan()

    assert REAL_TICKER in text
    assert SUB_TICKER not in text
    assert '_Canary_' not in text
    assert '_Paper_' not in text


def test_eod_review_drops_canary_and_paper_sections(env):
    """No canary scenario-check text, no Paper section header at all --
    even with zero real activity, the report must render without either."""
    text_or_result = signals_notify.build_eod_scenario_review('2026-08-19')
    posted = env
    assert len(posted) == 1
    text = posted[0]
    assert '_Scenario checks_' not in text
    assert 'canary' not in text.lower()
    assert '_Paper_' not in text
    assert '_Live_' in text


def test_eod_review_includes_real_capital_activity_excludes_subthreshold(env):
    real_node = _add_node(REAL_TICKER)
    sub_node = _add_node(SUB_TICKER)
    entry_time = '2026-08-19 09:30:00'
    exit_time = '2026-08-19 14:00:00'
    for node in (real_node, sub_node):
        pid = signals_db.open_position(node, signal_price=10.0, signal_time=entry_time,
                                        entry_price=10.0, entry_time=entry_time, shares=10)
        signals_db.close_position(pid, exit_signal_price=10.5, exit_price=10.5,
                                   exit_time=exit_time, exit_reason='SL')

    signals_notify.build_eod_scenario_review('2026-08-19')
    posted = env
    text = posted[-1]
    assert REAL_TICKER in text
    assert SUB_TICKER not in text
