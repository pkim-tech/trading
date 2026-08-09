"""Tests for the 2026-08-05 daily-track paper node design (docs/design.md's
"Two-account paper trading" section): compute_buy_signal's price-source gate,
add_node's paper_role dedup/schema support, and
paper_trading.reconcile_daily_track_nodes' classification logic.

reconcile_daily_track_nodes is PURE OBSERVATION (final design, corrected
mid-session from an earlier reconcile-and-resync version): it never opens,
closes, or halts a position -- it only classifies each node's actual state
against a fresh backtest replay and writes one diagnostic log row per node
per night. Every test below that exercises reconcile therefore also asserts
the node's real paper state is UNCHANGED by the call."""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_compute as compute
import signals_config
import signals_db as db
import paper_trading
from tests.conftest import make_synthetic_csv, cleanup_csv, fake_node

TICKER = 'TEST_DAILY_TRACK'


@pytest.fixture
def isolated_db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    db.ensure_tables()
    yield db
    os.unlink(tmp_db.name)


# ---------------------------------------------------------------------------
# compute_buy_signal's daily_sync price-source gate
# ---------------------------------------------------------------------------

def test_compute_buy_signal_ignores_price_override_and_live_tick_for_daily_track_node(monkeypatch):
    make_synthetic_csv(TICKER, last_close=100.0)
    node = fake_node(TICKER, 'ZScoreBreakout')
    node['paper_role'] = 'daily_sync'
    from tests.conftest import _synthetic_timestamps
    last_bar_ts = _synthetic_timestamps(90)[-1]

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return last_bar_ts + timedelta(minutes=5)

    monkeypatch.setattr(compute, 'datetime', _FrozenDatetime)
    result = compute.compute_buy_signal(node, price_override=5.0)
    cleanup_csv(TICKER)
    assert result is not None
    assert result['current_price'] != 5.0
    assert abs(result['current_price'] - 100.0) < 5.0


def test_compute_buy_signal_stale_bar_returns_none_for_daily_track_node(monkeypatch):
    make_synthetic_csv(TICKER, last_close=100.0)
    node = fake_node(TICKER, 'ZScoreBreakout')
    node['paper_role'] = 'daily_sync'
    import signals_compute as sc

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2099, 1, 1)

    monkeypatch.setattr(sc, 'datetime', _FrozenDatetime)
    result = compute.compute_buy_signal(node, price_override=5.0)
    cleanup_csv(TICKER)
    assert result is None


def test_ordinary_node_still_honors_price_override():
    make_synthetic_csv(TICKER, last_close=100.0)
    node = fake_node(TICKER, 'ZScoreBreakout')
    result = compute.compute_buy_signal(node, price_override=99.5)
    cleanup_csv(TICKER)
    assert result['current_price'] == 99.5


def test_compute_buy_signal_daily_track_trims_still_forming_current_bar(monkeypatch):
    make_synthetic_csv(TICKER, last_close=100.0)
    node = fake_node(TICKER, 'ZScoreBreakout')
    node['paper_role'] = 'daily_sync'
    from tests.conftest import _synthetic_timestamps
    last_bar_ts = _synthetic_timestamps(90)[-1]

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return last_bar_ts.replace(minute=45)

    monkeypatch.setattr(compute, 'datetime', _FrozenDatetime)
    result = compute.compute_buy_signal(node)
    cleanup_csv(TICKER)
    assert result is not None
    assert result['last_bar'] < last_bar_ts


# ---------------------------------------------------------------------------
# add_node / paper_role schema support
# ---------------------------------------------------------------------------

def _add_pair(ticker=TICKER, strategy='TrailingBothZScoreBreakout'):
    db.add_node(ticker, strategy, 'v5', window=10, take_profit=25,
                stop_loss=1, max_hold_hours=56, trail_buy_pct=3.0, trail_pct=4.0,
                fixed_sl_override=1, state='paper')
    db.add_node(ticker, strategy, 'v5', window=10, take_profit=25,
                stop_loss=1, max_hold_hours=56, trail_buy_pct=3.0, trail_pct=4.0,
                fixed_sl_override=1, state='paper', paper_role='daily_sync')
    nodes = sorted([n for n in db.get_watchlist() if n['ticker'] == ticker],
                    key=lambda n: n.get('paper_role') or '')
    return nodes[0], nodes[1]


def test_add_node_allows_daily_track_sibling_without_collision(isolated_db):
    live_node, daily_node = _add_pair()
    assert daily_node['paper_role'] == 'daily_sync'
    assert live_node.get('paper_role') is None
    assert len([n for n in db.get_watchlist() if n['ticker'] == TICKER]) == 2

    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5', window=10, take_profit=25,
                stop_loss=1, max_hold_hours=56, trail_buy_pct=3.0, trail_pct=4.0,
                fixed_sl_override=1, state='paper', paper_role='daily_sync')
    assert len([n for n in db.get_watchlist() if n['ticker'] == TICKER]) == 2


# ---------------------------------------------------------------------------
# scripts/paper_vs_backtest_reconcile.get_daily_track_wl_ids -- 2026-08-06 fix
# (commit d64ef98), landed with zero test coverage until this file. Real live
# bug: a staged 'v5-overlay-test*' clone sharing a ticker+paper_role could
# silently shadow the real v5 daily-track node in the ticker->wl_id lookup
# (last-row-wins, no version filter).
# ---------------------------------------------------------------------------

