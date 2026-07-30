"""One-time fix: gives each of the 5 new inverse-pair canaries (SPXU, QID,
TWM, SDOW, FAZ) the exact same hair-trigger design as its A-F counterpart
(IVV/QQQ/IWM/DIA/XLF, docs/deep_backlog.md's 2026-07-23 entry), instead of
the generic identical config they were accidentally created with 2026-07-29.
Mirrors config only (arm/SL/trail/entry_timing/starting_notional) -- ticker/
strategy/account/mode untouched.

Usage: .venv/bin/python scripts/mirror_canary_pair_config.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

# new_ticker -> (counterpart_ticker, entry_timing to also match, since only
# C/IWM uses open_check; the rest use 'close')
MIRROR = {
    'SPXU': 'IVV',   # A: full happy path
    'QID':  'QQQ',   # B: early-SL path
    'TWM':  'IWM',   # C: pinned/open_check entry
    'SDOW': 'DIA',   # D: overnight carry
    'FAZ':  'XLF',   # F: TIME-only exit
}

with db._conn() as c:
    for new_ticker, counterpart in MIRROR.items():
        counterpart_row = c.execute(
            "SELECT trail_buy_pct, fixed_sl, trail_sell_pct, arm_sell_pct, starting_notional, entry_timing "
            "FROM watch_list WHERE ticker=? AND account='ira' AND version='canary'",
            (counterpart,)
        ).fetchone()
        if counterpart_row is None:
            print(f"  [skip] {new_ticker}: counterpart {counterpart} not found")
            continue
        c.execute("""
            UPDATE watch_list SET
                trail_buy_pct=?, fixed_sl=?, trail_sell_pct=?, arm_sell_pct=?,
                starting_notional=?, entry_timing=?
            WHERE ticker=? AND account='ira' AND version='canary'
        """, (*counterpart_row, new_ticker))
        print(f"  {new_ticker:6s} <- mirrored from {counterpart} "
              f"(trail_buy_pct={counterpart_row[0]}% fixed_sl={counterpart_row[1]}% "
              f"trail_sell_pct={counterpart_row[2]}% arm_sell_pct={counterpart_row[3]}% "
              f"starting_notional=${counterpart_row[4]:,.0f} entry_timing={counterpart_row[5]})")
    c.commit()
