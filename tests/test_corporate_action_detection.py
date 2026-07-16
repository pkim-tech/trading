"""Tests for the corporate-action discontinuity check (signals_helpers.
detect_price_discontinuity), its freeze wiring into compute_buy_signal /
check_sell_condition, and the one-time Slack alert + correction handler."""
import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_compute as compute
from signals_helpers import detect_price_discontinuity
from tests.conftest import make_synthetic_csv, cleanup_csv, fake_position

TICKER = 'TEST_CORP_ACTION'


@pytest.fixture(autouse=True)
def _no_real_slack(monkeypatch, tmp_path):
    """Every test in this file touches check_sell_condition/compute_buy_signal,
    which post to Slack on a detected discontinuity -- stub it out and isolate
    the alert-state file so tests never hit the real workspace or leak state
    between runs (a real live post to #trading happened once before this
    fixture existed, 2026-07-15 -- see conversation_summary.md)."""
    monkeypatch.setattr(compute, '_post_message', lambda *a, **kw: (None, None))
    import signals_helpers
    monkeypatch.setattr(signals_helpers, '_CORP_ACTION_ALERT_PATH', tmp_path / "corp_action_alerts.json")


def test_no_discontinuity_on_ordinary_move():
    assert detect_price_discontinuity(current_price=100.0, reference_price=95.0) is None
    assert detect_price_discontinuity(current_price=100.0, reference_price=115.0) is None


def test_detects_forward_split_exact_ratio():
    # e.g. KORU's real 20:1 split
    ratio = detect_price_discontinuity(current_price=20.0, reference_price=400.0)
    assert ratio == 20.0


def test_detects_forward_split_within_tolerance():
    # real fills are never perfectly clean -- within 3% of a 20:1 ratio should still match
    ratio = detect_price_discontinuity(current_price=23.0488, reference_price=460.976)
    assert round(ratio, 2) == 20.0


def test_detects_reverse_split():
    # e.g. a 1-for-20 reverse split: pre-split reference ~20, post-split current ~400
    ratio = detect_price_discontinuity(current_price=400.0, reference_price=20.0)
    assert ratio == 0.05


def test_large_but_non_round_move_not_flagged():
    # a real leveraged-ETF crash can be huge (ratio 2.63, a 62% drop) but won't
    # land near a clean split ratio (nearest candidates are 2.5 and 3, both >3% away)
    assert detect_price_discontinuity(current_price=38.0, reference_price=100.0) is None


def test_custom_tolerance():
    # ratio is exactly 2.1 -- outside a tight tolerance, inside a loose one
    assert detect_price_discontinuity(current_price=100.0, reference_price=210.0, tolerance=0.02) is None
    assert detect_price_discontinuity(current_price=100.0, reference_price=210.0, tolerance=0.06) == 2.1


def test_none_inputs_dont_crash():
    assert detect_price_discontinuity(None, 100.0) is None
    assert detect_price_discontinuity(100.0, None) is None
    assert detect_price_discontinuity(0, 100.0) is None


def test_check_sell_condition_freezes_on_stale_entry_price(capsys):
    pos = fake_position(TICKER, 'ZScoreBreakout', entry_price=460.976)
    make_synthetic_csv(TICKER, last_close=23.0488)
    df_hourly, _ = compute._load_cache(TICKER)
    reason, price, activated = compute.check_sell_condition(
        pos, current_price=23.0488, now=None, df_hourly=df_hourly
    )
    cleanup_csv(TICKER)
    assert (reason, price, activated) == (None, None, False)
    assert "Possible corporate action" in capsys.readouterr().out


def test_check_sell_condition_normal_when_no_discontinuity(capsys):
    pos = fake_position(TICKER, 'ZScoreBreakout', entry_price=100.0)
    make_synthetic_csv(TICKER, last_close=95.0)
    df_hourly, _ = compute._load_cache(TICKER)
    compute.check_sell_condition(pos, current_price=95.0, now=None, df_hourly=df_hourly)
    cleanup_csv(TICKER)
    assert "Possible corporate action" not in capsys.readouterr().out


def test_compute_buy_signal_freezes_on_discontinuity(capsys):
    from tests.conftest import fake_node
    make_synthetic_csv(TICKER, last_close=100.0)
    node = fake_node(TICKER, 'ZScoreBreakout')
    result = compute.compute_buy_signal(node, price_override=5.0)
    cleanup_csv(TICKER)
    assert result is None
    assert "Possible corporate action" in capsys.readouterr().out


def test_alert_fires_once_not_every_poll(monkeypatch):
    calls = []
    monkeypatch.setattr(compute, '_post_message', lambda *a, **kw: calls.append(a) or (None, None))
    pos = fake_position(TICKER, 'ZScoreBreakout', entry_price=460.976)
    make_synthetic_csv(TICKER, last_close=23.0488)
    df_hourly, _ = compute._load_cache(TICKER)
    compute.check_sell_condition(pos, current_price=23.0488, now=None, df_hourly=df_hourly)
    compute.check_sell_condition(pos, current_price=23.0488, now=None, df_hourly=df_hourly)
    compute.check_sell_condition(pos, current_price=23.0488, now=None, df_hourly=df_hourly)
    cleanup_csv(TICKER)
    assert len(calls) == 1


def test_apply_correction_clears_alert_and_updates_entry_price(monkeypatch, tmp_path):
    import tempfile, os
    import signals_config
    import signals_db
    from signals_helpers import already_alerted_corp_action, mark_corp_action_alerted, clear_corp_action_alert

    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    signals_db.ensure_tables()
    node_ticker = 'TEST_CORP_ACTION_APPLY'
    signals_db.add_node(node_ticker, 'ZScoreBreakout', 'test', window=20, take_profit=10,
                         stop_loss=5, max_hold_hours=56, mode='live')
    node = [n for n in signals_db.get_watchlist() if n['ticker'] == node_ticker][0]
    from datetime import datetime
    now = datetime.now()
    signals_db.open_position(node, signal_price=460.976, signal_time=now, entry_price=460.976, entry_time=now)

    mark_corp_action_alerted(node_ticker)
    assert already_alerted_corp_action(node_ticker) is True

    signals_db.correct_entry_price(node_ticker, 23.0488)
    clear_corp_action_alert(node_ticker)

    pos = signals_db.get_open_position(node_ticker)
    assert round(pos['entry_price'], 4) == 23.0488
    assert already_alerted_corp_action(node_ticker) is False
    os.unlink(tmp_db.name)