def test_get_daily_track_wl_ids_ignores_staged_overlay_test_clone(isolated_db):
    from scripts.paper_vs_backtest_reconcile import get_daily_track_wl_ids

    wl_id = db.create_watchlist('TEST_RECONCILE_WL')
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5', window=10, take_profit=25,
                stop_loss=1, max_hold_hours=56, trail_buy_pct=3.0, trail_pct=4.0,
                fixed_sl_override=1, state='paper', paper_role='daily_sync', watchlist_id=wl_id)
    # A staged combo-matrix clone, same ticker/paper_role, added LATER (higher id) --
    # this is exactly the shape that shadowed the real node before the fix.
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5-overlay-test', window=10, take_profit=25,
                stop_loss=1, max_hold_hours=56, trail_buy_pct=3.0, trail_pct=4.0,
                fixed_sl_override=1, state='paper', paper_role='daily_sync', watchlist_id=wl_id)

    real_node_row = [n for n in db.get_watchlist(wl_id) if n['version'] == 'v5'][0]
    result = get_daily_track_wl_ids(wl_id)

    assert result[TICKER] == real_node_row['id']


def test_get_daily_track_wl_ids_excludes_live_track_and_other_watchlists(isolated_db):
    from scripts.paper_vs_backtest_reconcile import get_daily_track_wl_ids

    wl_id = db.create_watchlist('TEST_RECONCILE_WL2')
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5', window=10, take_profit=25,
                stop_loss=1, max_hold_hours=56, trail_buy_pct=3.0, trail_pct=4.0,
                fixed_sl_override=1, state='paper', watchlist_id=wl_id)  # live-track: no paper_role
    daily_node_row = [n for n in db.get_watchlist(wl_id) if n['paper_role'] is None]
    assert daily_node_row  # sanity: the live-track node exists

    assert get_daily_track_wl_ids(wl_id) == {}  # no daily_sync node yet -- must not pick up live-track

    other_wl_id = db.create_watchlist('TEST_RECONCILE_WL3')
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5', window=10, take_profit=25,
                stop_loss=1, max_hold_hours=56, trail_buy_pct=3.0, trail_pct=4.0,
                fixed_sl_override=1, state='paper', paper_role='daily_sync', watchlist_id=other_wl_id)
    assert get_daily_track_wl_ids(wl_id) == {}  # scoped to wl_id, not global


# ---------------------------------------------------------------------------
# daily_sync_halted_at gate -- still real code (for a future manual "sync"
# tool), just never set by reconcile anymore
# ---------------------------------------------------------------------------

def test_daily_sync_halted_node_is_skipped_by_start_paper_buy(isolated_db, monkeypatch):
    _, daily_node = _add_pair()
    db.set_daily_sync_halted(daily_node['id'], halted=True)
    daily_node = db.get_watch_list_node_by_id(daily_node['id'])
    assert daily_node['daily_sync_halted_at'] is not None

    monkeypatch.setattr(paper_trading, '_post_message', lambda *a, **kw: (None, None))
    paper_trading.start_paper_market_buy(daily_node, dict(
        ticker=TICKER, current_price=100.0, last_bar=datetime.now(), z_score=-2.5))
    assert db.get_open_position_by_wl_id(daily_node['id'], paper=True) is None


def test_set_daily_sync_halted_clears(isolated_db):
    _, daily_node = _add_pair()
    db.set_daily_sync_halted(daily_node['id'], halted=True)
    db.set_daily_sync_halted(daily_node['id'], halted=False)
    daily_node = db.get_watch_list_node_by_id(daily_node['id'])
    assert daily_node['daily_sync_halted_at'] is None


# ---------------------------------------------------------------------------
# reconcile_daily_track_nodes -- pure observation, no state mutation
# ---------------------------------------------------------------------------

def test_reconcile_logs_replay_failed_instead_of_silently_skipping(isolated_db, monkeypatch):
    """No cached price data at all -- _backtest_replay_for_node raises (no
    hourly CSV for TICKER), so reconcile should not crash, but a permanently
    broken node must still show up in the log (not silently vanish -- found
    by the independent Opus review, 2026-08-05: silence here would
    contradict the whole "logs would be terrible to query" premise)."""
    _, daily_node = _add_pair()
    touched = paper_trading.reconcile_daily_track_nodes()
    assert touched == 0
    log = db.get_daily_track_reconciliation_log(daily_node['id'])
    assert len(log) == 1
    assert log[0]['action'] == 'replay_failed'


