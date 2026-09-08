"""Tests for _last_sale_recovery's addon_legs handling (2026-09-04 fix,
revised 2026-09-06 after paired review).

Real live-vs-backtest mismatch: an add-on-at-arm leg is a real margin buy/sell
with its own P&L, stored in addon_legs -- never in trade_log. close_position()
writes trade_log.shares from open_positions.shares (the core's own share
count), which never gets a leg's real filled shares folded in, so a leg's
proceeds never compounded into the next buy's sizing, contradicting the
validated backtest's apply_addon_overlay_ground_truth (which compounds a
blended core+addon return trade-to-trade as one stream).

Whether a leg was merged_into_core (filled in the same broker SELL order as
its parent's close) or closed independently is broker-order mechanics, not an
economic distinction -- an earlier version of this fix raced an independent
leg against its own parent for "most recent," which silently discarded
whichever side lost. Correct model: every real closed leg's PROFIT --
(exit_price - entry_price) * shares, never raw exit proceeds -- is ADDITIVE
to its own parent trade_log row (via parent_trade_log_id), and an episode's
recency is the later of its trade_log row's close or any of its legs' closes.
A leg whose parent doesn't itself qualify (e.g. dry-run-sim) is an "orphan,"
sized on the leg's own exit_time using the same profit-only formula.

Economics corrected 2026-09-08 (real live-money bug): the original
implementation summed each leg's raw exit_price*shares proceeds, which
double-counts the leg's own margin-financed capital -- backtester.py's
apply_addon_overlay_ground_truth only ever compounds the addon leg's PROFIT
((exit_price - arm_price) * shares) into the next trade's capital base, never
its full exit proceeds. All fixture entry_prices below are deliberately not
exit_price*0.98 where the test cares about the leg's own profit -- see each
assertion's inline math.
"""
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
from signals_helpers import _last_sale_recovery

TICKER = 'TEST_ADDON_SIZING'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    signals_db.ensure_tables()
    signals_db.add_node(
        TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
        stop_loss=1, max_hold_hours=100, state='live', trail_buy_pct=1.0, trail_pct=1.0,
        fixed_sl_override=1.0, account='brokerage', starting_notional=2000)
    wl_id = _node()['id']
    yield wl_id
    tmp_db_path = Path(tmp_db.name)
    if tmp_db_path.exists():
        tmp_db_path.unlink()


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _fmt(dt):
    """Matches signals_db.log_trade_exit/close_addon_leg's real second-resolution
    timestamp format -- a prior version of these fixtures used isoformat() (with a
    'T' separator and microseconds), which never exercised the string comparisons
    against the format actually written in production."""
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def _log_closed_trade(exit_price, shares, exit_time, position_source='core'):
    """Returns the new trade_log row's id (needed as addon_legs.parent_trade_log_id)."""
    entry_price = exit_price * 0.98
    entry_time = _fmt(exit_time - timedelta(hours=5))
    with signals_db._conn() as c:
        cur = c.execute("""
            INSERT INTO trade_log
                (ticker, strategy, version, window, stop_loss, max_hold_hours, account,
                 signal_price, signal_time, entry_price, entry_time, entry_drift_pct,
                 exit_price, exit_time, exit_reason, shares, is_dry_run_sim, position_source)
            VALUES (?, 'TrailingBothZScoreBreakout', 'test', 10, 1, 100, 'brokerage',
                    ?, ?, ?, ?, 0.0, ?, ?, 'SL', ?, 0, ?)
        """, (TICKER, entry_price, entry_time, entry_price, entry_time, exit_price,
              _fmt(exit_time), shares, position_source))
        c.commit()
        return cur.lastrowid


def _log_addon_leg(parent_trade_log_id, exit_price, shares, exit_time, merged_into_core,
                    is_dry_run_sim=0, status='closed', account='brokerage', exit_reason='SL',
                    entry_price=None):
    if entry_price is None:
        entry_price = exit_price * 0.98
    entry_time = _fmt(exit_time - timedelta(hours=5))
    with signals_db._conn() as c:
        c.execute("""
            INSERT INTO addon_legs
                (wl_id, parent_trade_log_id, ticker, account, shares, entry_price, entry_time,
                 status, exit_price, exit_time, exit_reason, pnl_pct, is_dry_run_sim, merged_into_core)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?)
        """, (parent_trade_log_id, TICKER, account, shares, entry_price, entry_time,
              status, exit_price, _fmt(exit_time), exit_reason, is_dry_run_sim, merged_into_core))
        c.commit()


