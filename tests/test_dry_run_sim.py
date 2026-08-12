"""dry_run fill synthesis: signals_notify.update_dry_run_buys (entry) and
check_dry_run_sim_sells (exit) -- a dry_run=True account's real order never
gets a real fill event (schwab_client short-circuits before the broker call),
so these mirror paper_trading.py's simulation but write to the real
open_positions/trade_log tables, tagged is_dry_run_sim=1. Follows
tests/test_live_state_reconciliation.py's fixture pattern (real add_node,
account patched to 'roth', a real dry_run=True account)."""
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db
import signals_notify
import schwab_safety
from tests.conftest import make_synthetic_csv, cleanup_csv

TICKER = 'TEST_DRYSIM'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    posted = []
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda *a, **kw: (posted.append(a[0] if a else kw.get('text')), (None, None))[1])

    db.ensure_tables()
    make_synthetic_csv(TICKER, last_close=100.0)
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                stop_loss=1, max_hold_hours=7, state='live',
                trail_buy_pct=1.0, trail_pct=1.0, starting_notional=5000, fixed_sl_override=1.0)
    with db._conn() as c:
        c.execute("UPDATE watch_list SET account = 'roth' WHERE ticker = ?", (TICKER,))  # real ACCOUNTS['roth'].trading_enabled == False
        c.commit()

    yield posted

    cleanup_csv(TICKER)
    tmp_db_path = Path(tmp_db.name)
    if tmp_db_path.exists():
        tmp_db_path.unlink()


def _node():
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]


def _sig(price):
    return {'ticker': TICKER, 'current_price': price, 'z_score': -2.5, 'last_bar': datetime.now()}


def test_dry_run_account_is_actually_dry_run():
    # 'roth' was the dry-run example account until it was activated for real
    # 2026-08-12 (docs/deep_backlog.md) -- 'sep' is the current stand-in,
    # same fix pattern as the 2026-08-11 sweep that already caught this for
    # other tests hardcoding 'roth'.
    assert schwab_safety.ACCOUNTS['sep'].trading_enabled is False


def test_pending_buy_for_dry_run_account_never_fills_without_synthesis(env):
    node = _node()
    db.add_pending_buy(node, _sig(100.0), channel=None, ts=None)
    assert len(db.get_pending_buys()) == 1
    assert db.get_open_positions() == []


def test_update_dry_run_buys_tracks_running_low_without_filling(env, monkeypatch):
    node = _node()
    db.add_pending_buy(node, _sig(100.0), channel=None, ts=None)
    monkeypatch.setattr(signals_notify.compute, '_current_price', lambda t: (98.0, None))
    signals_notify.update_dry_run_buys()
    pending = db.get_pending_buys()
    assert len(pending) == 1
    assert pending[0]['running_low'] == 98.0
    assert db.get_open_positions() == []


def test_update_dry_run_buys_fills_on_bounce_and_opens_real_position_tagged(env, monkeypatch):
    node = _node()
    db.add_pending_buy(node, _sig(100.0), channel=None, ts=None)
    monkeypatch.setattr(signals_notify.compute, '_current_price', lambda t: (90.0, None))
    signals_notify.update_dry_run_buys()
    monkeypatch.setattr(signals_notify.compute, '_current_price', lambda t: (92.0, None))  # >= 1% above 90
    signals_notify.update_dry_run_buys()

    assert db.get_pending_buys() == []
    positions = db.get_open_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos['is_dry_run_sim'] == 1
    assert pos['entry_price'] == 92.0
    assert pos['account'] == 'roth'

    with db._conn() as c:
        row = c.execute("SELECT is_dry_run_sim FROM trade_log WHERE id = ?", (pos['trade_log_id'],)).fetchone()
    assert row['is_dry_run_sim'] == 1