def test_reconcile_flags_ambiguous_position_without_touching_it(isolated_db, monkeypatch):
    """daily-track holds a position the backtest replay has no reference for
    at all (result=[] -- no trades) -- logged as ambiguous, position left
    exactly as it was."""
    import pandas as pd
    import numpy as np

    _, daily_node = _add_pair()
    now = datetime.now()
    db.open_position(daily_node, 100.0, now, 100.0, now, paper=True)

    idx = pd.date_range(now - timedelta(hours=5), periods=5, freq='h')
    df_h = pd.DataFrame({'Open': [100.0] * 5, 'Close': [100.0] * 5}, index=idx)
    p = dict(daily_idx=np.array([0, 0, 0, 0, 0]), sma_arr=np.array([100.0]),
             std_arr=np.array([1.0]), prices=np.array([100.0] * 5))

    def _fake_replay(node):
        return [], df_h, p

    monkeypatch.setattr(paper_trading, '_backtest_replay_for_node', _fake_replay)
    touched = paper_trading.reconcile_daily_track_nodes()
    assert touched == 1

    daily_node_after = db.get_watch_list_node_by_id(daily_node['id'])
    assert daily_node_after['daily_sync_halted_at'] is None
    pos_after = db.get_open_position_by_wl_id(daily_node['id'], paper=True)
    assert pos_after is not None and pos_after['entry_price'] == 100.0

    log = db.get_daily_track_reconciliation_log(daily_node['id'])
    assert len(log) == 1
    assert log[0]['action'] == 'ambiguous_position'
    assert log[0]['actual_state'] == 'open_different_trade'
    assert log[0]['backtest_state'] == 'flat'


def test_reconcile_classifies_entry_miss_explained_without_opening_a_position(isolated_db, monkeypatch):
    """The one mechanistically-explained-by-price shape: backtest fired via
    the bar's Open (op <= lower_band) while Close alone would not have --
    daily-track (Close-only) correctly stayed flat. Classified as
    'entry_miss_explained' -- NOT resynced (no position opened)."""
    import pandas as pd
    import numpy as np
    from backtester import OPEN

    _, daily_node = _add_pair()
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(now - timedelta(hours=5), periods=5, freq='h')
    df_h = pd.DataFrame({'Open': [90.0] * 5, 'Close': [100.0] * 5}, index=idx)
    # lower_band = sma - std*z = 100 - 1*2 = 98. Open=90 <= 98 (fires), Close=100 > 98 (would not).
    p = dict(daily_idx=np.array([0, 0, 0, 0, 0]), sma_arr=np.array([100.0]),
             std_arr=np.array([1.0]), prices=np.array([100.0] * 5))

    bt_trade = dict(signal_i=2, signal_z=-10.0, entry_i=3, arm_i=None, exit_i=4,
                     entry_p=91.0, exit_p=95.0, held=2, result=OPEN, ret=0.04)

    def _fake_replay(node):
        return [bt_trade], df_h, p

    monkeypatch.setattr(paper_trading, '_backtest_replay_for_node', _fake_replay)
    touched = paper_trading.reconcile_daily_track_nodes()
    assert touched == 1

    assert db.get_open_position_by_wl_id(daily_node['id'], paper=True) is None
    daily_node_after = db.get_watch_list_node_by_id(daily_node['id'])
    assert daily_node_after['daily_sync_halted_at'] is None

    log = db.get_daily_track_reconciliation_log(daily_node['id'])
    assert log[0]['action'] == 'entry_miss_explained'
    assert log[0]['explained_by_price'] == 1


def test_reconcile_classifies_entry_miss_unexplained(isolated_db, monkeypatch):
    """Close alone would ALSO have fired the signal -- daily-track should have
    caught it, no price-resolution excuse. Unexplained, no state change."""
    import pandas as pd
    import numpy as np
    from backtester import OPEN

    _, daily_node = _add_pair()
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(now - timedelta(hours=5), periods=5, freq='h')
    df_h = pd.DataFrame({'Open': [90.0] * 5, 'Close': [90.0] * 5}, index=idx)
    # lower_band=98; Close=90 <= 98 too -- Close alone would have fired.
    p = dict(daily_idx=np.array([0, 0, 0, 0, 0]), sma_arr=np.array([100.0]),
             std_arr=np.array([1.0]), prices=np.array([90.0] * 5))

    bt_trade = dict(signal_i=2, signal_z=-10.0, entry_i=3, arm_i=None, exit_i=4,
                     entry_p=91.0, exit_p=95.0, held=2, result=OPEN, ret=0.04)

    def _fake_replay(node):
        return [bt_trade], df_h, p

    monkeypatch.setattr(paper_trading, '_backtest_replay_for_node', _fake_replay)
    touched = paper_trading.reconcile_daily_track_nodes()
    assert touched == 1
    assert db.get_open_position_by_wl_id(daily_node['id'], paper=True) is None

    log = db.get_daily_track_reconciliation_log(daily_node['id'])
    assert log[0]['action'] == 'entry_miss_unexplained'
    assert log[0]['explained_by_price'] == 0


