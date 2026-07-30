"""One-time fix: converts JNUG from a generic TrailingBothZScoreBreakout node
into the missing E-mirror (VOO's TrailingExitZScoreBreakout immediate-
market-buy path) -- the 6th A-F scenario had no inverse counterpart until
now. Copies every strategy-relevant field from VOO's real node, including
the strategy class itself (not just parameters, since E is mechanically a
different code path -- start_paper_market_buy / immediate market buy, no
trailing-buy bounce phase).

Usage: .venv/bin/python scripts/convert_jnug_to_e_mirror.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

with db._conn() as c:
    voo = c.execute(
        "SELECT strategy, window, take_profit, stop_loss, trail_buy_pct, fixed_sl, "
        "trail_sell_pct, arm_sell_pct, starting_notional, entry_timing, z_score_threshold "
        "FROM watch_list WHERE ticker='VOO' AND account='ira' AND version='canary'"
    ).fetchone()
    if voo is None:
        print("VOO canary node not found")
        sys.exit(1)
    print(f"VOO real config: {dict(voo)}")

    c.execute("""
        UPDATE watch_list SET
            strategy=?, window=?, take_profit=?, stop_loss=?, trail_buy_pct=?, fixed_sl=?,
            trail_sell_pct=?, arm_sell_pct=?, starting_notional=?, entry_timing=?, z_score_threshold=?
        WHERE ticker='JNUG' AND account='ira' AND version='canary'
    """, tuple(voo))
    c.commit()
    print("JNUG converted to mirror VOO (E: TrailingExit immediate market-buy path).")