def test_merged_leg_proceeds_add_onto_its_parent_trade_log_row(env):
    """Merged leg's shares are real filled shares absent from trade_log.shares
    -- its PROFIT must be added to its own parent's proceeds, not ignored
    (and not its raw exit proceeds -- entry_price=24.5 here via the default
    exit_price*0.98, so profit = (25.0-24.5)*20 = 10.0)."""
    now = datetime.now()
    tl_id = _log_closed_trade(exit_price=25.0, shares=100, exit_time=now)  # core proceeds = 2500
    _log_addon_leg(tl_id, exit_price=25.0, shares=20, exit_time=now, merged_into_core=1)  # +10 (profit only)
    node = _node()
    assert _last_sale_recovery(node) == 2510.0


def test_merged_leg_on_older_trade_log_row_does_not_leak_into_newer_one(env):
    """A merged leg tied to an OLDER trade_log row must not get added to a
    genuinely more recent, unrelated trade_log row's proceeds."""
    now = datetime.now()
    old_tl_id = _log_closed_trade(exit_price=25.0, shares=100, exit_time=now - timedelta(days=2))
    _log_addon_leg(old_tl_id, exit_price=25.0, shares=20, exit_time=now - timedelta(days=2), merged_into_core=1)
    _log_closed_trade(exit_price=10.0, shares=50, exit_time=now)  # newer, unrelated, proceeds = 500
    node = _node()
    assert _last_sale_recovery(node) == 500.0


def test_unmerged_leg_adds_onto_its_own_parent_and_that_episode_wins_on_recency(env):
    """A leg that closes independently (merged_into_core=0) is still additive
    to its own parent, never a separately-competing candidate -- an earlier
    version of this fix raced it against its parent for "most recent" and
    silently discarded whichever side lost. Here the leg's own late close
    (now) makes ITS episode (parent + leg) more recent than an unrelated
    older-timestamped trade_log row, so the combined total wins."""
    now = datetime.now()
    _log_closed_trade(exit_price=25.0, shares=100, exit_time=now - timedelta(hours=2))  # unrelated, proceeds = 2500
    tl_id = _log_closed_trade(exit_price=10.0, shares=10, exit_time=now - timedelta(hours=2))  # this leg's real parent
    # entry_price = 30.0*0.98 = 29.4, profit = (30.0-29.4)*15 = 9.0
    _log_addon_leg(tl_id, exit_price=30.0, shares=15, exit_time=now, merged_into_core=0)  # +9 (profit only), closes later
    node = _node()
    assert _last_sale_recovery(node) == pytest.approx(109.0)  # 100 (parent) + 9 (leg profit), not 9 alone


def test_unmerged_leg_still_adds_onto_core_even_when_closed_earlier(env):
    """A leg closing BEFORE its parent's own close (e.g. an add-on abandoned/
    closed ahead of the core's eventual exit) is still real proceeds from the
    same episode -- must be added, not dropped just because it isn't the most
    recent activity."""
    now = datetime.now()
    tl_id = _log_closed_trade(exit_price=25.0, shares=100, exit_time=now)  # core proceeds = 2500
    # entry_price = 30.0*0.98 = 29.4, profit = (30.0-29.4)*15 = 9.0
    _log_addon_leg(tl_id, exit_price=30.0, shares=15, exit_time=now - timedelta(days=1), merged_into_core=0)  # +9
    node = _node()
    assert _last_sale_recovery(node) == 2509.0


