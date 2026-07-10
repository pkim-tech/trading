#!/usr/bin/env python3
"""
Test _phase_emoji() (active_signals.py) -- the single glance-able lifecycle
ball shown in the reference table's Phase column.

Verifies every state transition:
  flat, no signal                     -> '' (idle, no emoji)
  flat, pending buy (signal fired)     -> yellow
  filled, not yet armed                -> green
  armed, sell order not yet placed     -> yellow
  armed, sell order placed (awaiting fill) -> yellow
  exit_pending set (sell signal fired) -> yellow

Usage:
    python scripts/test_phase_emoji.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import active_signals as a

YELLOW, GREEN, IDLE = '🟡', '🟢', ''

CASES = [
    ("flat, no signal",                          None, None,                                  IDLE),
    ("flat, pending buy signal fired",            None, {'order_placed': 0},                   YELLOW),
    ("flat, pending buy order placed",            None, {'order_placed': 1},                   YELLOW),
    ("filled, not yet armed",                     {'trail_state': {}}, None,                   GREEN),
    ("armed, sell order not placed",              {'trail_state': {'trailing': True}}, None,   YELLOW),
    ("armed, sell order placed, awaiting fill",   {'trail_state': {'trailing': True, 'order_placed': True}}, None, YELLOW),
    ("exit_pending set",                          {'trail_state': {'exit_pending': {'reason': 'TP'}}}, None, YELLOW),
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
