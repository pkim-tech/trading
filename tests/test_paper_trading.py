"""Paper-trading layer: start_paper_buy -> update_paper_buys (bounce-fill) ->
check_paper_sells (exit), against an isolated sqlite file, mirroring
tests/test_db_roundtrip.py's fixture pattern but exercising the paper=True
tables instead of the real ones."""
import os
import sys
import tempfile
import pytest
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_db as db
import signals_config
import paper_trading
from tests.conftest import make_synthetic_csv, cleanup_csv

TICKER = 'TEST_PAPER'


@pytest.fixture
def isolated_db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    db.ensure_tables()
    make_synthetic_csv(TICKER, last_close=100.0)
    yield
    cleanup_csv(TICKER)
    os.unlink(tmp_db.name)


def _node():
    return {
        'id': 1, 'ticker': TICKER, 'strategy': 'TrailingBothZScoreBreakout', 'version': 'test',
        'window': 20, 'take_profit': None, 'stop_loss': 1, 'max_hold_hours': 7,
        'trail_buy_pct': 5.0, 'trail_sell_pct': 1.0, 'fixed_sl': 1.0, 'arm_sell_pct': 7.0,
        'starting_notional': 5000, 'account': 'ira',
    }


def _sig(price):
    return {'ticker': TICKER, 'current_price': price, 'z_score': -2.5, 'last_bar': datetime.now()}


def test_start_paper_buy_creates_pending_row(isolated_db):
    paper_trading.start_paper_buy(_node(), _sig(100.0))
    pending = db.get_paper_pending_buys()
    assert len(pending) == 1
    assert pending[0]['ticker'] == TICKER
    assert pending[0]['running_low'] == 100.0


def test_start_paper_buy_dedups_existing_pending(isolated_db):
    paper_trading.start_paper_buy(_node(), _sig(100.0))
    paper_trading.start_paper_buy(_node(), _sig(99.0))
    assert len(db.get_paper_pending_buys()) == 1


def test_update_paper_buys_tracks_running_low_without_filling(monkeypatch, isolated_db):
    paper_trading.start_paper_buy(_node(), _sig(100.0))
    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (98.0, None))
    paper_trading.update_paper_buys()
    pending = db.get_paper_pending_buys()
    assert len(pending) == 1
    assert pending[0]['running_low'] == 98.0
    assert db.get_open_positions(paper=True) == []


def test_update_paper_buys_fills_on_bounce_and_opens_paper_position(monkeypatch, isolated_db):
    paper_trading.start_paper_buy(_node(), _sig(100.0))
    # running_low drops to 90, then bounces >= 5% above it (94.5) -> fill
    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (90.0, None))
    paper_trading.update_paper_buys()
    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (95.0, None))
    paper_trading.update_paper_buys()

    assert db.get_paper_pending_buys() == []
    positions = db.get_open_positions(paper=True)
    assert len(positions) == 1
    pos = positions[0]
    assert pos['entry_price'] == 95.0
    # Flat starting_notional/fill_price -- deliberately NOT buy_order_sizing's
    # worst-case pad (2026-07-31, reverted after review: the fill price is
    # already known here, and this flat formula matches what a real position
    # actually ends up holding after _reconcile_fill's post-fill top-up,
    # unlike the padded formula which would undersize it).
    assert pos['shares'] == int(5000 // 95.0)
    assert pos['trade_log_id'] is not None


def test_bounce_fill_hold_time_anchors_to_fill_not_stale_signal_time(monkeypatch, isolated_db):
    """Hold-time origin fix (2026-07-31): a bounce-fill that happens hours
    after the original signal must NOT carry that wait against the
    position's hold-time budget -- pos['signal_time'] (what _bars_held
    counts from) should reflect the real fill moment, matching the backtest
    kernel's entry_bar/held=0-at-fill semantics, not the original signal."""
    stale_signal_time = datetime.now() - timedelta(hours=5)
    sig = {'ticker': TICKER, 'current_price': 100.0, 'z_score': -2.5, 'last_bar': stale_signal_time}
    paper_trading.start_paper_buy(_node(), sig)
    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (90.0, None))
    paper_trading.update_paper_buys()
    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (95.0, None))
    paper_trading.update_paper_buys()

    pos = db.get_open_positions(paper=True)[0]
    stored_signal_time = datetime.strptime(pos['signal_time'], '%Y-%m-%d %H:%M:%S')
    assert (datetime.now() - stored_signal_time).total_seconds() < 60, (
        f"pos['signal_time']={pos['signal_time']} should be ~now, not the stale "
        f"original signal time ({stale_signal_time})"
    )