def test_unmerged_leg_wins_when_no_real_core_row_qualifies(env):
    """An unmerged leg's own JOIN only needs the parent trade_log row to match
    on strategy/version/window (for scoping), not to itself qualify as a real
    trade_log_row candidate -- a dry-run-sim parent (is_dry_run_sim=1, excluded
    from the trade_log_row race the same as always) must still let its real,
    non-dry-run addon leg win outright when it's the only real candidate. The
    orphan branch has no real core proceeds to add profit onto, so it falls
    back to the node's starting_notional (2000, per the fixture) as the base
    -- bare leg profit alone would catastrophically under-size the next buy."""
    now = datetime.now()
    with signals_db._conn() as c:
        cur = c.execute("""
            INSERT INTO trade_log
                (ticker, strategy, version, window, stop_loss, max_hold_hours, account,
                 signal_price, signal_time, entry_price, entry_time, entry_drift_pct,
                 exit_price, exit_time, exit_reason, shares, is_dry_run_sim, position_source)
            VALUES (?, 'TrailingBothZScoreBreakout', 'test', 10, 1, 100, 'brokerage',
                    1.0, ?, 1.0, ?, 0.0, 1.0, ?, 'SL', 1, 1, 'core')
        """, (TICKER, _fmt(now - timedelta(days=365)), _fmt(now - timedelta(days=365)),
              _fmt(now - timedelta(days=365))))
        c.commit()
        tl_id = cur.lastrowid
    # entry_price = 30.0*0.98 = 29.4, profit = (30.0-29.4)*15 = 9.0
    _log_addon_leg(tl_id, exit_price=30.0, shares=15, exit_time=now, merged_into_core=0)  # 9 (profit only)
    node = _node()
    assert _last_sale_recovery(node) == pytest.approx(2009.0)  # starting_notional(2000) + profit(9)


def test_dry_run_sim_addon_leg_excluded(env):
    now = datetime.now()
    tl_id = _log_closed_trade(exit_price=25.0, shares=100, exit_time=now - timedelta(hours=2))
    _log_addon_leg(tl_id, exit_price=999.0, shares=999, exit_time=now, merged_into_core=0, is_dry_run_sim=1)
    node = _node()
    assert _last_sale_recovery(node) == 2500.0


def test_open_addon_leg_excluded(env):
    now = datetime.now()
    tl_id = _log_closed_trade(exit_price=25.0, shares=100, exit_time=now - timedelta(hours=2))
    _log_addon_leg(tl_id, exit_price=999.0, shares=999, exit_time=now, merged_into_core=0, status='open')
    node = _node()
    assert _last_sale_recovery(node) == 2500.0


def test_override_still_takes_precedence_over_addon_leg_history(env):
    now = datetime.now()
    tl_id = _log_closed_trade(exit_price=25.0, shares=100, exit_time=now)
    _log_addon_leg(tl_id, exit_price=25.0, shares=20, exit_time=now, merged_into_core=1)
    wl_id = env
    signals_db.set_starting_notional_override(wl_id, 900)
    node = _node()
    assert _last_sale_recovery(node) == 900.0


def test_addon_leg_scoped_to_matching_account_only(env):
    """An addon leg closed under a different account (e.g. a different node
    that happens to share ticker) must not size this node."""
    now = datetime.now()
    tl_id = _log_closed_trade(exit_price=25.0, shares=100, exit_time=now - timedelta(hours=2))
    _log_addon_leg(tl_id, exit_price=999.0, shares=999, exit_time=now, merged_into_core=0, account='ira')
    node = _node()
    assert _last_sale_recovery(node) == 2500.0


def test_abandoned_addon_leg_excluded(env):
    """An addon leg whose entry order never filled is closed with
    exit_reason='ABANDONED' (signals_notify.py's abandon/reconciliation-timeout
    paths) -- exit_price/shares are the intended, never-bought values, not real
    proceeds, and must never size the next real buy."""
    now = datetime.now()
    tl_id = _log_closed_trade(exit_price=25.0, shares=100, exit_time=now - timedelta(hours=2))
    _log_addon_leg(tl_id, exit_price=999.0, shares=999, exit_time=now, merged_into_core=0,
                    exit_reason='ABANDONED')
    node = _node()
    assert _last_sale_recovery(node) == 2500.0


