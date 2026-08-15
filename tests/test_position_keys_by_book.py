"""Regression for the highest-severity defect found in the Tranche 3 paired
review: a PAPER position silently suppressing a REAL node's BUY entries.

active_signals unioned real and paper position keys into one duplicate-
suppression set. _pos_key returns pos['wl_id'], so a paper row and a real row
on the SAME node produce an identical key -- meaning a node that flips
paper->live while its paper position is still open reads as already_held
forever after, and every real BUY is dropped by a branch that only prints
(no Slack, no coverage event).

This is not hypothetical. SOXL/ira wl_id=92 ($10k, state='live' since
2026-08-10) carried an open paper position (paper_trade_log id=36) until
2026-08-13 -- roughly 3 trading days during which its real entries were
suppressed. It cost nothing purely because no signal fired in that window.

Note it needs NO NULL wl_id to trigger: the same-wl_id paper->live flip is
sufficient on its own.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import active_signals


def _p(wl_id, ticker='SOXL', window=10):
    return {'wl_id': wl_id, 'ticker': ticker, 'window': window}


def test_paper_and_real_keys_are_kept_in_separate_books():
    keys = active_signals._position_keys_by_book([_p(92)], [_p(95, 'YANG')])
    assert keys['live'] == {92}
    assert keys['paper'] == {95}


def test_a_paper_position_does_not_appear_in_the_live_book():
    """The exact SOXL/wl_id=92 shape: a paper row on a node that is now live."""
    keys = active_signals._position_keys_by_book([], [_p(92)])
    assert 92 not in keys['live'], (
        "a paper position must never suppress a real node's entry -- this is the "
        "condition that was armed on a real $10k live node for 3 days")
    assert 92 in keys['paper']


def test_each_book_keeps_its_own_ticker_window_fallback_for_null_wl_id():
    """The 2026-07-26 duplicate-suppression fix must survive the split intact
    WITHIN each book -- it protects real order placement against a real legacy
    NULL-wl_id row, and that protection is unchanged."""
    keys = active_signals._position_keys_by_book(
        [{'wl_id': None, 'ticker': 'USD', 'window': 20}],
        [{'wl_id': None, 'ticker': 'YANG', 'window': 10}])
    assert ('USD', 20) in keys['live'], "real NULL-wl_id fallback must still work"
    assert ('YANG', 10) in keys['paper']
    assert ('YANG', 10) not in keys['live'], "paper's fallback must not leak into the live book"


def test_a_null_wl_id_paper_row_cannot_masquerade_as_a_real_nodes_key():
    """The collision the reviewers found pre-staged in the live DB: paper USD/
    window=20 vs live node 155 USD/window=20."""
    keys = active_signals._position_keys_by_book(
        [], [{'wl_id': None, 'ticker': 'USD', 'window': 20}])
    assert ('USD', 20) not in keys['live']


def test_both_books_are_always_present_even_when_empty():
    """_scan_buy_signals indexes the dict unconditionally by node state."""
    keys = active_signals._position_keys_by_book([], [])
    assert keys == {'live': set(), 'paper': set()}