def test_paper_bounce_fill_sizing_matches_real_post_topup_end_state(monkeypatch, isolated_db):
    """Regression test for the third, looser paper sizing formula (2026-07-31
    audit, left open, then further corrected same day after review): a real
    trailing-buy position is sized off buy_order_sizing's worst-case pad at
    SIGNAL time (fill price unknown), then topped up post-fill
    (_reconcile_fill, signals_notify.py) until shares*fill_price converges
    on target_notional -- so the real end state a real position lands on is
    simply target_notional // fill_price, with no pad. Paper already knows
    the fill price when it fills (no signal-time uncertainty to pad against),
    so it should size directly to that same end state instead of re-applying
    the signal-time pad (which would UNDERsize it relative to what a real
    position actually ends up holding -- the opposite of the intended
    alignment, caught by review before landing)."""
    node = _node()
    paper_trading.start_paper_buy(node, _sig(100.0))
    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (90.0, None))
    paper_trading.update_paper_buys()
    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (95.0, None))
    paper_trading.update_paper_buys()

    pos = db.get_open_positions(paper=True)[0]
    assert pos['shares'] == int(node['starting_notional'] // 95.0)


def test_check_paper_sells_closes_on_sl_and_writes_paper_trade_log(monkeypatch, isolated_db):
    node = _node()
    signal_time = datetime.now() - timedelta(hours=1)
    db.open_position(node, signal_price=100.0, signal_time=signal_time,
                      entry_price=100.0, entry_time=signal_time, shares=50, paper=True)
    pos = db.get_open_positions(paper=True)[0]

    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (98.0, None))  # -2% > 1% fixed_sl

    from signals_compute import _load_cache
    df_hourly, _ = _load_cache(TICKER)
    # Pre-mark this bar as already seen so check_paper_sells takes the mid-bar
    # (continuous) branch, which prices off _current_price -- the synthetic CSV
    # fixture only has a Close column, no Low/High, so the bar-close branch
    # (which would read bar['Low']/bar['High']) isn't exercisable here.
    last_seen_bar = {pos['wl_id']: df_hourly.index[-1]}

    paper_trading.check_paper_sells(last_seen_bar, set(), _load_cache)

    assert db.get_open_positions(paper=True) == []
    with db._conn() as c:
        row = c.execute(
            "SELECT exit_reason, pnl_pct FROM paper_trade_log WHERE id = ?", (pos['trade_log_id'],)
        ).fetchone()
    assert row['exit_reason'] == 'SL'
    assert row['pnl_pct'] < 0


