"""Property-based test for paper_trading.update_paper_buys' fill-vs-abandon
ordering, using hypothesis instead of hand-enumerated scenarios.

Built 2026-07-31, per explicit user request for a more comprehensive way to
test permutations than a hand-written truth table -- check_entry_abandon's
state space (3 independent axes, 8 cells) was small enough to hand-enumerate
(see tests/test_entry_abandon.py + scripts/mutation_test_entry_abandon.py),
but update_paper_buys' real bug (found by review: abandon was checked before
the bounce-fill, inverting the kernel's real per-bar priority) lives in a
5-axis space (price available/stale, price value, running_low, trail_buy_pct,
bars_held vs max_hold_hours) where hand enumeration would either miss corners
or take as long to write as hypothesis takes to search.

Rather than assert a single example, this asserts the INVARIANT the ordering
bug violated: a genuine bounce must always fill, even on the exact poll a
position is also overdue (matches backtester.py's _simulate_trail_both:
check the fill first, only fall through to `wait_bars >= max_hours_to_hold`
if this bar didn't fill) -- fill and abandon are mutually exclusive outcomes
for a single poll, and hypothesis searches hundreds of real (price,
running_low, trail_buy_pct, bars_held, max_hold_hours) combinations for a
counterexample instead of trusting the handful a human would think to write.

Isolates the ordering/priority logic under test by monkeypatching
_current_price/_load_cache/_bars_held directly (bypassing real CSV/timestamp
mechanics, already covered by tests/test_paper_trading.py's scenario tests)
-- this test is deliberately about the branching logic, not data loading."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from hypothesis import given, strategies as st, settings, HealthCheck

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_db as db
import signals_config
import paper_trading

TICKER = 'TEST_PAPER_PROPERTIES'


@pytest.fixture
def env(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(paper_trading, '_post_message', lambda *a, **kw: (None, None))
    db.ensure_tables()
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                stop_loss=1, max_hold_hours=7, state='live',
                trail_buy_pct=5.0, trail_pct=1.0, starting_notional=5000,
                fixed_sl_override=1.0, account='ira')
    yield
    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]


def _reset_pending_and_positions():
    with db._conn() as c:
        c.execute("DELETE FROM paper_pending_buys WHERE ticker=?", (TICKER,))
        c.execute("DELETE FROM paper_positions WHERE ticker=?", (TICKER,))
        c.commit()


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    price=st.one_of(st.none(), st.floats(min_value=50.0, max_value=150.0, allow_nan=False, allow_infinity=False)),
    running_low=st.floats(min_value=50.0, max_value=100.0, allow_nan=False, allow_infinity=False),
    trail_buy_pct=st.floats(min_value=0.1, max_value=10.0, allow_nan=False, allow_infinity=False),
    bars_held=st.integers(min_value=0, max_value=20),
    max_hold_hours=st.integers(min_value=1, max_value=15),
)
def test_paper_fill_and_abandon_are_mutually_exclusive_and_correctly_prioritized(
        env, monkeypatch, price, running_low, trail_buy_pct, bars_held, max_hold_hours):
    _reset_pending_and_positions()
    with db._conn() as c:
        c.execute("UPDATE watch_list SET trail_buy_pct=?, max_hold_hours=? WHERE ticker=?",
                   (trail_buy_pct, max_hold_hours, TICKER))
        c.commit()
    node = _node()
    sig = {'ticker': TICKER, 'current_price': running_low, 'z_score': -2.5, 'last_bar': datetime.now()}
    db.add_paper_pending_buy(node, sig)

    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (price, None))
    monkeypatch.setattr(paper_trading, '_load_cache', lambda t: (object(), None))
    monkeypatch.setattr(paper_trading, '_bars_held', lambda df_hourly, signal_time: bars_held)

    paper_trading.update_paper_buys()

    filled = len(db.get_open_positions(paper=True)) == 1
    pending = db.get_paper_pending_buys()
    cleared = pending == []
    abandoned = cleared and not filled
    still_pending = len(pending) == 1

    # Exactly one outcome per poll -- never both filled and abandoned, never
    # neither happening AND the row also vanishing.
    assert sum([filled, abandoned, still_pending]) == 1, (
        f"expected exactly one outcome, got filled={filled} abandoned={abandoned} "
        f"still_pending={still_pending} (price={price} running_low={running_low} "
        f"trail_buy_pct={trail_buy_pct} bars_held={bars_held} max_hold_hours={max_hold_hours})"
    )

    overdue = bars_held >= max_hold_hours
    trigger = running_low * (1 + trail_buy_pct / 100)
    bounced = price is not None and price > running_low and price >= trigger

    if bounced:
        # The core invariant the real ordering bug violated: a genuine bounce
        # must always fill, even on the exact poll the position is ALSO
        # overdue -- matches the kernel's real per-bar priority (check the
        # fill first, abandon is only a same-bar fallback when it didn't).
        assert filled, "a genuine bounce must fill even when also overdue"
        assert not abandoned
    elif overdue:
        # No bounce this poll (or no price to check one against) and overdue
        # -- must abandon, regardless of whether price was available.
        assert abandoned
    else:
        assert still_pending

    _reset_pending_and_positions()
