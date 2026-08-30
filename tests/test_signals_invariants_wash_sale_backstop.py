"""Tests for signals_invariants.check_no_wash_sale_risk_backstop -- a DETECT-ONLY
backstop for the IRS wash-sale-into-IRA rule (Rev. Rul. 2008-5): a real
brokerage-account loss on a ticker followed by a same-security repurchase in a
tax-advantaged account within 61 days (30 before + 30 after the loss sale)
permanently disallows that loss. Real incident this backstops:
trading_incidents #2 (GDXU/soxl_ira, 2026-08-23)."""
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db
import schwab_safety
import signals_invariants


@pytest.fixture
def env(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    db.ensure_tables()
    schwab_safety.reload_accounts()
    yield
    Path(tmp_db.name).unlink()


def _add_brokerage_loss(ticker, exit_time, entry_time=None, is_dry_run_sim=0):
    """Insert a real closed losing trade_log row in the taxable brokerage
    account, independent of any watch_list node (mirrors the real GDXU
    incident, where the loss-side trade was in a different account/node than
    the later IRA repurchase)."""
    entry_time = entry_time or (exit_time - timedelta(days=1))
    with db._conn() as c:
        c.execute("""
            INSERT INTO trade_log
                (ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
                 signal_price, signal_time, entry_price, entry_time, entry_drift_pct,
                 exit_signal_price, exit_price, exit_time, exit_drift_pct, pnl_pct, exit_reason,
                 account, is_dry_run_sim)
            VALUES (?, 'TrailingBothZScoreBreakout', 'v5', 10, 30, 2, 48,
                    100.0, ?, 100.0, ?, 0.0,
                    90.0, 90.0, ?, 0.0, -10.0, 'SL',
                    'brokerage', ?)
        """, (ticker, entry_time.strftime('%Y-%m-%d %H:%M:%S'), entry_time.strftime('%Y-%m-%d %H:%M:%S'),
              exit_time.strftime('%Y-%m-%d %H:%M:%S'), is_dry_run_sim))
        c.commit()


def _add_live_node_with_real_fill(ticker, account, entry_time, closed=True, is_dry_run_sim=0):
    """Create a live tax-advantaged node and give it a real buy fill (either a
    closed trade_log row via log_trade_entry/log_trade_exit, or a still-open
    open_positions row) at entry_time."""
    db.add_node(ticker=ticker, strategy='TrailingBothZScoreBreakout', version='v5', window=10,
                take_profit=30, stop_loss=2, max_hold_hours=48, state='live',
                account=account, fixed_sl_override=15)
    with db._conn() as c:
        node_id = c.execute(
            "SELECT id FROM watch_list WHERE ticker=? AND account=? ORDER BY id DESC LIMIT 1",
            (ticker, account)
        ).fetchone()[0]
    node = db.get_watch_list_node_by_id(node_id)
    trade_id = db.log_trade_entry(node, signal_price=100.0, signal_time=entry_time,
                                   entry_price=100.0, entry_time=entry_time,
                                   is_dry_run_sim=bool(is_dry_run_sim))
    if closed:
        db.log_trade_exit(trade_id, exit_signal_price=110.0, exit_price=110.0,
                           exit_time=entry_time + timedelta(hours=2), exit_reason='TP',
                           entry_price=100.0)
    else:
        with db._conn() as c:
            c.execute("""
                INSERT INTO open_positions
                    (ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
                     signal_price, signal_time, entry_price, entry_time, trade_log_id, is_dry_run_sim, wl_id, account)
                VALUES (?, 'TrailingBothZScoreBreakout', 'v5', 10, 30, 2, 48,
                        100.0, ?, 100.0, ?, ?, ?, ?, ?)
            """, (ticker, entry_time.strftime('%Y-%m-%d %H:%M:%S'), entry_time.strftime('%Y-%m-%d %H:%M:%S'),
                  trade_id, is_dry_run_sim, node_id, account))
            c.commit()
    return node_id


def test_brokerage_loss_then_soxl_ira_repurchase_in_window_flagged(env):
    """Real GDXU shape: brokerage loss, then a same-security repurchase in a
    tax-advantaged account within 30 days after."""
    loss_exit = datetime(2026, 7, 6, 15, 30, 0)
    _add_brokerage_loss('GDXU', loss_exit)
    repurchase_time = loss_exit + timedelta(days=18)  # within 30-day after window
    node_id = _add_live_node_with_real_fill('GDXU', 'soxl_ira', repurchase_time)
    violations = signals_invariants.check_no_wash_sale_risk_backstop()
    assert len(violations) == 1
    assert 'GDXU' in violations[0]
    assert f'wl_id={node_id}' in violations[0]


def test_repurchase_before_loss_within_30_days_before_also_flagged(env):
    """Window is CENTERED on the loss (30 before + 30 after), not just
    30-forward -- a repurchase shortly before the loss sale is also risky."""
    loss_exit = datetime(2026, 7, 6, 15, 30, 0)
    _add_brokerage_loss('GDXU', loss_exit)
    repurchase_time = loss_exit - timedelta(days=10)
    node_id = _add_live_node_with_real_fill('GDXU', 'ira', repurchase_time)
    violations = signals_invariants.check_no_wash_sale_risk_backstop()
    assert len(violations) == 1
    assert 'GDXU' in violations[0]


def test_repurchase_outside_61_day_window_not_flagged(env):
    loss_exit = datetime(2026, 7, 6, 15, 30, 0)
    _add_brokerage_loss('GDXU', loss_exit)
    repurchase_time = loss_exit + timedelta(days=45)  # outside window
    _add_live_node_with_real_fill('GDXU', 'soxl_ira', repurchase_time)
    assert signals_invariants.check_no_wash_sale_risk_backstop() == []


def test_dry_run_sim_loss_ignored(env):
    """A simulated (is_dry_run_sim=1) brokerage loss row must not be treated
    as a real tax event."""
    loss_exit = datetime(2026, 7, 6, 15, 30, 0)
    _add_brokerage_loss('GDXU', loss_exit, is_dry_run_sim=1)
    repurchase_time = loss_exit + timedelta(days=18)
    _add_live_node_with_real_fill('GDXU', 'soxl_ira', repurchase_time)
    assert signals_invariants.check_no_wash_sale_risk_backstop() == []


def test_dry_run_sim_repurchase_ignored(env):
    """A simulated fill on the tax-advantaged side must not be treated as a
    real replacement-share purchase."""
    loss_exit = datetime(2026, 7, 6, 15, 30, 0)
    _add_brokerage_loss('GDXU', loss_exit)
    repurchase_time = loss_exit + timedelta(days=18)
    _add_live_node_with_real_fill('GDXU', 'soxl_ira', repurchase_time, is_dry_run_sim=1)
    assert signals_invariants.check_no_wash_sale_risk_backstop() == []


def test_open_position_real_fill_also_checked(env):
    """A still-open real position (not yet closed in trade_log) must still be
    checked -- the tax event is the buy fill, not the eventual close."""
    loss_exit = datetime(2026, 7, 6, 15, 30, 0)
    _add_brokerage_loss('GDXU', loss_exit)
    repurchase_time = loss_exit + timedelta(days=18)
    node_id = _add_live_node_with_real_fill('GDXU', 'soxl_ira', repurchase_time, closed=False)
    violations = signals_invariants.check_no_wash_sale_risk_backstop()
    assert len(violations) == 1
    assert f'wl_id={node_id}' in violations[0]


def test_live_node_with_no_real_fill_yet_not_flagged(env):
    """A live node that's never actually filled has no tax event yet -- must
    not be flagged just for existing in a tax-advantaged account."""
    loss_exit = datetime(2026, 7, 6, 15, 30, 0)
    _add_brokerage_loss('GDXU', loss_exit)
    db.add_node(ticker='GDXU', strategy='TrailingBothZScoreBreakout', version='v5', window=10,
                take_profit=30, stop_loss=2, max_hold_hours=48, state='live',
                account='soxl_ira', fixed_sl_override=15)
    assert signals_invariants.check_no_wash_sale_risk_backstop() == []


def test_brokerage_side_node_not_flagged(env):
    """A node in the taxable brokerage account itself is never in scope --
    only tax-advantaged repurchases matter."""
    loss_exit = datetime(2026, 7, 6, 15, 30, 0)
    _add_brokerage_loss('GDXU', loss_exit)
    repurchase_time = loss_exit + timedelta(days=18)
    _add_live_node_with_real_fill('GDXU', 'brokerage', repurchase_time)
    assert signals_invariants.check_no_wash_sale_risk_backstop() == []


def test_window_is_calendar_date_based_not_raw_timedelta(env):
    """Paired Opus review (2026-08-29/30), both independent-cold and
    contextual reviewers, reproduced an off-by-one: comparing raw
    (entry_dt - loss_dt).days (which truncates toward zero) instead of
    calendar-date difference made the window time-of-day dependent and
    asymmetric -- a fill exactly 30 CALENDAR days before a loss (with an
    earlier time-of-day) evaluated to -31 days and was wrongly excluded
    (false negative), while a fill 31 calendar days after with an earlier
    time-of-day evaluated to 30 days and was wrongly included (false
    positive). This test pins the exact reproduction case from that review:
    a loss at 15:30 and a repurchase exactly 30 calendar days earlier at
    09:30 must be flagged (squarely in the 30-days-before half of the
    window)."""
    loss_exit = datetime(2026, 7, 6, 15, 30, 0)
    _add_brokerage_loss('GDXU', loss_exit)
    repurchase_time = datetime(2026, 6, 6, 9, 30, 0)  # exactly 30 calendar days before, earlier time-of-day
    node_id = _add_live_node_with_real_fill('GDXU', 'soxl_ira', repurchase_time)
    violations = signals_invariants.check_no_wash_sale_risk_backstop()
    assert len(violations) == 1
    assert f'wl_id={node_id}' in violations[0]


def test_repurchase_31_calendar_days_after_not_flagged(env):
    """Mirror boundary case: 31 calendar days after (with an earlier
    time-of-day than the loss) must NOT be flagged -- outside the 30-day
    window."""
    loss_exit = datetime(2026, 7, 6, 15, 30, 0)
    _add_brokerage_loss('GDXU', loss_exit)
    repurchase_time = datetime(2026, 8, 6, 9, 30, 0)  # 31 calendar days after
    _add_live_node_with_real_fill('GDXU', 'soxl_ira', repurchase_time)
    assert signals_invariants.check_no_wash_sale_risk_backstop() == []


def test_multiple_distinct_losses_each_surfaced(env):
    """A single real fill sitting in-window of two distinct brokerage losses
    must surface both, not collapse to one flag (paired Opus review found a
    stray `break` was under-reporting exposure)."""
    loss1 = datetime(2026, 7, 6, 15, 30, 0)
    loss2 = datetime(2026, 7, 20, 15, 30, 0)
    _add_brokerage_loss('GDXU', loss1)
    _add_brokerage_loss('GDXU', loss2)
    repurchase_time = loss1 + timedelta(days=18)  # within 30 days of both losses
    node_id = _add_live_node_with_real_fill('GDXU', 'soxl_ira', repurchase_time)
    violations = signals_invariants.check_no_wash_sale_risk_backstop()
    assert len(violations) == 2
    assert all(f'wl_id={node_id}' in v for v in violations)


def test_more_than_50_closed_trades_still_scanned(env):
    """get_trade_log_for_wl_id defaults to limit=50, newest-first -- a real
    node with more than 50 closed trades must still have its OLDER fills
    scanned, or an older disallowed loss silently drops out (paired Opus
    review finding)."""
    loss_exit = datetime(2026, 7, 6, 15, 30, 0)
    _add_brokerage_loss('GDXU', loss_exit)
    repurchase_time = loss_exit + timedelta(days=18)
    node_id = _add_live_node_with_real_fill('GDXU', 'soxl_ira', repurchase_time)
    # Pad with 60 more recent, unrelated closed trades on the same node so the
    # in-window fill above would fall outside a naive limit=50 newest-first window.
    node = db.get_watch_list_node_by_id(node_id)
    for i in range(60):
        t = repurchase_time + timedelta(days=100 + i)
        trade_id = db.log_trade_entry(node, signal_price=100.0, signal_time=t, entry_price=100.0, entry_time=t)
        db.log_trade_exit(trade_id, exit_signal_price=101.0, exit_price=101.0,
                           exit_time=t + timedelta(hours=1), exit_reason='TP', entry_price=100.0)
    violations = signals_invariants.check_no_wash_sale_risk_backstop()
    assert len(violations) == 1
    assert f'wl_id={node_id}' in violations[0]