def test_check_paper_sells_first_poll_ignores_pre_entry_bar_low(monkeypatch, isolated_db):
    """Regression test for a real incident (2026-07-27): a freshly-opened
    position's first check_paper_sells call must NOT be graded as 'a bar just
    closed' using the current hourly bar's own Low -- that bar may contain a
    real price dip that happened BEFORE the position was even entered, which
    has nothing to do with the position's actual post-entry price path. Here
    the current bar's real Low ($90) is well past the 1% fixed_sl from a $100
    entry, but the live current_price ($99.80) never came close to it -- a
    fresh position must not be closed on that first check."""
    import pandas as pd
    from tests.conftest import CACHE_DIR, _synthetic_timestamps

    timestamps = _synthetic_timestamps(90)
    closes = [100.0] * len(timestamps)
    df = pd.DataFrame({
        'Open': closes, 'High': closes, 'Low': closes, 'Close': closes,
    }, index=timestamps)
    df.index.name = 'Datetime'
    # Current (still-forming) bar: a real dip to $90 happened earlier in this
    # bar, well before the position was entered.
    df.loc[df.index[-1], ['Open', 'High', 'Low', 'Close']] = [100.0, 100.0, 90.0, 99.5]
    df.to_csv(CACHE_DIR / f"{TICKER}_1h.csv")

    node = _node()
    signal_time = datetime.now() - timedelta(hours=1)
    db.open_position(node, signal_price=100.0, signal_time=signal_time,
                      entry_price=100.0, entry_time=datetime.now(), shares=50, paper=True)
    pos = db.get_open_positions(paper=True)[0]

    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (99.80, None))

    from signals_compute import _load_cache
    last_seen_bar = {}
    paper_trading.check_paper_sells(last_seen_bar, set(), _load_cache)

    open_positions = db.get_open_positions(paper=True)
    assert len(open_positions) == 1, "fresh position must not be closed against pre-entry bar history"
    assert pos['wl_id'] in last_seen_bar, "current bar must be seeded as already-seen"


def test_real_open_positions_untouched_by_paper_flow(monkeypatch, isolated_db):
    paper_trading.start_paper_buy(_node(), _sig(100.0))
    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (90.0, None))
    paper_trading.update_paper_buys()
    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (95.0, None))
    paper_trading.update_paper_buys()

    assert db.get_open_positions(paper=False) == []
    assert db.get_pending_buys() == []


# ---------------------------------------------------------------------------
# Same-bar re-entry cooldown (2026-08-14) -- paper mirror of the RETL/soxl_ira
# real-account fix in active_signals._scan_buy_signals. Reproduces the exact
# bug shape found live in LABD's paper node (wl_id=152): open -> SL exit ->
# fresh open, all within the same bar / under ~90 seconds, a sequence the
# backtest kernel's per-bar loop structurally cannot produce.
# ---------------------------------------------------------------------------

def test_paper_market_buy_reentry_blocked_same_bar_after_reactive_exit(isolated_db):
    """The LABD shape: a mid-bar reactive SL exit (exit_bar_time=None, exactly
    what check_paper_sells' non-at_bar_close branch writes) must still block a
    same-bar re-entry, via exit_decision_bar rather than exit_bar_time."""
    node = _node()
    bar1 = datetime(2026, 8, 13, 9, 30, 0)
    paper_trading.start_paper_market_buy(node, {'ticker': TICKER, 'current_price': 100.0,
                                                 'z_score': -2.5, 'last_bar': bar1})
    pos = db.get_open_positions(paper=True)[0]

    db.close_position(pos['id'], exit_signal_price=99.0, exit_price=99.0,
                       exit_time=datetime.now(), exit_reason='SL', paper=True,
                       exit_bar_time=None, exit_decision_bar=bar1)
    assert db.get_last_paper_exit_decision_bar(node['id']) == bar1.strftime('%Y-%m-%d %H:%M:%S')

    # Same-bar re-entry attempt must be suppressed.
    paper_trading.start_paper_market_buy(node, {'ticker': TICKER, 'current_price': 98.0,
                                                 'z_score': -2.6, 'last_bar': bar1})
    assert db.get_open_positions(paper=True) == []

    # A genuinely later bar must be allowed through.
    bar2 = bar1 + timedelta(hours=1)
    paper_trading.start_paper_market_buy(node, {'ticker': TICKER, 'current_price': 98.0,
                                                 'z_score': -2.6, 'last_bar': bar2})
    assert len(db.get_open_positions(paper=True)) == 1


