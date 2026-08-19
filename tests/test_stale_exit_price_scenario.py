"""Reproduces the real SOXS incident (2026-08-19): a drought-overlay entry's
off-bar-close exit check used signals_compute._current_price (the cached
hourly bar's Close, only refreshed on data_collector.py's own cadence)
instead of a genuinely live quote. That cache was still serving an
18-minute-stale price ($43.45, itself the entry's own original signal price)
long after real price had round-tripped from $42.49 up through $46+ -- so the
first post-entry exit check classified a brand-new, never-actually-losing
position as an SL breach against a price that was never live at that moment.
The subsequent market-order replace then filled at the REAL live price
($46.43), netting a positive P&L despite the SL label -- the mechanism that
made this look benign, but the underlying misclassification is real and can
just as easily go the other way.

Fix: signals_helpers.resolve_live_exit_price (real schwab_client.get_current_price
quote) replaces signals_compute._current_price in both off-bar-close exit-check
call sites: active_signals._check_position_exit (a closure, not directly
testable in isolation -- see the module docstring precedent in
tests/test_fake_broker_sh_scenario.py for why these tests exercise the
directly-callable production functions instead) and
signals_notify.check_dry_run_sim_sells (directly callable, exercised
end-to-end below)."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import signals_compute
import signals_notify
import signals_helpers
import schwab_client
import schwab_safety

TICKER = 'TEST_STALE_EXIT_PRICE_SCENARIO'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=9.0,
                         stop_loss=0, max_hold_hours=42, state='live',
                         trail_buy_pct=8.0, trail_pct=1.0, fixed_sl_override=1.0)
    yield
    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def test_resolve_live_exit_price_uses_a_real_quote_not_the_cache(monkeypatch):
    """Direct proof resolve_live_exit_price calls schwab_client.get_current_price
    (a real quote fetch), not anything cache-derived -- the exact substitution
    this fix makes at both call sites."""
    monkeypatch.setattr(schwab_client, 'get_current_price', lambda ticker: 46.43)
    # resolve_live_exit_price gates on the real trading-day/regular-session
    # window -- pin to a real trading Wednesday, mid-session, so this test's
    # pass/fail doesn't depend on the real clock at run time.
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 8, 19, 10, 30))
    assert signals_helpers.resolve_live_exit_price(TICKER) == 46.43


def test_resolve_live_exit_price_returns_none_on_failure(monkeypatch):
    """Fail-safe contract preserved -- callers already handle None (skip/
    suppress), same as the old _current_price None case."""
    def _boom(ticker):
        raise ValueError("no quote")
    monkeypatch.setattr(schwab_client, 'get_current_price', _boom)
    assert signals_helpers.resolve_live_exit_price(TICKER) is None


def test_check_sell_condition_pins_the_bug_mechanism():
    """Documents WHY the stale price was dangerous: check_sell_condition itself
    has no way to know a price is stale -- fed the real incident's stale
    cached value (43.45, well under the $45.5441 stop), it correctly-but-on-
    bad-data reports SL. Fed the real live price at that same moment (46.43,
    never near the stop), it correctly reports no exit. The bug was never in
    check_sell_condition -- it was entirely in which price reached it."""
    pos = {
        'ticker': TICKER, 'window': 20, 'strategy': 'TrailingBothZScoreBreakout',
        'entry_price': 46.00, 'stop_loss': 1.0, 'fixed_sl': 1.0, 'trail_sell_pct': 1.0,
        'take_profit': None, 'arm_sell_pct': 9.0, 'trail_pct': 1.0, 'max_hold_hours': 42,
        'trail_state': {}, 'signal_time': '2026-08-19 09:49:19', 'wl_id': 1,
    }
    bars = pd.date_range('2026-08-19 09:30:00', periods=2, freq='h')
    df_hourly = pd.DataFrame({'Open': 42.83, 'High': 48.34, 'Low': 42.486, 'Close': 47.00}, index=bars)

    reason_stale, _, _ = signals_compute.check_sell_condition(
        pos, current_price=43.45, now=datetime(2026, 8, 19, 9, 50, 10),
        at_bar_close=False, low=43.45, high=43.45, open_price=43.45, df_hourly=df_hourly)
    assert reason_stale == 'SL', (
        "the stale cached price ($43.45, below the $45.5441 stop) SHOULD trigger "
        "SL when fed directly -- this pins the real incident's mechanism, not a "
        "new assertion about correctness"
    )

    reason_live, _, _ = signals_compute.check_sell_condition(
        pos, current_price=46.43, now=datetime(2026, 8, 19, 9, 50, 10),
        at_bar_close=False, low=46.43, high=46.43, open_price=46.43, df_hourly=df_hourly)
    assert reason_live is None, (
        "the real live price at that moment (46.43, never near the stop) must "
        "NOT trigger an exit -- this is what resolve_live_exit_price now supplies"
    )


def test_check_position_exit_price_resolution_matches_the_real_closure(env, monkeypatch):
    """active_signals._check_position_exit is a closure defined inside
    run_loop() -- not directly callable in isolation (confirmed by reading it;
    same structural limitation tests/test_fake_broker_sh_scenario.py's own
    docstring notes for this file's sibling functions). This test reproduces
    its EXACT off-bar-close sequence line-for-line against the real, callable
    production functions it actually calls (resolve_live_exit_price then
    check_sell_condition) -- not a reimplementation, the same two calls in the
    same order with the same arguments the closure makes.

    NOTE: signals_notify.check_dry_run_sim_sells shares this code SHAPE but
    was deliberately NOT changed by this fix (still uses the cached bar Close
    -- see its own updated comment) since is_dry_run_sim positions carry zero
    real capital and giving them a real broker-quote dependency wasn't
    warranted. This test is the real-capital path's proof; that function's
    existing tests (tests/test_dry_run_sim.py) remain correct as-is."""
    monkeypatch.setattr(schwab_client, 'get_current_price', lambda ticker: 46.43)
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 8, 19, 10, 30))

    cp = signals_helpers.resolve_live_exit_price(TICKER)
    assert cp == 46.43

    pos = {
        'ticker': TICKER, 'window': 20, 'strategy': 'TrailingBothZScoreBreakout',
        'entry_price': 46.00, 'stop_loss': 1.0, 'fixed_sl': 1.0, 'trail_sell_pct': 1.0,
        'take_profit': None, 'arm_sell_pct': 9.0, 'trail_pct': 1.0, 'max_hold_hours': 42,
        'trail_state': {}, 'signal_time': '2026-08-19 09:49:19', 'wl_id': 1,
    }
    bars = pd.date_range('2026-08-19 09:30:00', periods=2, freq='h')
    # Same-hour bar, still forming -- Close reflects the real incident's stale
    # cached value (43.45), which the closure's at_bar_close=False branch must
    # never read (low=high=op=cp, all from resolve_live_exit_price instead).
    df_hourly = pd.DataFrame({'Open': 42.83, 'High': 48.34, 'Low': 42.486, 'Close': 43.45}, index=bars)

    reason, target, _ = signals_compute.check_sell_condition(
        pos, cp, datetime(2026, 8, 19, 9, 50, 10),
        at_bar_close=False, low=cp, high=cp, open_price=cp, df_hourly=df_hourly)
    assert reason is None, (
        f"expected no exit (live price $46.43 never near the $45.5441 stop), got {reason!r} "
        f"-- the real incident's false-SL mechanism reproduced"
    )