def test_reconcile_matches_organic_trailing_buy_fill_via_signal_bar_time(isolated_db, monkeypatch):
    """Regression: a real trailing-buy paper fill stores signal_time=fill_time
    (wall-clock, the deliberate 2026-07-31 hold-budget fix), which can never
    resolve to a bar index. signal_bar_time (captured by update_paper_buys)
    fixes the bar-match."""
    import pandas as pd
    import numpy as np
    from backtester import OPEN

    _, daily_node = _add_pair()
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(now - timedelta(hours=5), periods=5, freq='h')
    df_h = pd.DataFrame({'Open': [100.0] * 5, 'Close': [100.0] * 5}, index=idx)
    p = dict(daily_idx=np.array([0, 0, 0, 0, 0]), sma_arr=np.array([100.0]),
             std_arr=np.array([1.0]), prices=np.array([100.0] * 5))

    signal_bar = idx[2]
    db.open_position(daily_node, 100.0, now, 100.0, now, shares=10, paper=True,
                      signal_bar_time=signal_bar)

    bt_trade = dict(signal_i=2, signal_z=-2.0, entry_i=2, arm_i=None, exit_i=4,
                     entry_p=100.0, exit_p=101.0, held=2, result=OPEN, ret=0.01)

    def _fake_replay(node):
        return [bt_trade], df_h, p

    monkeypatch.setattr(paper_trading, '_backtest_replay_for_node', _fake_replay)
    touched = paper_trading.reconcile_daily_track_nodes()
    assert touched == 0

    log = db.get_daily_track_reconciliation_log(daily_node['id'])
    assert log[0]['action'] == 'match'


def test_reconcile_skips_node_with_resting_pending_buy(isolated_db, monkeypatch):
    """A node mid bounce-fill wait is a fourth state (pending), not
    flat/open/closed -- judging it against that dichotomy caused false
    unexplained classifications."""
    _, daily_node = _add_pair()
    sig = dict(current_price=100.0, last_bar=datetime.now())
    db.add_paper_pending_buy(daily_node, sig)

    def _fake_replay(node):
        raise AssertionError("should not reach backtest replay when a pending buy is resting")

    monkeypatch.setattr(paper_trading, '_backtest_replay_for_node', _fake_replay)
    touched = paper_trading.reconcile_daily_track_nodes()
    assert touched == 0

    log = db.get_daily_track_reconciliation_log(daily_node['id'])
    assert log[0]['action'] == 'pending_skip'


def test_reconcile_exit_early_when_daily_track_closed_but_backtest_still_open(isolated_db, monkeypatch):
    """daily-track already closed a trade against this exact signal bar for
    real; the backtest replay still shows it open. Classified 'exit_early' --
    no counterfactual (exits aren't price-source-isolated), no state change."""
    import pandas as pd
    import numpy as np
    from backtester import OPEN

    _, daily_node = _add_pair()
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(now - timedelta(hours=5), periods=5, freq='h')
    df_h = pd.DataFrame({'Open': [90.0] * 5, 'Close': [100.0] * 5}, index=idx)
    p = dict(daily_idx=np.array([0, 0, 0, 0, 0]), sma_arr=np.array([100.0]),
             std_arr=np.array([1.0]), prices=np.array([100.0] * 5))
    signal_bar = idx[2]

    db.open_position(daily_node, 91.0, now, 91.0, now, shares=10, paper=True,
                      signal_bar_time=signal_bar)
    pos = db.get_open_position_by_wl_id(daily_node['id'], paper=True)
    db.close_position(pos['id'], exit_signal_price=95.0, exit_price=95.0, exit_time=now,
                       exit_reason='TIME', paper=True)
    assert db.get_open_position_by_wl_id(daily_node['id'], paper=True) is None

    bt_trade = dict(signal_i=2, signal_z=-10.0, entry_i=3, arm_i=None, exit_i=4,
                     entry_p=91.0, exit_p=95.0, held=2, result=OPEN, ret=0.04)

    def _fake_replay(node):
        return [bt_trade], df_h, p

    monkeypatch.setattr(paper_trading, '_backtest_replay_for_node', _fake_replay)
    touched = paper_trading.reconcile_daily_track_nodes()
    assert touched == 1

    assert db.get_open_position_by_wl_id(daily_node['id'], paper=True) is None
    log = db.get_daily_track_reconciliation_log(daily_node['id'])
    assert log[0]['action'] == 'exit_early'
    assert log[0]['explained_by_price'] is None


def test_reconcile_exit_match_same_bar(isolated_db, monkeypatch):
    """Both sides closed the trade within the same hourly bar -- a match,
    regardless of the exact intrabar price. Exit bar resolved via
    _bar_containing (last bar at-or-before wall-clock exit_time)."""
    import pandas as pd
    import numpy as np
    from backtester import LOSS

    _, daily_node = _add_pair()
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(now - timedelta(hours=6), periods=6, freq='h')
    df_h = pd.DataFrame({'Open': [90.0] * 6, 'Close': [100.0] * 6}, index=idx)
    p = dict(daily_idx=np.array([0, 0, 0, 0, 0, 0]), sma_arr=np.array([100.0]),
             std_arr=np.array([1.0]), prices=np.array([100.0] * 6))
    signal_bar = idx[2]

    db.open_position(daily_node, 91.0, now, 91.0, now, shares=10, paper=True,
                      signal_bar_time=signal_bar)
    pos = db.get_open_position_by_wl_id(daily_node['id'], paper=True)
    exit_time = idx[4] + timedelta(minutes=20)
    db.close_position(pos['id'], exit_signal_price=95.0, exit_price=95.0, exit_time=exit_time,
                       exit_reason='SL', paper=True)

    bt_trade = dict(signal_i=2, signal_z=-10.0, entry_i=3, arm_i=None, exit_i=4,
                     entry_p=91.0, exit_p=94.0, held=2, result=LOSS, ret=0.03)

    def _fake_replay(node):
        return [bt_trade], df_h, p

    monkeypatch.setattr(paper_trading, '_backtest_replay_for_node', _fake_replay)
    touched = paper_trading.reconcile_daily_track_nodes()
    assert touched == 0

    log = db.get_daily_track_reconciliation_log(daily_node['id'])
    assert log[0]['action'] == 'match'