def test_check_dry_run_sim_sells_closes_immediately_without_slack_button(env, monkeypatch):
    node = _node()
    signal_time = datetime.now() - timedelta(hours=1)
    db.open_position(node, signal_price=100.0, signal_time=signal_time, entry_price=100.0,
                      entry_time=signal_time, shares=50, is_dry_run_sim=True)
    pos = db.get_open_positions()[0]

    monkeypatch.setattr(signals_notify.compute, '_current_price', lambda t: (98.0, None))  # -2% > 1% fixed_sl

    from signals_compute import _load_cache
    df_hourly, _ = _load_cache(TICKER)
    last_seen_bar = {pos['wl_id']: df_hourly.index[-1]}  # forces the mid-bar (continuous) branch

    signals_notify.check_dry_run_sim_sells(last_seen_bar, set(), _load_cache)

    assert db.get_open_positions() == []
    with db._conn() as c:
        row = c.execute(
            "SELECT exit_reason, pnl_pct, is_dry_run_sim FROM trade_log WHERE id = ?", (pos['trade_log_id'],)
        ).fetchone()
    assert row['exit_reason'] == 'SL'
    assert row['pnl_pct'] < 0
    assert row['is_dry_run_sim'] == 1


def test_live_account_pending_buy_untouched_by_dry_run_synthesis(env, monkeypatch):
    with db._conn() as c:
        c.execute("UPDATE watch_list SET account = 'soxl_ira' WHERE ticker = ?", (TICKER,))  # real dry_run=False account
        c.commit()
    node = _node()
    db.add_pending_buy(node, _sig(100.0), channel=None, ts=None)
    monkeypatch.setattr(signals_notify.compute, '_current_price', lambda t: (92.0, None))
    signals_notify.update_dry_run_buys()

    assert len(db.get_pending_buys()) == 1  # untouched -- a real account's fill is left to the real detection path
    assert db.get_open_positions() == []


def test_update_dry_run_buys_does_not_double_fill_or_reclear_stale_pending_row(env, monkeypatch):
    """Opus review 2026-07-26: _fill_dry_run_buy must check open_position()'s
    return value -- a duplicate fill attempt (e.g. a leftover pending_buys row
    for a node that already has an open position) must not post a second false
    '[DRY RUN] would have filled' message or a second false coverage_events row."""
    node = _node()
    signal_time = datetime.now()
    db.open_position(node, signal_price=100.0, signal_time=signal_time, entry_price=100.0,
                      entry_time=signal_time, shares=50, is_dry_run_sim=True)
    db.add_pending_buy(node, _sig(100.0), channel=None, ts=None)  # stale/leftover row, node already open

    monkeypatch.setattr(signals_notify.compute, '_current_price', lambda t: (90.0, None))
    signals_notify.update_dry_run_buys()  # tracks running_low=90, doesn't fill yet
    posted = env
    posted.clear()
    monkeypatch.setattr(signals_notify.compute, '_current_price', lambda t: (92.0, None))  # >= 1% above 90 -> would trigger a fill
    signals_notify.update_dry_run_buys()

    assert db.get_pending_buys() == []  # dropped, but...
    assert len(db.get_open_positions()) == 1  # ...no second position opened
    assert not any('would have filled' in p for p in posted)  # ...and no false fill alert


def test_update_dry_run_buys_market_buy_eligible_node_fills_immediately(env, monkeypatch):
    """Non-trailing (market-buy-eligible) node: no bounce-fill phase, should fill
    on the very next poll at whatever price is current."""
    db.add_node(TICKER, 'TrailingExitZScoreBreakout', 'test2', window=20, take_profit=7,
                stop_loss=1, max_hold_hours=7, state='live', trail_pct=1.0, starting_notional=5000,
                fixed_sl_override=1.0)
    with db._conn() as c:
        c.execute("UPDATE watch_list SET account = 'roth' WHERE ticker = ? AND version = 'test2'", (TICKER,))
        c.commit()
    node = [n for n in db.get_watchlist() if n['ticker'] == TICKER and n['version'] == 'test2'][0]
    assert db._is_trailing_buy(node) is False

    db.add_pending_buy(node, _sig(100.0), channel=None, ts=None)
    monkeypatch.setattr(signals_notify.compute, '_current_price', lambda t: (100.0, None))
    signals_notify.update_dry_run_buys()

    assert db.get_pending_buys() == []
    positions = [p for p in db.get_open_positions() if p['wl_id'] == node['id']]
    assert len(positions) == 1
    assert positions[0]['is_dry_run_sim'] == 1
    assert positions[0]['entry_price'] == 100.0


