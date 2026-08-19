"""Real parametrized truth table for the exit-check price-source state space
this fix touches (2026-08-19 SOXS incident -- see
tests/test_stale_exit_price_scenario.py for the single incident-shaped
reproduction; this is the systematic version the user asked for after that
single case wasn't enough: "it should have all these variations in the
opening tested as well").

State space: {at_bar_close: True/False} x {cached bar Close vs stop:
above/below} x {live quote vs stop: above/below}. 8 real reachable cells.
The bug this closes was specifically the at_bar_close=False rows silently
using the cached-Close column instead of the live-quote column -- so every
row where those two columns DISAGREE (cells 5-8 below) is the part that
would have failed before the fix; cells 1-4 (at_bar_close=True) are included
anyway to confirm the bar-close path is genuinely unaffected by either fix
(it never reads the live quote at all, by design -- a real bar-close
evaluation should trust the real bar, not a live tick).

Distinct from tests/test_stale_exit_price_scenario.py: that file proves the
mechanism with a hand-picked pair of values (43.45 vs 46.43) matching the
real incident, and pins the RED/GREEN distinction via a git-stash rerun.
This file is the systematic enumeration -- one parametrized test asserting
the correct outcome per real reachable cell in a single place, so a future
change to this logic is checked against the whole space, not just the one
value pair that happened to bite.

active_signals._check_position_exit is a closure defined inside run_loop,
not directly callable in isolation -- this table reproduces its exact
off-bar-close sequence (resolve_live_exit_price then check_sell_condition,
same calls in the same order) against the real, callable production
functions instead of the closure wrapper. signals_notify.check_dry_run_sim_sells
was deliberately NOT changed by this fix (is_dry_run_sim carries zero real
capital -- see that function's own updated comment), so it's not exercised
here; this table is scoped to the real-capital path this incident actually
hit."""
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_compute
import signals_helpers
import schwab_client
import schwab_safety

TICKER = 'TEST_EXIT_PRICE_SOURCE_TRUTH_TABLE'
ENTRY_PRICE = 46.00
STOP_PRICE = 45.5441  # 46.00 * (1 - 0.01), fixed_sl=1%
BELOW_STOP = 43.45    # the real incident's stale value
ABOVE_STOP = 46.43    # the real incident's true live value


def _pos():
    return {
        'ticker': TICKER, 'window': 20, 'strategy': 'TrailingBothZScoreBreakout',
        'entry_price': ENTRY_PRICE, 'stop_loss': 1.0, 'fixed_sl': 1.0, 'trail_sell_pct': 1.0,
        'take_profit': None, 'arm_sell_pct': 9.0, 'trail_pct': 1.0, 'max_hold_hours': 42,
        'trail_state': {}, 'signal_time': '2026-08-19 09:49:19', 'wl_id': 1,
    }


# at_bar_close, bar_close_vs_stop ('below'/'above'), live_quote_vs_stop ('below'/'above') -> expect_sl
TRUTH_TABLE = [
    # at_bar_close=True: real bar Open/Low drive the decision -- live quote is
    # never consulted at all, so its column is irrelevant here by design.
    (True,  'below', 'below', True),
    (True,  'below', 'above', True),   # bar itself breached -- live quote irrelevant
    (True,  'above', 'below', False),  # bar itself never breached -- live quote irrelevant
    (True,  'above', 'above', False),
    # at_bar_close=False: the live quote must drive the decision, the cached
    # bar Close must be ignored entirely -- these 4 rows are the actual bug.
    (False, 'below', 'below', True),
    (False, 'below', 'above', False),  # THE incident's exact shape: cache says breach, live doesn't
    (False, 'above', 'below', True),   # mirror case: cache looks safe, live has genuinely breached
    (False, 'above', 'above', False),
]


@pytest.mark.parametrize(
    "at_bar_close,bar_close_vs_stop,live_quote_vs_stop,expect_sl",
    TRUTH_TABLE,
    ids=[f"at_bar_close={a}-bar={b}-live={l}" for a, b, l, _ in TRUTH_TABLE],
)
def test_exit_check_price_source_truth_table(monkeypatch, at_bar_close, bar_close_vs_stop,
                                              live_quote_vs_stop, expect_sl):
    bar_close = BELOW_STOP if bar_close_vs_stop == 'below' else ABOVE_STOP
    live_quote = BELOW_STOP if live_quote_vs_stop == 'below' else ABOVE_STOP

    bars = pd.date_range('2026-08-19 09:30:00', periods=1, freq='h')
    df_hourly = pd.DataFrame(
        {'Open': bar_close, 'High': max(bar_close, ENTRY_PRICE), 'Low': bar_close, 'Close': bar_close},
        index=bars)
    monkeypatch.setattr(schwab_client, 'get_current_price', lambda ticker: live_quote)
    # resolve_live_exit_price gates on the real trading-day/regular-session
    # window (see its docstring) -- pin to a real trading Wednesday, mid-
    # session, so this test's own pass/fail doesn't depend on when it happens
    # to run (found: failed unconditionally outside 9:30-16:00 ET on a
    # trading day, since the un-pinned real clock made the gate return None
    # every time regardless of the mocked quote above).
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 8, 19, 10, 30))

    # Reproduces active_signals._check_position_exit's exact branch, real calls:
    if at_bar_close:
        bar = df_hourly.iloc[-1]
        cp, low, high, op = float(bar['Close']), float(bar['Low']), float(bar['High']), float(bar['Open'])
    else:
        cp = signals_helpers.resolve_live_exit_price(TICKER)
        low = high = op = cp

    reason, _, _ = signals_compute.check_sell_condition(
        _pos(), cp, datetime(2026, 8, 19, 9, 50, 10),
        at_bar_close=at_bar_close, low=low, high=high, open_price=op, df_hourly=df_hourly)

    label = f"at_bar_close={at_bar_close} bar={bar_close_vs_stop} live={live_quote_vs_stop}"
    if expect_sl:
        assert reason == 'SL', f"[{label}] expected SL, got {reason!r}"
    else:
        assert reason is None, f"[{label}] expected no exit, got {reason!r}"