def test_reconcile_exit_bar_close_uses_exit_bar_time_not_wallclock(isolated_db, monkeypatch):
    """Regression: an at_bar_close exit's exit_time is wall-clock (poll time),
    which always falls chronologically inside the NEXT bar's window (bar N
    ends exactly when bar N+1 begins). Without exit_bar_time (captured
    explicitly by check_paper_sells), this would misattribute to the wrong
    bar and falsely classify a clean exit as mismatched."""
    import pandas as pd
    import numpy as np
    from backtester import LOSS

    _, daily_node = _add_pair()
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(now - timedelta(hours=6), periods=6, freq='h')
    df_h = pd.DataFrame({'Open': [90.0] * 6, 'Close': [100.0] * 6}, index=idx)
    p = dict(daily_idx=np.array([0, 0, 0, 0, 0, 0]), sma_arr=np.array([100.0]),
             std_arr=np.array([1.0]), prices=np.array([100.0] * 6))
    signal_bar = idx[2]

    db.open_position(daily_node, 91.0, now, 91.0, now, shares=10, paper=True,
                      signal_bar_time=signal_bar)
    pos = db.get_open_position_by_wl_id(daily_node['id'], paper=True)
    exit_time = idx[4] + timedelta(minutes=2)
    db.close_position(pos['id'], exit_signal_price=95.0, exit_price=95.0, exit_time=exit_time,
                       exit_reason='SL', paper=True, exit_bar_time=idx[3])

    bt_trade = dict(signal_i=2, signal_z=-10.0, entry_i=3, arm_i=None, exit_i=3,
                     entry_p=91.0, exit_p=94.0, held=1, result=LOSS, ret=0.03)

    def _fake_replay(node):
        return [bt_trade], df_h, p

    monkeypatch.setattr(paper_trading, '_backtest_replay_for_node', _fake_replay)
    touched = paper_trading.reconcile_daily_track_nodes()
    assert touched == 0

    log = db.get_daily_track_reconciliation_log(daily_node['id'])
    assert log[0]['action'] == 'match'


def test_reconcile_exit_bar_mismatch(isolated_db, monkeypatch):
    """Both sides closed, but on genuinely different bars -- no known
    price-source explanation once entries already match."""
    import pandas as pd
    import numpy as np
    from backtester import LOSS

    _, daily_node = _add_pair()
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(now - timedelta(hours=5), periods=5, freq='h')
    df_h = pd.DataFrame({'Open': [90.0] * 5, 'Close': [100.0] * 5}, index=idx)
    p = dict(daily_idx=np.array([0, 0, 0, 0, 0]), sma_arr=np.array([100.0]),
             std_arr=np.array([1.0]), prices=np.array([100.0] * 5))
    signal_bar = idx[2]

    db.open_position(daily_node, 91.0, now, 91.0, now, shares=10, paper=True,
                      signal_bar_time=signal_bar)
    pos = db.get_open_position_by_wl_id(daily_node['id'], paper=True)
    exit_time = idx[3] + timedelta(minutes=10)
    db.close_position(pos['id'], exit_signal_price=95.0, exit_price=95.0, exit_time=exit_time,
                       exit_reason='SL', paper=True)

    bt_trade = dict(signal_i=2, signal_z=-10.0, entry_i=3, arm_i=None, exit_i=4,
                     entry_p=91.0, exit_p=94.0, held=2, result=LOSS, ret=0.03)

    def _fake_replay(node):
        return [bt_trade], df_h, p

    monkeypatch.setattr(paper_trading, '_backtest_replay_for_node', _fake_replay)
    touched = paper_trading.reconcile_daily_track_nodes()
    assert touched == 1

    log = db.get_daily_track_reconciliation_log(daily_node['id'])
    assert log[0]['action'] == 'exit_bar_mismatch'


def test_reconcile_exit_wick_only_sl_explained(isolated_db, monkeypatch):
    """The backtest's SL breach was wick-only (Close alone would not have
    triggered it) -- daily-track's live-tick polling legitimately couldn't
    have caught it either. Classified 'exit_wick_explained' -- position left
    open, untouched (pure observation -- no force-close anymore)."""
    import pandas as pd
    import numpy as np
    from backtester import LOSS

    _, daily_node = _add_pair()
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(now - timedelta(hours=5), periods=5, freq='h')
    df_h = pd.DataFrame({'Open': [90.0] * 5, 'Close': [100.0] * 5}, index=idx)
    # fixed_sl=1% off entry_p=91.0 -> stop_price=90.09. Close (100.0) never breaches it.
    p = dict(daily_idx=np.array([0, 0, 0, 0, 0]), sma_arr=np.array([100.0]),
             std_arr=np.array([1.0]), prices=np.array([100.0] * 5))
    signal_bar = idx[2]

    db.open_position(daily_node, 91.0, now, 91.0, now, shares=10, paper=True,
                      signal_bar_time=signal_bar)

    bt_trade = dict(signal_i=2, signal_z=-10.0, entry_i=3, arm_i=None, exit_i=4,
                     entry_p=91.0, exit_p=88.0, held=2, result=LOSS, ret=-0.03)

    def _fake_replay(node):
        return [bt_trade], df_h, p

    monkeypatch.setattr(paper_trading, '_backtest_replay_for_node', _fake_replay)
    touched = paper_trading.reconcile_daily_track_nodes()
    assert touched == 1

    # Pure observation -- the position is untouched.
    pos_after = db.get_open_position_by_wl_id(daily_node['id'], paper=True)
    assert pos_after is not None and pos_after['entry_price'] == 91.0
    log = db.get_daily_track_reconciliation_log(daily_node['id'])
    assert log[0]['action'] == 'exit_wick_explained'
    assert log[0]['explained_by_price'] == 1


