"""TP/TRAIL/TIME sell alerts hardcoded manual-cancel instructions regardless
of whether an automated exit order was already resting (2026-08-01, resolving
a 2026-07-28 GDXU finding). Once armed, the SL order is replaced by a resting
trailing-sell (_attempt_automated_sell) -- there's no stop-loss left to
cancel, and notify_sell_signal's manual alert only fires when a short
fill-confirm poll hasn't seen a fill yet, which is the NORMAL case for a
resting trailing-stop, not evidence of a problem.

An Opus review of the first version of this fix found the naive
"order_id is not None" signal doesn't prove an order is still resting -- a
REJECTED/CANCELED order looks identical by id alone, so the first version
could render false reassurance for a genuinely unprotected position. Fixed
via signals_notify._exit_order_resting, which actually checks the real
broker status (schwab_client.get_order_status) before trusting an order_id,
and falls back to a fresh trail_state.order_placed read for TRAIL's
known unextractable-order-id case. _build_sell_blocks/_exit_pending_blocks
now take a resting_confirmed bool computed by the caller, not a raw order_id
-- this file tests the block builders directly (resting_confirmed as a given
boolean); tests/test_fake_broker_retl_scenario.py-style integration coverage
for _exit_order_resting itself lives in test_exit_order_resting.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config as cfg
from signals_blocks import _build_sell_blocks
from signals_notify import _exit_pending_blocks, EXIT_REMINDER_MINUTES

POS = {'id': 1, 'ticker': 'GDXU', 'entry_price': 80.0, 'account': 'soxl_ira'}


def _text(blocks):
    return blocks[0]['text']['text']


def test_trail_resting_confirmed_is_informational_not_cancel_instruction(monkeypatch):
    monkeypatch.setattr(cfg, 'INTERACTIVE', False)
    text = _text(_build_sell_blocks(POS, 'TRAIL', 84.0, 83.5, resting_confirmed=True))
    assert 'Cancel Stop Loss' not in text
    assert 'resting' in text
    assert 'no action needed' in text


def test_trail_not_confirmed_keeps_manual_cancel_instruction(monkeypatch):
    monkeypatch.setattr(cfg, 'INTERACTIVE', False)
    text = _text(_build_sell_blocks(POS, 'TRAIL', 84.0, 83.5, resting_confirmed=False))
    assert 'Cancel Stop Loss order' in text


def test_trail_default_param_is_the_cautious_path(monkeypatch):
    # resting_confirmed defaults to False -- callers that don't explicitly
    # confirm get the cautious/manual text, not the reassuring one.
    monkeypatch.setattr(cfg, 'INTERACTIVE', False)
    text = _text(_build_sell_blocks(POS, 'TRAIL', 84.0, 83.5))
    assert 'Cancel Stop Loss order' in text


def test_tp_resting_confirmed_is_informational(monkeypatch):
    monkeypatch.setattr(cfg, 'INTERACTIVE', False)
    text = _text(_build_sell_blocks(POS, 'TP', 90.0, 90.0, resting_confirmed=True))
    assert 'Cancel Stop Loss' not in text
    assert 'no action needed' in text


def test_tp_not_confirmed_keeps_manual_cancel_instruction(monkeypatch):
    monkeypatch.setattr(cfg, 'INTERACTIVE', False)
    text = _text(_build_sell_blocks(POS, 'TP', 90.0, 90.0, resting_confirmed=False))
    assert 'Cancel Stop Loss order' in text


def test_time_resting_confirmed_is_informational_not_change_sl_instruction(monkeypatch):
    # Regression for the MEDIUM finding: TIME was left on the old text even
    # though it routes through the identical automated market-sell path as TP.
    monkeypatch.setattr(cfg, 'INTERACTIVE', False)
    text = _text(_build_sell_blocks(POS, 'TIME', 80.0, 80.0, resting_confirmed=True))
    assert 'Change Stop Loss' not in text
    assert 'no action needed' in text


def test_time_not_confirmed_keeps_original_instruction(monkeypatch):
    monkeypatch.setattr(cfg, 'INTERACTIVE', False)
    text = _text(_build_sell_blocks(POS, 'TIME', 80.0, 80.0, resting_confirmed=False))
    assert 'Change Stop Loss' in text


def test_exit_pending_reminder_is_informational_when_resting_confirmed(monkeypatch):
    monkeypatch.setattr('signals_notify._exit_order_resting', lambda pos, reason, order_id: True)
    exit_pending = {'reason': 'TRAIL', 'current_price': 84.0, 'target_price': 83.5, 'order_id': 999}
    blocks = _exit_pending_blocks(POS, exit_pending, reminder_num=1)
    text = _text(blocks)
    assert 'may still be open and unmanaged' not in text
    assert 'resting' in text
    assert 'should have filled' not in text


def test_exit_pending_reminder_escalates_after_repeated_cycles(monkeypatch):
    # The most dangerous spot for false reassurance: a real order that's
    # rested through several reminders without filling.
    monkeypatch.setattr('signals_notify._exit_order_resting', lambda pos, reason, order_id: True)
    exit_pending = {'reason': 'TRAIL', 'current_price': 84.0, 'target_price': 83.5, 'order_id': 999}
    blocks = _exit_pending_blocks(POS, exit_pending, reminder_num=3)
    text = _text(blocks)
    assert 'should have filled by now' in text
    assert 'Worth a look' in text


def test_exit_pending_reminder_keeps_unmanaged_warning_when_not_resting(monkeypatch):
    monkeypatch.setattr('signals_notify._exit_order_resting', lambda pos, reason, order_id: False)
    exit_pending = {'reason': 'TRAIL', 'current_price': 84.0, 'target_price': 83.5, 'order_id': 999}
    blocks = _exit_pending_blocks(POS, exit_pending, reminder_num=1)
    text = _text(blocks)
    assert 'may still be open and unmanaged' in text


def test_exit_pending_reminder_keeps_unmanaged_warning_when_unconfirmed(monkeypatch):
    # None (unconfirmed status check) must fail toward the cautious path,
    # same as False -- not toward the reassuring one.
    monkeypatch.setattr('signals_notify._exit_order_resting', lambda pos, reason, order_id: None)
    exit_pending = {'reason': 'TRAIL', 'current_price': 84.0, 'target_price': 83.5, 'order_id': 999}
    blocks = _exit_pending_blocks(POS, exit_pending, reminder_num=1)
    text = _text(blocks)
    assert 'may still be open and unmanaged' in text


def test_exit_pending_reminder_tp_time_also_use_resting_check(monkeypatch):
    # The reminder's resting check applies to every non-SL reason, not just
    # TRAIL -- confirms it's not accidentally TRAIL-only.
    monkeypatch.setattr('signals_notify._exit_order_resting', lambda pos, reason, order_id: True)
    for reason in ('TP', 'TIME'):
        exit_pending = {'reason': reason, 'current_price': 80.0, 'target_price': 80.0, 'order_id': 999}
        text = _text(_exit_pending_blocks(POS, exit_pending, reminder_num=1))
        assert 'may still be open and unmanaged' not in text