def test_paper_market_buy_returns_true_on_cooldown_suppression(isolated_db):
    """start_paper_market_buy/start_paper_buy must return True specifically when
    the cooldown suppressed the call -- active_signals._scan_buy_signals relies
    on this return value to release the node's buy_alerted slot (Opus review,
    2026-08-14: without it, a cross-day stale-bar suppression burns the node's
    one alert slot for the rest of that day with nothing to ever release it,
    since a cooldown suppression -- unlike already_pending/already_held -- has
    no later fill/close event that would naturally clear it)."""
    node = _node()
    bar1 = datetime(2026, 8, 13, 9, 30, 0)
    sig1 = {'ticker': TICKER, 'current_price': 100.0, 'z_score': -2.5, 'last_bar': bar1}
    assert paper_trading.start_paper_market_buy(node, sig1) is None
    pos = db.get_open_positions(paper=True)[0]
    db.close_position(pos['id'], exit_signal_price=99.0, exit_price=99.0,
                       exit_time=datetime.now(), exit_reason='SL', paper=True,
                       exit_bar_time=None, exit_decision_bar=bar1)

    # Same-bar re-entry: suppressed, must return True (release the alert slot).
    sig2 = {'ticker': TICKER, 'current_price': 98.0, 'z_score': -2.6, 'last_bar': bar1}
    assert paper_trading.start_paper_market_buy(node, sig2) is True
    assert paper_trading.start_paper_buy(node, sig2) is True

    # A genuinely later bar: not suppressed, must not return True.
    bar2 = bar1 + timedelta(hours=1)
    sig3 = {'ticker': TICKER, 'current_price': 98.0, 'z_score': -2.6, 'last_bar': bar2}
    assert paper_trading.start_paper_market_buy(node, sig3) is not True


def test_paper_trailing_buy_pending_reentry_blocked_same_bar_after_reactive_exit(isolated_db):
    """Same shape via the trailing-buy entry path (LABD's actual strategy,
    TrailingBothZScoreBreakout) -- start_paper_buy must not even open a
    pending-buy row for a same-bar re-entry."""
    node = _node()
    bar1 = datetime(2026, 8, 13, 9, 30, 0)
    db.open_position(node, signal_price=100.0, signal_time=bar1,
                      entry_price=100.0, entry_time=datetime.now(), shares=50, paper=True)
    pos = db.get_open_positions(paper=True)[0]
    db.close_position(pos['id'], exit_signal_price=99.0, exit_price=99.0,
                       exit_time=datetime.now(), exit_reason='SL', paper=True,
                       exit_bar_time=None, exit_decision_bar=bar1)

    paper_trading.start_paper_buy(node, {'ticker': TICKER, 'current_price': 98.0,
                                          'z_score': -2.6, 'last_bar': bar1})
    assert db.get_paper_pending_buys() == []

    bar2 = bar1 + timedelta(hours=1)
    paper_trading.start_paper_buy(node, {'ticker': TICKER, 'current_price': 98.0,
                                          'z_score': -2.6, 'last_bar': bar2})
    assert len(db.get_paper_pending_buys()) == 1


def test_check_paper_sells_populates_exit_decision_bar_on_mid_bar_exit(monkeypatch, isolated_db):
    """Confirms check_paper_sells itself (not a hand-built close_position call)
    writes exit_decision_bar unconditionally, unlike exit_bar_time which stays
    NULL on the mid-bar/reactive branch."""
    node = _node()
    signal_time = datetime.now() - timedelta(hours=1)
    db.open_position(node, signal_price=100.0, signal_time=signal_time,
                      entry_price=100.0, entry_time=datetime.now(), shares=50, paper=True)

    from signals_compute import _load_cache
    last_seen_bar = {}
    # Seed last_seen_bar so the very next call is treated as an already-seen
    # bar (mid-bar/reactive branch), matching resolve_at_bar_close's own
    # first-check deferral -- mirrors test_at_bar_close_deferred_on_fresh_position above.
    paper_trading.check_paper_sells(last_seen_bar, set(), _load_cache)
    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (98.0, None))
    paper_trading.check_paper_sells(last_seen_bar, set(), _load_cache)

    with db._conn() as c:
        trade = dict(c.execute(
            "SELECT * FROM paper_trade_log WHERE wl_id = ? ORDER BY id DESC LIMIT 1", (node['id'],)
        ).fetchone())
    assert trade['exit_decision_bar'] is not None
    assert trade['exit_bar_time'] is None