def test_reconcile_exit_sl_close_would_also_breach_unexplained(isolated_db, monkeypatch):
    """The bar's Close ALSO breaches the SL trigger -- not just a wick,
    daily-track should have caught it. Unexplained."""
    import pandas as pd
    import numpy as np
    from backtester import LOSS

    _, daily_node = _add_pair()
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(now - timedelta(hours=5), periods=5, freq='h')
    df_h = pd.DataFrame({'Open': [90.0] * 5, 'Close': [80.0] * 5}, index=idx)
    # fixed_sl=1% off entry_p=91.0 -> stop_price=90.09. Close=80.0 <= 90.09 -- breaches too.
    p = dict(daily_idx=np.array([0, 0, 0, 0, 0]), sma_arr=np.array([100.0]),
             std_arr=np.array([1.0]), prices=np.array([80.0] * 5))
    signal_bar = idx[2]

    db.open_position(daily_node, 91.0, now, 91.0, now, shares=10, paper=True,
                      signal_bar_time=signal_bar)

    bt_trade = dict(signal_i=2, signal_z=-10.0, entry_i=3, arm_i=None, exit_i=4,
                     entry_p=91.0, exit_p=79.0, held=2, result=LOSS, ret=-0.13)

    def _fake_replay(node):
        return [bt_trade], df_h, p

    monkeypatch.setattr(paper_trading, '_backtest_replay_for_node', _fake_replay)
    touched = paper_trading.reconcile_daily_track_nodes()
    assert touched == 1

    pos_after = db.get_open_position_by_wl_id(daily_node['id'], paper=True)
    assert pos_after is not None and pos_after['entry_price'] == 91.0
    log = db.get_daily_track_reconciliation_log(daily_node['id'])
    assert log[0]['action'] == 'exit_wick_unexplained'
    assert log[0]['explained_by_price'] == 0


def test_reconcile_exit_trail_wick_only_explained(isolated_db, monkeypatch):
    """Trailing-stop counterpart to the SL wick case: reconstructs the peak
    using only Close from arm_i to exit_i -- if that alone would not have
    breached, the real breach came from an intrabar High/Low wick."""
    import pandas as pd
    import numpy as np
    from backtester import WIN

    _, daily_node = _add_pair()
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(now - timedelta(hours=5), periods=5, freq='h')
    df_h = pd.DataFrame({'Open': [90.0] * 5, 'Close': [100.0] * 5}, index=idx)
    # trail_sell_pct=4.0% (from _add_pair). arm_i=3: peak starts at Close[3]=100.
    # Only bar checked after arming is exit_i=4: Close[4]=100, peak stays 100,
    # trail_stop=100*0.96=96. Close(100) never breaches -- wick-only.
    p = dict(daily_idx=np.array([0, 0, 0, 0, 0]), sma_arr=np.array([100.0]),
             std_arr=np.array([1.0]), prices=np.array([100.0] * 5))
    signal_bar = idx[2]

    db.open_position(daily_node, 91.0, now, 91.0, now, shares=10, paper=True,
                      signal_bar_time=signal_bar)

    bt_trade = dict(signal_i=2, signal_z=-10.0, entry_i=3, arm_i=3, exit_i=4,
                     entry_p=91.0, exit_p=95.0, held=2, result=WIN, ret=0.04)

    def _fake_replay(node):
        return [bt_trade], df_h, p

    monkeypatch.setattr(paper_trading, '_backtest_replay_for_node', _fake_replay)
    touched = paper_trading.reconcile_daily_track_nodes()
    assert touched == 1

    pos_after = db.get_open_position_by_wl_id(daily_node['id'], paper=True)
    assert pos_after is not None and pos_after['entry_price'] == 91.0
    log = db.get_daily_track_reconciliation_log(daily_node['id'])
    assert log[0]['action'] == 'exit_wick_explained'
    assert log[0]['explained_by_price'] == 1


