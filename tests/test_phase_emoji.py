#!/usr/bin/env python3
"""
Test _phase_emoji() (active_signals.py) -- the four-bubble lifecycle strip
(Signal / Filled / Armed / Sold) shown in the reference table's Phase column.

Verifies every state transition:
  flat, no signal                          -> all gray
  flat, pending buy signal fired            -> Signal yellow, rest gray
  flat, pending buy order placed            -> Signal green, Filled yellow, rest gray
  filled, not yet armed                     -> Signal+Filled green, rest gray
  armed, sell order not yet placed          -> Signal+Filled green, Armed yellow, Sold gray
  armed, sell order placed (awaiting fill)  -> Signal+Filled+Armed green, Sold gray
  exit_pending set (sell signal fired)      -> Signal+Filled+Armed green, Sold yellow

Usage:
    python tests/test_phase_emoji.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import active_signals as a

G, Y, N = '🟢', '🟡', '⚪'

CASES = [
    ("flat, no signal",                          None, None,                                  N*4),
    ("flat, pending buy signal fired",            None, {'order_placed': 0},                   Y+N+N+N),
    ("flat, pending buy order placed",            None, {'order_placed': 1},                   G+Y+N+N),
    ("filled, not yet armed",                     {'trail_state': {}}, None,                   G+G+N+N),
    ("armed, sell order not placed",              {'trail_state': {'trailing': True}}, None,   G+G+Y+N),
    ("armed, sell order placed, awaiting fill",   {'trail_state': {'trailing': True, 'order_placed': True}}, None, G+G+G+N),
    ("exit_pending set",                          {'trail_state': {'trailing': True, 'order_placed': True, 'exit_pending': {'reason': 'TP'}}}, None, G+G+G+Y),
]


def main():
    failures = 0
    for label, pos, pending_buy, expected in CASES:
        got = a._phase_emoji(pos, pending_buy)
        status = "OK" if got == expected else "FAIL"
        if got != expected:
            failures += 1
        print(f"[{status}] {label}: expected {expected!r}, got {got!r}")

    if failures:
        raise SystemExit(f"\n{failures} case(s) failed")
    print(f"\nAll {len(CASES)} cases passed")


if __name__ == '__main__':
    main()
