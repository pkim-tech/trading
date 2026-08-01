"""One-time fix, 2026-08-01: JNUG/JDST's revert to a real G-scenario pair
(docs/deep_backlog.md's 2026-08-01 entry) was label-deep only -- a second
independent Opus review found JNUG still carrying VOO's entire
TrailingExitZScoreBreakout config (never actually reverted to its real
2026-07-29 creation strategy, TrailingBothZScoreBreakout, confirmed via
watch_list_audit ids 248/249), and JDST still carrying max_hold_hours=2 --
the same defect mirror_canary_pair_config.py's headline fix corrected for
SPXU/QID/TWM/SDOW, but JDST has no MIRROR counterpart so nothing caught it.

Unlike the MIRROR pairs (which intentionally keep `strategy` untouched, since
those pairs are unrelated-sector instruments on purpose), JNUG/JDST are
supposed to be fully symmetric -- same underlying, same strategy, same
every field except ticker -- so this copies JDST's full real config onto
JNUG outright, not just the subset MIRROR touches.

Usage: .venv/bin/python scripts/fix_jnug_jdst_pair_config.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

with db._conn() as c:
    # JDST: fix its own max_hold_hours -- the same defect as SPXU/QID/TWM/SDOW.
    c.execute("UPDATE watch_list SET max_hold_hours=48 WHERE ticker='JDST' AND account='ira'")

    jdst = c.execute(
        "SELECT strategy, window, z_score_threshold, trail_sell_pct, fixed_sl, trail_buy_pct, "
        "arm_sell_pct, entry_timing, starting_notional, max_hold_hours "
        "FROM watch_list WHERE ticker='JDST' AND account='ira'"
    ).fetchone()

    c.execute("""
        UPDATE watch_list SET
            strategy=?, window=?, z_score_threshold=?, trail_sell_pct=?, fixed_sl=?,
            trail_buy_pct=?, arm_sell_pct=?, entry_timing=?, starting_notional=?,
            max_hold_hours=?, take_profit=NULL
        WHERE ticker='JNUG' AND account='ira'
    """, (*jdst,))
    c.commit()

jnug = db.get_watch_list_node(ticker='JNUG', account='ira')
jdst_after = db.get_watch_list_node(ticker='JDST', account='ira')
print(f"JNUG now: strategy={jnug['strategy']} window={jnug['window']} "
      f"z_score_threshold={jnug['z_score_threshold']} trail_sell_pct={jnug['trail_sell_pct']} "
      f"fixed_sl={jnug['fixed_sl']} trail_buy_pct={jnug['trail_buy_pct']} "
      f"arm_sell_pct={jnug['arm_sell_pct']} entry_timing={jnug['entry_timing']} "
      f"starting_notional={jnug['starting_notional']} max_hold_hours={jnug['max_hold_hours']}")
print(f"JDST now: max_hold_hours={jdst_after['max_hold_hours']}")