def test_addon_leg_compounds_profit_not_raw_proceeds(env):
    """Exact worked example from the 2026-09-08 fix: core 10sh entry=$5 exit=$10
    (proceeds=100), addon 10sh arm(entry)=$6 exit=$10 (profit=(10-6)*10=40).
    Matches backtester.py's apply_addon_overlay_ground_truth blended_return =
    (2*exit - entry - arm) / entry -- addon capital (financed via margin at
    the arm price) is excluded from the compounding base, only its profit
    compounds in. Correct total is 140.0, NOT 200.0 (100 core proceeds + 100
    raw addon proceeds -- the pre-fix bug)."""
    now = datetime.now()
    tl_id = _log_closed_trade(exit_price=10.0, shares=10, exit_time=now)
    with signals_db._conn() as c:
        c.execute("UPDATE trade_log SET entry_price=5.0 WHERE id=?", (tl_id,))
        c.commit()
    _log_addon_leg(tl_id, exit_price=10.0, shares=10, exit_time=now, merged_into_core=0,
                    entry_price=6.0)
    node = _node()
    assert _last_sale_recovery(node) == 140.0


def test_episode_losing_addon_leg_reduces_total_but_floors_at_zero(env):
    """A losing addon leg (exit < entry, e.g. armed then reversed against a
    leveraged ETF) subtracts from the episode total -- and if its loss
    exceeds the core's own proceeds, the total must floor at 0, never go
    negative (a negative next-buy notional must never reach share-count
    math)."""
    now = datetime.now()
    tl_id = _log_closed_trade(exit_price=5.0, shares=10, exit_time=now)  # core proceeds = 50
    # entry=10.0 (arm price), exit=4.0 -> loss = (4-10)*10 = -60
    _log_addon_leg(tl_id, exit_price=4.0, shares=10, exit_time=now, merged_into_core=0, entry_price=10.0)
    node = _node()
    assert _last_sale_recovery(node) == 0.0  # 50 - 60 = -10, floored to 0


def test_orphan_losing_addon_leg_falls_back_to_starting_notional_plus_loss(env):
    """An orphan leg (no qualifying core row) that loses money still uses
    starting_notional as its base -- never bare (negative) profit alone."""
    now = datetime.now()
    with signals_db._conn() as c:
        cur = c.execute("""
            INSERT INTO trade_log
                (ticker, strategy, version, window, stop_loss, max_hold_hours, account,
                 signal_price, signal_time, entry_price, entry_time, entry_drift_pct,
                 exit_price, exit_time, exit_reason, shares, is_dry_run_sim, position_source)
            VALUES (?, 'TrailingBothZScoreBreakout', 'test', 10, 1, 100, 'brokerage',
                    1.0, ?, 1.0, ?, 0.0, 1.0, ?, 'SL', 1, 1, 'core')
        """, (TICKER, _fmt(now - timedelta(days=365)), _fmt(now - timedelta(days=365)),
              _fmt(now - timedelta(days=365))))
        c.commit()
        tl_id = cur.lastrowid
    # entry=10.0 (arm price), exit=4.0 -> loss = (4-10)*10 = -60
    _log_addon_leg(tl_id, exit_price=4.0, shares=10, exit_time=now, merged_into_core=0, entry_price=10.0)
    node = _node()
    assert _last_sale_recovery(node) == pytest.approx(1940.0)  # starting_notional(2000) - 60


def test_multiple_real_legs_on_same_parent_all_sum(env):
    """Two real closed legs tied to the same parent (e.g. one abandoned leg
    freed the arm guard for a second, later real leg) must both be added, not
    just the first one a non-aggregating lookup happens to fetch."""
    now = datetime.now()
    tl_id = _log_closed_trade(exit_price=25.0, shares=100, exit_time=now - timedelta(hours=3))  # 2500
    # entry=30*0.98=29.4, profit=(30-29.4)*10=6.0
    _log_addon_leg(tl_id, exit_price=30.0, shares=10, exit_time=now - timedelta(hours=2), merged_into_core=0)  # +6
    # entry=20*0.98=19.6, profit=(20-19.6)*10=4.0
    _log_addon_leg(tl_id, exit_price=20.0, shares=10, exit_time=now, merged_into_core=0)  # +4
    node = _node()
    assert _last_sale_recovery(node) == 2510.0