def test_reconcile_exit_trail_close_would_also_breach_unexplained(isolated_db, monkeypatch):
    """Trailing-stop counterpart where Close alone WOULD also breach --
    unexplained."""
    import pandas as pd
    import numpy as np
    from backtester import WIN

    _, daily_node = _add_pair()
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(now - timedelta(hours=5), periods=5, freq='h')
    df_h = pd.DataFrame({'Open': [90.0] * 5, 'Close': [70.0] * 5}, index=idx)
    # arm_i=3: peak starts at Close[3]=70. exit_i=4: Close[4]=70, trail_stop=70*0.96=67.2.
    # 70 <= 67.2? No -- need a real drop after arm to trigger. Use a peak then drop.
    prices = np.array([90.0, 90.0, 90.0, 100.0, 90.0])
    p = dict(daily_idx=np.array([0, 0, 0, 0, 0]), sma_arr=np.array([100.0]),
             std_arr=np.array([1.0]), prices=prices)
    signal_bar = idx[2]

    db.open_position(daily_node, 91.0, now, 91.0, now, shares=10, paper=True,
                      signal_bar_time=signal_bar)

    bt_trade = dict(signal_i=2, signal_z=-10.0, entry_i=3, arm_i=3, exit_i=4,
                     entry_p=91.0, exit_p=95.0, held=2, result=WIN, ret=0.04)

    def _fake_replay(node):
        return [bt_trade], df_h, p

    monkeypatch.setattr(paper_trading, '_backtest_replay_for_node', _fake_replay)
    touched = paper_trading.reconcile_daily_track_nodes()
    assert touched == 1

    log = db.get_daily_track_reconciliation_log(daily_node['id'])
    assert log[0]['action'] == 'exit_wick_unexplained'
    assert log[0]['explained_by_price'] == 0


def test_reconcile_exit_time_forced_while_armed_unexplained(isolated_db, monkeypatch):
    """held >= max_hold_hours while armed -- a bar-count question, not a
    price one, so the wick counterfactual doesn't apply and this is always
    unexplained."""
    import pandas as pd
    import numpy as np
    from backtester import WIN

    _, daily_node = _add_pair()
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(now - timedelta(hours=5), periods=5, freq='h')
    df_h = pd.DataFrame({'Open': [90.0] * 5, 'Close': [100.0] * 5}, index=idx)
    p = dict(daily_idx=np.array([0, 0, 0, 0, 0]), sma_arr=np.array([100.0]),
             std_arr=np.array([1.0]), prices=np.array([100.0] * 5))
    signal_bar = idx[2]

    db.open_position(daily_node, 91.0, now, 91.0, now, shares=10, paper=True,
                      signal_bar_time=signal_bar)

    # max_hold_hours=56 (from _add_pair) -- held >= 56 means TIME-forced.
    bt_trade = dict(signal_i=2, signal_z=-10.0, entry_i=3, arm_i=3, exit_i=4,
                     entry_p=91.0, exit_p=95.0, held=56, result=WIN, ret=0.04)

    def _fake_replay(node):
        return [bt_trade], df_h, p

    monkeypatch.setattr(paper_trading, '_backtest_replay_for_node', _fake_replay)
    touched = paper_trading.reconcile_daily_track_nodes()
    assert touched == 1

    log = db.get_daily_track_reconciliation_log(daily_node['id'])
    assert log[0]['action'] == 'exit_wick_unexplained'
    assert log[0]['explained_by_price'] == 0
    assert 'TIME-forced' in log[0]['detail']


def test_reconcile_catches_new_missed_entry_after_old_closed_trade(isolated_db, monkeypatch):
    """Regression for the HIGH bug found by both paired Opus reviews,
    2026-08-05: the first restructure anchored on daily-track's own most
    recent closed trade with no recency scoping, so once a node had ANY
    trade history, entry-miss detection became permanently dead. Fixed by
    anchoring on the backtest's own most recent trade (bt_ref) instead."""
    import pandas as pd
    import numpy as np
    from backtester import OPEN, LOSS

    _, daily_node = _add_pair()
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(now - timedelta(hours=6), periods=6, freq='h')
    df_h = pd.DataFrame({'Open': [90.0] * 6, 'Close': [100.0] * 6}, index=idx)
    p = dict(daily_idx=np.array([0, 0, 0, 0, 0, 0]), sma_arr=np.array([100.0]),
             std_arr=np.array([1.0]), prices=np.array([100.0] * 6))

    old_signal_bar = idx[0]
    db.open_position(daily_node, 91.0, now, 91.0, now, shares=10, paper=True,
                      signal_bar_time=old_signal_bar)
    old_pos = db.get_open_position_by_wl_id(daily_node['id'], paper=True)
    db.close_position(old_pos['id'], exit_signal_price=95.0, exit_price=95.0, exit_time=idx[1],
                       exit_reason='SL', paper=True, exit_bar_time=idx[1])

    new_bt_trade = dict(signal_i=4, signal_z=-10.0, entry_i=5, arm_i=None, exit_i=5,
                         entry_p=89.0, exit_p=89.0, held=0, result=OPEN, ret=0.0)

    def _fake_replay(node):
        return [dict(signal_i=0, signal_z=-10.0, entry_i=1, arm_i=None, exit_i=1,
                      entry_p=91.0, exit_p=95.0, held=1, result=LOSS, ret=0.04),
                new_bt_trade], df_h, p

    monkeypatch.setattr(paper_trading, '_backtest_replay_for_node', _fake_replay)
    touched = paper_trading.reconcile_daily_track_nodes()
    assert touched == 1

    log = db.get_daily_track_reconciliation_log(daily_node['id'])
    assert log[0]['action'] == 'entry_miss_explained'
    assert log[0]['actual_state'] == 'flat'
    assert log[0]['backtest_state'] == 'open'
    # Pure observation -- no position opened for the missed entry.
    assert db.get_open_position_by_wl_id(daily_node['id'], paper=True) is None


