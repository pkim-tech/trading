"""One-time fix: gives each of the 6 new inverse/parallel canaries (SPXU, QID,
TWM, SDOW, FAZ, JNUG) the exact same hair-trigger design as its A-F counterpart
(IVV/QQQ/IWM/DIA/XLF/VOO, docs/deep_backlog.md's 2026-07-23 entry), instead of
the generic identical config they were accidentally created with 2026-07-29.
Mirrors config only (arm/SL/trail/entry_timing/starting_notional/
max_hold_hours) -- ticker/strategy/account/mode untouched.

max_hold_hours added 2026-08-01 -- the original field list omitted it, so
SPXU/QID/TWM/SDOW silently kept the generic-config default (2h) instead of
their counterpart's real design value (48h for the A/B/C/D scenarios; only
FAZ was coincidentally correct, since the F scenario's own design genuinely
wants a short 2h hold). This is what broke canary_full_lifecycle (SPXU) and
canary_overnight_carry (SDOW) once the 2026-07-31 entry-abandon timeout
started reusing max_hold_hours as its cancel threshold -- a 2h hold forces a
TIME exit or entry-abandon long before either scenario's real exit path
(TRAIL / overnight pending carry) can play out. Found via the 2026-08-01
nightly EOD review's canary deviation investigation.

JNUG->VOO was added, then reverted, same day (2026-08-01) -- an independent
Opus review of the max_hold_hours fix found JNUG (then treated as the
E-scenario market-buy mirror) missing from MIRROR entirely, still carrying
max_hold_hours=2 against VOO's real 24. Fixed, then immediately reconsidered:
JNUG's own `label` field already said "gold miners bull (replaces GDXU
pairing)" -- its real original design (2026-07-28) was pairing with JDST as a
genuine same-underlying bull/bear pair (JNUG/JDST are both junior-gold-miner
2x ETFs, unlike every other MIRROR pair here, which are unrelated-sector
instruments that merely happen to be inverse products). JNUG was reassigned
to the E-scenario 2026-07-29 for unclear reasons, orphaning JDST. Reverted:
JNUG removed from MIRROR (E stays VOO-only -- it never needed a symmetry
partner the way A-D do), max_hold_hours reverted to 2, both nodes relabeled.
See docs/backlog_cache.md's 2026-08-01 entry for the still-open idea of an
actual correlation check between the two (not built yet -- today's fix only
restores their config/labels, not new verification logic).

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
    # JNUG deliberately NOT here -- see module docstring (2026-08-01):
    # reverted its brief E/VOO mirroring, restored to pairing with JDST.
}

with db._conn() as c:
    for new_ticker, counterpart in MIRROR.items():
        counterpart_row = c.execute(
            "SELECT trail_buy_pct, fixed_sl, trail_sell_pct, arm_sell_pct, starting_notional, entry_timing, "
            "max_hold_hours "
            "FROM watch_list WHERE ticker=? AND account='ira' AND version='canary'",
            (counterpart,)
        ).fetchone()
        if counterpart_row is None:
            print(f"  [skip] {new_ticker}: counterpart {counterpart} not found")
            continue
        c.execute("""
            UPDATE watch_list SET
                trail_buy_pct=?, fixed_sl=?, trail_sell_pct=?, arm_sell_pct=?,
                starting_notional=?, entry_timing=?, max_hold_hours=?
            WHERE ticker=? AND account='ira' AND version='canary'
        """, (*counterpart_row, new_ticker))
        print(f"  {new_ticker:6s} <- mirrored from {counterpart} "
              f"(trail_buy_pct={counterpart_row[0]}% fixed_sl={counterpart_row[1]}% "
              f"trail_sell_pct={counterpart_row[2]}% arm_sell_pct={counterpart_row[3]}% "
              f"starting_notional=${counterpart_row[4]:,.0f} entry_timing={counterpart_row[5]} "
              f"max_hold_hours={counterpart_row[6]})")
    c.commit()
