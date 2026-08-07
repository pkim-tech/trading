"""Pre-market prep report: daemon health + per-live-node action list + real
overnight-gap flag on any resting trailing-buy order.

Usage:
  python scripts/premarket_prep.py [watchlist_id]

Replaces manually re-deriving "what do I need to do before the open" each
morning by hand. Three sections:

1. Daemon check -- is active_signals.py running (reuses daemon_status's PID
   lookup). check_gap_resize only fires from inside the live run_loop at its
   9:15-9:29 ET window; if the daemon isn't up by then, no ticker gets
   gap-corrected that day, silently.
2. Per live-mode node action:
   - held position -> no action
   - resting trailing-buy order (pending_buys, order_placed=True) -> gap
     check using the exact same trigger math and price source
     (schwab_client.get_current_price) as signals_notify.check_gap_resize,
     so a "GAP CLEARED" flag here means gap_resize will (or should have)
     acted on it -- a real pre-open verification point, not a guess.
   - no pending order, TrailingExitZScoreBreakout, not yet triggered ->
     ACTION: stage the pre-market absurd-low limit order (operational_limits.md).
   - no pending order, TrailingBothZScoreBreakout, not yet triggered -> no
     action yet, waiting for a bar-close z-cross.
3. Research-mode / canary nodes listed separately, informational only --
   never a real action item.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import active_signals as a
import signals_db as db
import schwab_client
from scripts.daemon_status import _find_daemon_pid

_MARKET_OPEN_HOUR, _MARKET_OPEN_MIN = 9, 30
_GAP_WINDOW_START = (9, 15)


def _daemon_section():
    pid = _find_daemon_pid()
    if not pid:
        print("DAEMON: NOT RUNNING -- check_gap_resize will not fire at 9:15 today. Start it now.")
        return False
    print(f"DAEMON: running (pid {pid})")
    return True


def _gap_check(node, pending):
    """Mirrors signals_notify.check_gap_resize's trigger math and price
    source exactly, read-only -- reports what gap_resize will see/do, does
    not place or cancel any order itself."""
    trail_buy_pct = node.get('trail_buy_pct') or 0.0
    running_low = pending['running_low'] or pending['signal_price']
    buy_trigger = running_low * (1 + trail_buy_pct / 100)
    try:
        current_price = schwab_client.get_current_price(node['ticker'])
    except Exception as e:
        return f"price lookup failed ({e}) -- can't evaluate gap"
    if current_price >= buy_trigger:
        return (f"GAP CLEARED -- current ${current_price:.4f} >= trigger ${buy_trigger:.4f} -- "
                f"gap_resize should cancel+replace this at 9:15; verify it actually did at Schwab")
    return f"resting order intact -- current ${current_price:.4f} < trigger ${buy_trigger:.4f}, no gap"


def main(watchlist_id=None):
    daemon_up = _daemon_section()
    print()

    watchlist_id = watchlist_id or a.get_active_watchlist_id()
    wl = a.get_watchlist(watchlist_id)
    open_by_wl_id = {p['wl_id']: p for p in db.get_open_positions() if p.get('wl_id')}
    pending_by_wl_id = {p['node']['id']: p for p in db.get_pending_buys() if p.get('order_placed')}

    live_nodes = [n for n in wl if n.get('state') != 'paper']
    other_nodes = [n for n in wl if n.get('state') == 'paper']

    print(f"LIVE NODES ({len(live_nodes)}):")
    for n in sorted(live_nodes, key=lambda n: n['ticker']):
        tag = " [CANARY]" if n.get('version') == 'canary' else ""
        label = f"{n['ticker']:<6} {n['strategy']:<28} {n.get('account'):<10}{tag}"

        pos = open_by_wl_id.get(n['id'])
        if pos is not None:
            print(f"  {label}  HELD, no action")
            continue

        pending = pending_by_wl_id.get(n['id'])
        if pending is not None:
            print(f"  {label}  {_gap_check(n, pending)}")
            continue

        if n['strategy'] == 'TrailingExitZScoreBreakout':
            print(f"  {label}  ACTION: stage pre-market absurd-low limit order")
        else:
            print(f"  {label}  waiting for z-cross, no action yet")

    print(f"\nOTHER NODES ({len(other_nodes)}, research/paper -- informational only):")
    for n in sorted(other_nodes, key=lambda n: n['ticker']):
        tag = " [CANARY]" if n.get('version') == 'canary' else ""
        print(f"  {n['ticker']:<6} {n['strategy']:<28} {n.get('account'):<10} {n.get('state')}{tag}")


if __name__ == '__main__':
    wl_arg = int(sys.argv[1]) if len(sys.argv) > 1 else None
    main(wl_arg)
