"""Clears a stale trail_state['exit_pending'] record -- the leftover state when
an exit alert fired under an old config value (e.g. a since-reverted
max_hold_hours) and the condition it was tracking is no longer true, but
nothing ever clears exit_pending itself (only a real close or a fresh
sell_alerted re-fire does). A stale exit_pending renders as a misleading
"exit still pending" bubble/state even though nothing is actually pending.

Refuses to clear if the position's real current state still matches the
stale reason (TIME: held >= max_hold_hours; SL: price already through the
stop) -- that would mean the condition is genuinely still true and the
stuck order_id=None is a live automation gap, not stale leftover data.

Usage:
  .venv/bin/python scripts/clear_stale_exit_pending.py --position-id 18
  .venv/bin/python scripts/clear_stale_exit_pending.py --position-id 18 --force
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db
import signals_compute as sc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--position-id", type=int, required=True)
    ap.add_argument("--force", action="store_true",
                     help="clear even if the exit condition still looks true (not recommended)")
    args = ap.parse_args()

    pos = db.get_position_by_id(args.position_id)
    if pos is None:
        print(f"no open position with id={args.position_id}")
        return 1

    state = dict(pos.get("trail_state") or {})
    exit_pending = state.get("exit_pending")
    if not exit_pending:
        print(f"position {args.position_id} ({pos['ticker']}): no exit_pending to clear")
        return 0

    reason = exit_pending.get("reason")
    print(f"position {args.position_id} ({pos['ticker']}): exit_pending reason={reason} "
          f"order_id={exit_pending.get('order_id')} last_reminder_at={exit_pending.get('last_reminder_at')}")

    if reason == "TIME" and not args.force:
        df_hourly, _ = sc._load_cache(pos["ticker"])
        if df_hourly is not None:
            from datetime import datetime
            signal_time = datetime.strptime(pos["signal_time"], "%Y-%m-%d %H:%M:%S")
            held = sc._bars_held(df_hourly, signal_time)
            max_hold = pos.get("max_hold_hours")
            print(f"  held={held}h max_hold_hours={max_hold}h")
            if max_hold is not None and held >= max_hold:
                print("  REFUSING: held >= max_hold_hours -- TIME condition is still genuinely true, "
                      "this is a live automation gap, not stale state. Use --force to override.")
                return 1

    state.pop("exit_pending", None)
    db.update_position_trail_state(args.position_id, state)
    print(f"  cleared. trail_state now: {db.get_position_by_id(args.position_id).get('trail_state')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
