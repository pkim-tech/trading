"""Task #6 (2026-08-19), real incident: a real SOXL/ira position sat with
broker shares=0 and a confirmed-terminal recorded sl_order_id, unresolved
for 1h40min (tripping the node circuit breaker twice), before a human
manually reconciled it. check_live_state_reconciliation is detection-only
by design (automation_principles.md #5) -- defensible for every AMBIGUOUS
mismatch shape, but this one specific combination (broker shares==0 AND the
recorded sl_order_id CONFIRMED terminal, not just absent from the open-
orders list) is an unambiguous, safe signal the position is genuinely
closed. signals_notify._reconcile_auto_close_flat_position auto-closes the
local row in exactly that case; every other mismatch shape is untouched --
covered already by tests/test_fake_broker_reconciliation_reporting_scenario.py."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import signals_config
import signals_db
import schwab_client
import schwab_safety
import signals_notify

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_RECONCILE_AUTO_CLOSE'
_IN_WINDOW_TIME = datetime(2026, 8, 19, 10, 30)


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
    monkeypatch.setattr(schwab_safety, 'NODE_BREAKER_PATH', tmp_path / "schwab_node_breaker_state.json")
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_node_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: _IN_WINDOW_TIME)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    signals_db.ensure_tables()
    yield
    schwab_safety.disengage_kill_switch()
    Path(tmp_db.name).unlink(missing_ok=True)


def _add_node(account='soxl_ira', notional=50_000):
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=2, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=2.0,
                         account=account, starting_notional=notional)
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER and n['account'] == account][0]


def _trade_log_row(ticker):
    with signals_db._conn() as c:
        row = c.execute(
            "SELECT * FROM trade_log WHERE ticker=? ORDER BY id DESC LIMIT 1", (ticker,)
        ).fetchone()
    return dict(row) if row else None


def test_auto_closes_on_confirmed_fill_of_the_recorded_sl_order(env, fake_broker, monkeypatch):
    """The common real-incident shape: the recorded SL order actually FILLED
    (broker confirms it, get_filled_order finds it) but the normal
    fill-detection polls missed it -- auto-close uses the real confirmed
    fill price, not an approximation."""
    node = _add_node()
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    r, buy_oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 40, 10.0)
    fake_broker.force_fill(buy_oid, 10.0)
    pos_id = signals_db.open_position(node, signal_price=10.0, signal_time='2026-08-19 09:30:00',
                                       entry_price=10.0, entry_time='2026-08-19 09:30:00', shares=40)
    sl_oid = fake_broker.seed_resting_order('soxl_ira', TICKER, 'STOP', 'SELL', 40, stop_price=9.80)
    signals_db.set_sl_order_id_by_position(pos_id, sl_oid)

    # The stop genuinely fills at the broker -- normal fill-detection missed it.
    fake_broker.force_fill(sl_oid, 9.75)
    assert schwab_client.get_real_position('soxl_ira', TICKER) == 0

    open_positions = signals_db.get_open_positions()
    signals_notify.check_live_state_reconciliation(open_positions)

    assert signals_db.get_open_position(TICKER) is None, "position should be auto-closed"
    events = signals_db.get_coverage_events(scenario_key='reconciliation_auto_close')
    matches = [e for e in events if e['ticker'] == TICKER]
    assert len(matches) == 1 and matches[0]['result'] == 'closed_via_confirmed_fill', matches

    trade = _trade_log_row(TICKER)
    assert trade['exit_reason'] == 'SL', (
        "an unarmed position's confirmed-fill close must record the real "
        "exit_reason (SL here -- never armed), not a placeholder"
    )
    assert trade['exit_price'] == pytest.approx(9.75)

    generic = signals_db.get_coverage_events(scenario_key='reconciliation_mismatch')
    assert not any(e['ticker'] == TICKER for e in generic), (
        "the confirmed-terminal auto-close case must not ALSO log a generic "
        "shares mismatch -- it replaces that path, not adds to it"
    )


def test_auto_closes_via_price_approximation_when_sl_order_has_no_fill_record(env, fake_broker, monkeypatch):
    """The recorded SL order is confirmed terminal (CANCELED) but never
    shows a fill -- the position closed via some other real mechanism
    (manual sale). Broker still confirms 0 shares, so auto-close still
    fires, but via the price-approximation path, clearly labeled as such."""
    node = _add_node()
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    r, buy_oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 40, 10.0)
    fake_broker.force_fill(buy_oid, 10.0)
    pos_id = signals_db.open_position(node, signal_price=10.0, signal_time='2026-08-19 09:30:00',
                                       entry_price=10.0, entry_time='2026-08-19 09:30:00', shares=40)
    sl_oid = fake_broker.seed_resting_order('soxl_ira', TICKER, 'STOP', 'SELL', 40, stop_price=9.80)
    signals_db.set_sl_order_id_by_position(pos_id, sl_oid)

    # The recorded stop is CANCELED (never filled) -- a human sold manually
    # via a different real order instead.
    fake_broker.orders[sl_oid]['status'] = 'CANCELED'
    r, manual_sell_oid = schwab_client.place_equity_sell('soxl_ira', TICKER, 40, 9.60)
    fake_broker.force_fill(manual_sell_oid, 9.60)
    assert schwab_client.get_real_position('soxl_ira', TICKER) == 0

    open_positions = signals_db.get_open_positions()
    signals_notify.check_live_state_reconciliation(open_positions)

    assert signals_db.get_open_position(TICKER) is None, "position should still be auto-closed"
    events = signals_db.get_coverage_events(scenario_key='reconciliation_auto_close')
    matches = [e for e in events if e['ticker'] == TICKER]
    assert len(matches) == 1 and matches[0]['result'] == 'closed_via_broker_zero_shares_no_fill_found', matches
    assert 'APPROXIMATED' in matches[0]['detail'], (
        "the price-approximation path must be clearly labeled as such in the record"
    )
    trade = _trade_log_row(TICKER)
    assert trade['exit_reason'] == 'RECONCILED', (
        "an approximated close must NOT claim a real exit_reason (SL/TRAIL/TIME) it can't back up -- "
        "must be distinguishable in trade_log itself (not just Slack text), since _last_sale_recovery "
        "reads trade_log directly for the next real order's sizing"
    )


def test_armed_position_auto_close_derives_trail_exit_reason_not_sl(env, fake_broker, monkeypatch):
    """Regression for a real bug caught by paired review, 2026-08-19: once a
    position arms, sl_order_id is REPOINTED to the trailing-sell order's id
    (see _attempt_automated_sell) -- so a confirmed-fill auto-close for an
    ARMED position must derive exit_reason='TRAIL' (order-identity-based,
    mirroring check_sl_order_fills exactly), not hardcode 'SL'."""
    node = _add_node()
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    r, buy_oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 40, 10.0)
    fake_broker.force_fill(buy_oid, 10.0)
    pos_id = signals_db.open_position(node, signal_price=10.0, signal_time='2026-08-19 09:30:00',
                                       entry_price=10.0, entry_time='2026-08-19 09:30:00', shares=40)
    trail_oid = fake_broker.seed_resting_order('soxl_ira', TICKER, 'TRAILING_STOP', 'SELL', 40, trail_offset=1.0)
    signals_db.set_sl_order_id_by_position(pos_id, trail_oid)
    signals_db.update_position_trail_state(pos_id, {
        'trailing': True, 'order_placed': True, 'peak': 11.0, 'exit_order_id': trail_oid,
    })

    # The trailing-sell genuinely fills -- normal fill-detection missed it.
    fake_broker.force_fill(trail_oid, 10.85)
    assert schwab_client.get_real_position('soxl_ira', TICKER) == 0

    open_positions = signals_db.get_open_positions()
    signals_notify.check_live_state_reconciliation(open_positions)

    assert signals_db.get_open_position(TICKER) is None
    trade = _trade_log_row(TICKER)
    assert trade['exit_reason'] == 'TRAIL', (
        f"expected TRAIL (sl_order_id is actually the trailing-sell order for an armed position), "
        f"got {trade['exit_reason']!r}"
    )
    assert trade['exit_price'] == pytest.approx(10.85)


def test_auto_close_declines_when_no_fill_and_no_fresh_quote_available(env, fake_broker, monkeypatch):
    """If neither a fill record NOR a fresh quote is available, auto-close
    must NOT fabricate a price at pos['entry_price'] (a fake breakeven) --
    it falls through to the normal alert-only path instead, same as before
    this feature existed."""
    node = _add_node()
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    r, buy_oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 40, 10.0)
    fake_broker.force_fill(buy_oid, 10.0)
    pos_id = signals_db.open_position(node, signal_price=10.0, signal_time='2026-08-19 09:30:00',
                                       entry_price=10.0, entry_time='2026-08-19 09:30:00', shares=40)
    sl_oid = fake_broker.seed_resting_order('soxl_ira', TICKER, 'STOP', 'SELL', 40, stop_price=9.80)
    signals_db.set_sl_order_id_by_position(pos_id, sl_oid)
    fake_broker.orders[sl_oid]['status'] = 'CANCELED'
    r, manual_sell_oid = schwab_client.place_equity_sell('soxl_ira', TICKER, 40, 9.60)
    fake_broker.force_fill(manual_sell_oid, 9.60)
    assert schwab_client.get_real_position('soxl_ira', TICKER) == 0

    def _boom(ticker):
        raise RuntimeError("simulated quote outage")
    monkeypatch.setattr(schwab_client, 'get_current_price', _boom)

    open_positions = signals_db.get_open_positions()
    signals_notify.check_live_state_reconciliation(open_positions)

    assert signals_db.get_open_position(TICKER) is not None, (
        "must NOT auto-close on a fabricated entry-price breakeven when no real price is available"
    )
    events = signals_db.get_coverage_events(scenario_key='reconciliation_auto_close')
    matches = [e for e in events if e['ticker'] == TICKER]
    assert len(matches) == 1 and matches[0]['result'] == 'skipped_no_price', matches


def test_does_not_auto_close_when_sl_order_status_is_ambiguous(env, fake_broker, monkeypatch):
    """Negative case: broker shows 0 shares, but the recorded sl_order_id's
    status can't be confirmed at all (e.g. the order id is bogus/unknown to
    the broker -- get_order_status returns None, the tri-state 'unconfirmed'
    result). Must fall through to the normal alert-only mismatch path, NOT
    auto-close on an ambiguous signal."""
    node = _add_node()
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    r, buy_oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 40, 10.0)
    fake_broker.force_fill(buy_oid, 10.0)
    pos_id = signals_db.open_position(node, signal_price=10.0, signal_time='2026-08-19 09:30:00',
                                       entry_price=10.0, entry_time='2026-08-19 09:30:00', shares=40)
    # A recorded sl_order_id the fake broker has never heard of -- mirrors
    # get_order_status returning None (unconfirmed), the tri-state case
    # _exit_order_resting must treat as "not safe to trust", not as proof
    # of anything.
    signals_db.set_sl_order_id_by_position(pos_id, 999999999999)

    r, manual_sell_oid = schwab_client.place_equity_sell('soxl_ira', TICKER, 40, 9.60)
    fake_broker.force_fill(manual_sell_oid, 9.60)
    assert schwab_client.get_real_position('soxl_ira', TICKER) == 0

    open_positions = signals_db.get_open_positions()
    signals_notify.check_live_state_reconciliation(open_positions)

    assert signals_db.get_open_position(TICKER) is not None, (
        "must NOT auto-close on an ambiguous/unconfirmed order status"
    )
    auto_close_events = signals_db.get_coverage_events(scenario_key='reconciliation_auto_close')
    assert not any(e['ticker'] == TICKER for e in auto_close_events)
    mismatch_events = signals_db.get_coverage_events(scenario_key='reconciliation_mismatch')
    assert any(e['ticker'] == TICKER and e['result'] == 'shares' for e in mismatch_events), (
        "should fall through to the normal alert-only shares-mismatch path"
    )