def test_reconcile_is_a_noop_the_second_time_same_day(isolated_db, monkeypatch):
    """A restart after 16:05 re-running the EOD job must not double-log."""
    import pandas as pd
    import numpy as np

    _, daily_node = _add_pair()

    def _fake_replay(node):
        return [], pd.DataFrame({'Open': [100.0], 'Close': [100.0]}, index=[datetime.now()]), \
            dict(daily_idx=np.array([0]), sma_arr=np.array([100.0]), std_arr=np.array([1.0]),
                 prices=np.array([100.0]))

    monkeypatch.setattr(paper_trading, '_backtest_replay_for_node', _fake_replay)
    assert paper_trading.reconcile_daily_track_nodes() == 0
    assert paper_trading.reconcile_daily_track_nodes() == 0
    assert len(db.get_daily_track_reconciliation_log(daily_node['id'])) == 1


def test_reconcile_exit_time_forced_unarmed_not_misread_as_sl_wick(isolated_db, monkeypatch):
    """Regression for the MEDIUM-HIGH bug found by the contextual Opus review,
    2026-08-05: an UNARMED TIME-forced exit (the kernel produces a genuine
    TWIN/TLOSS result for a trade that never armed too, not just the armed
    held>=max_hold_hours branch) was being routed into the SL wick
    counterfactual instead of the TIME-forced check, since the check
    previously required arm_i is not None. Close is nowhere near the
    entry-based SL threshold for a trade that never got close to breaching
    it, so it always resolved 'explained' with an actively false "SL breach
    was wick-only" detail -- silently hiding a real bar-count divergence."""
    import pandas as pd
    import numpy as np
    from backtester import TWIN

    _, daily_node = _add_pair()
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(now - timedelta(hours=5), periods=5, freq='h')
    df_h = pd.DataFrame({'Open': [90.0] * 5, 'Close': [100.0] * 5}, index=idx)
    p = dict(daily_idx=np.array([0, 0, 0, 0, 0]), sma_arr=np.array([100.0]),
             std_arr=np.array([1.0]), prices=np.array([100.0] * 5))
    signal_bar = idx[2]

    db.open_position(daily_node, 91.0, now, 91.0, now, shares=10, paper=True,
                      signal_bar_time=signal_bar)

    # Never armed (arm_i=None), but held >= max_hold_hours (56, from _add_pair) --
    # a genuine unarmed TIME exit (TWIN/TLOSS in the kernel), not an SL exit at all.
    bt_trade = dict(signal_i=2, signal_z=-10.0, entry_i=3, arm_i=None, exit_i=4,
                     entry_p=91.0, exit_p=95.0, held=56, result=TWIN, ret=0.04)

    def _fake_replay(node):
        return [bt_trade], df_h, p

    monkeypatch.setattr(paper_trading, '_backtest_replay_for_node', _fake_replay)
    touched = paper_trading.reconcile_daily_track_nodes()
    assert touched == 1

    log = db.get_daily_track_reconciliation_log(daily_node['id'])
    assert log[0]['action'] == 'exit_wick_unexplained'
    assert log[0]['explained_by_price'] == 0
    assert 'TIME-forced' in log[0]['detail']
    assert 'wick-only' not in log[0]['detail']


def test_reconcile_entry_miss_no_backtest_data_when_counterfactual_uncomputable(isolated_db, monkeypatch):
    """Regression: when the entry counterfactual itself can't be computed (no
    prior-day indicator history, di<0), this must be distinguished from
    'computed it and found unexplained' -- reported as 'no_backtest_data'
    with explained_by_price=None, not a false 'entry_miss_unexplained'."""
    import pandas as pd
    import numpy as np
    from backtester import OPEN

    _, daily_node = _add_pair()
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(now - timedelta(hours=5), periods=5, freq='h')
    df_h = pd.DataFrame({'Open': [90.0] * 5, 'Close': [100.0] * 5}, index=idx)
    # daily_idx=-1 at the signal bar -- no prior-day indicator row exists yet.
    p = dict(daily_idx=np.array([-1, -1, -1, -1, -1]), sma_arr=np.array([100.0]),
             std_arr=np.array([1.0]), prices=np.array([100.0] * 5))

    bt_trade = dict(signal_i=2, signal_z=-10.0, entry_i=3, arm_i=None, exit_i=4,
                     entry_p=91.0, exit_p=95.0, held=2, result=OPEN, ret=0.04)

    def _fake_replay(node):
        return [bt_trade], df_h, p

    monkeypatch.setattr(paper_trading, '_backtest_replay_for_node', _fake_replay)
    touched = paper_trading.reconcile_daily_track_nodes()
    assert touched == 1

    log = db.get_daily_track_reconciliation_log(daily_node['id'])
    assert log[0]['action'] == 'no_backtest_data'
    assert log[0]['explained_by_price'] is None
