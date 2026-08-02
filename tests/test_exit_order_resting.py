"""_exit_order_resting (2026-08-01): the real broker-status check that closes
the HIGH finding an Opus review made in the first version of the TP/TRAIL
alert-wording fix -- a stored order_id's mere presence isn't proof an order
is still resting (a REJECTED/CANCELED order looks identical by id alone).

Gated on schwab_safety._OPEN_ORDER_STATUSES_EXCLUDED (5 statuses: CANCELED,
EXPIRED, FILLED, REJECTED, REPLACED -- the same set the resting-order
duplicate guard uses for "is this genuinely still open"), not
schwab_client._ORDER_TERMINAL_BAD_STATUSES (only 3, built for a different
question and would have reported "confirmed resting" for an order that had
actually FILLED or been REPLACED -- caught by a second review round).

An earlier version of this function also had a TRAIL-specific fallback
(trusting trail_state['order_placed'] when order_id is None, to cover
_attempt_automated_sell's exit_order_id legitimately being None on a real
automated placement success) -- removed after the same review round found
order_placed is ALSO set by a manual Slack button tap
(signals_handlers.handle_trail_order_placed) with no broker verification, so
trusting it would have reintroduced the exact "trust an unverified flag"
pattern this function exists to eliminate. order_id is None now always
returns None (the pre-2026-08-01 cautious behavior) regardless of reason."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import schwab_client
from signals_notify import _exit_order_resting

POS = {'id': 1, 'ticker': 'GDXU', 'account': 'soxl_ira'}


def test_returns_none_when_no_order_id(monkeypatch):
    assert _exit_order_resting(POS, 'TP', None) is None
    assert _exit_order_resting(POS, 'TRAIL', None) is None


def test_returns_true_when_order_status_is_working(monkeypatch):
    monkeypatch.setattr(schwab_client, 'get_order_status', lambda account, order_id: 'AWAITING_STOP_CONDITION')
    assert _exit_order_resting(POS, 'TRAIL', 999) is True


def test_returns_false_when_order_confirmed_rejected(monkeypatch):
    monkeypatch.setattr(schwab_client, 'get_order_status', lambda account, order_id: 'REJECTED')
    assert _exit_order_resting(POS, 'TP', 999) is False


def test_returns_false_when_order_confirmed_canceled(monkeypatch):
    monkeypatch.setattr(schwab_client, 'get_order_status', lambda account, order_id: 'CANCELED')
    assert _exit_order_resting(POS, 'TIME', 999) is False


def test_returns_false_when_order_already_filled(monkeypatch):
    # The gap a second review round caught: _ORDER_TERMINAL_BAD_STATUSES
    # (3 statuses) doesn't include FILLED, which would have made this
    # wrongly report "confirmed resting" for an order that already filled.
    monkeypatch.setattr(schwab_client, 'get_order_status', lambda account, order_id: 'FILLED')
    assert _exit_order_resting(POS, 'TP', 999) is False


def test_returns_false_when_order_replaced(monkeypatch):
    monkeypatch.setattr(schwab_client, 'get_order_status', lambda account, order_id: 'REPLACED')
    assert _exit_order_resting(POS, 'TRAIL', 999) is False


def test_returns_none_when_status_check_unconfirmed(monkeypatch):
    monkeypatch.setattr(schwab_client, 'get_order_status', lambda account, order_id: None)
    assert _exit_order_resting(POS, 'TP', 999) is None