def test_update_dry_run_buys_skips_wl_id_less_pending_row(env, monkeypatch):
    """A pending_buys row predating the wl_id migration (or otherwise missing
    it) must be left alone, not trusted via the frozen node_json snapshot --
    update_pending_buy_running_low/clear_pending_buy_by_wl_id both key on
    wl_id, so a NULL wl_id row would otherwise never clear and re-fire a
    synthetic fill (and a false Slack alert) every poll forever."""
    node = _node()
    db.add_pending_buy(node, _sig(100.0), channel=None, ts=None)
    with db._conn() as c:
        c.execute("UPDATE pending_buys SET wl_id = NULL WHERE ticker = ?", (TICKER,))
        c.commit()

    monkeypatch.setattr(signals_notify.compute, '_current_price', lambda t: (92.0, None))
    posted = env
    posted.clear()
    signals_notify.update_dry_run_buys()

    assert len(db.get_pending_buys()) == 1  # untouched
    assert db.get_open_positions() == []
    assert posted == []


def test_closed_today_excludes_dry_run_sim_trades(env):
    node = _node()
    now = datetime.now()
    db.open_position(node, signal_price=100.0, signal_time=now, entry_price=100.0,
                      entry_time=now, shares=10, is_dry_run_sim=True)
    pos = db.get_open_positions()[0]
    db.close_position(pos['id'], exit_signal_price=101.0, exit_price=101.0,
                       exit_time=now, exit_reason='TP')

    assert db.closed_today(TICKER) is False  # a simulated exit must not warn/block a real same-day re-buy


def test_last_sale_recovery_excludes_dry_run_sim_trades(env):
    import signals_helpers
    node = _node()
    now = datetime.now()
    db.open_position(node, signal_price=100.0, signal_time=now, entry_price=100.0,
                      entry_time=now, shares=10, is_dry_run_sim=True)
    pos = db.get_open_positions()[0]
    db.close_position(pos['id'], exit_signal_price=101.0, exit_price=101.0,
                       exit_time=now, exit_reason='TP')

    # No real (non-sim) trade history exists -- must fall back to starting_notional,
    # never size off the simulated trade's proceeds (101.0 * 10 = 1010).
    assert signals_helpers._last_sale_recovery(node) == node['starting_notional']


def test_scan_pinned_exit_arm_skips_dry_run_sim_position(env, monkeypatch):
    """Opus review 2026-07-26: the pinned bar-boundary exit-arm check was missed
    by the original is_dry_run_sim skip guard -- it shares last_seen_bar with
    check_dry_run_sim_sells (corrupting its bar-close detection) and fires real
    notify_trailing_activated/notify_sell_signal Slack flows for a synthetic
    position with no real order behind it."""
    import active_signals
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    node = _node()
    signal_time = datetime.now() - timedelta(hours=1)
    db.open_position(node, signal_price=100.0, signal_time=signal_time, entry_price=100.0,
                      entry_time=signal_time, shares=50, is_dry_run_sim=True)
    pos = db.get_open_positions()[0]

    posted = env
    posted.clear()
    sell_alerted = set()
    last_seen_bar = {}
    active_signals._scan_pinned_exit_arm([pos], sell_alerted, last_seen_bar)

    assert last_seen_bar == {}  # untouched -- must not consume check_dry_run_sim_sells' bar-close marker
    assert sell_alerted == set()
    assert posted == []  # no real notify_trailing_activated/notify_sell_signal Slack flow
    assert db.get_open_positions() == [pos]  # still open -- untouched by this loop
