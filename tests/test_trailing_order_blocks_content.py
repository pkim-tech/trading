"""Tests for signals_notify._trailing_order_blocks -- the manual arm
confirmation Slack alert, shown when a human needs to place the trailing-
sell order by hand (out-of-scope ticker, or the automated placement
failed). Found via arming-logic walkthrough, 2026-07-31: this alert
previously gave neither the share count nor the trail percentage (the two
numbers actually needed to place the order), and never told the user to
cancel the existing protective stop first -- unlike the automated path,
which treats that as mandatory (an atomic replace specifically to avoid
both a stop-loss and a trailing-sell resting simultaneously for the same
shares)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_notify


def _pos(**over):
    base = dict(id=1, ticker='TEST_ARM', account='ira', entry_price=100.0,
                shares=50, trail_sell_pct=3.0, sl_order_id=None)
    base.update(over)
    return base


def _text(blocks):
    return "\n".join(b['text']['text'] for b in blocks if b.get('type') == 'section')


def test_includes_share_count_and_trail_pct():
    blocks = signals_notify._trailing_order_blocks(_pos(), current_price=105.0)
    text = _text(blocks)
    assert '50' in text, "share count missing from the manual arm alert"
    assert '3' in text, "trail percentage missing from the manual arm alert"


def test_includes_cancel_instruction_when_sl_order_exists():
    blocks = signals_notify._trailing_order_blocks(_pos(sl_order_id=98765), current_price=105.0)
    text = _text(blocks)
    assert 'Cancel' in text or 'cancel' in text
    assert '98765' in text


def test_no_cancel_instruction_when_no_sl_order_on_file():
    blocks = signals_notify._trailing_order_blocks(_pos(sl_order_id=None), current_price=105.0)
    text = _text(blocks)
    assert 'Cancel' not in text and 'cancel' not in text


def test_falls_back_gracefully_when_shares_or_trail_pct_missing():
    blocks = signals_notify._trailing_order_blocks(_pos(shares=None), current_price=105.0)
    text = _text(blocks)
    assert 'unavailable' in text.lower()


def test_reminder_variant_also_includes_order_details():
    blocks = signals_notify._trailing_order_blocks(_pos(), current_price=105.0, reminder_num=2)
    text = _text(blocks)
    assert '50' in text
    assert '3' in text
    assert 'STILL PENDING' in text
