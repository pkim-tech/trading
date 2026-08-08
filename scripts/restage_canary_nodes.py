"""Nightly-only tool: closes every canary node's stale open position so the
next trading day's canary activity is always exercising CURRENT code, never
a position that opened days ago under logic that's since changed.

This is a regression-safety tool, not an arm/trail-testing-frequency tool --
after any change to execution code (strategies.py, signals_*.py,
active_signals.py), an old canary position that's mid-lifecycle doesn't
prove the new code works; it just proves whatever was true when it opened.
Restaging clean every night means tomorrow's canary results are always a
real test of today's code, without having to remember to do it by hand only
on days something changed. Inverse pairs (e.g. IVV/SPXU, both A) exist
specifically so at least one side of each pair gets a fresh entry promptly
regardless of which direction the market moves that day.

**Close-only, deliberately does NOT synthetically reopen** (revised
2026-08-08 after a paired Opus review, both independent-cold and contextual,
converged on two HIGH bugs in an earlier reopen-at-current-price design):
(1) a synthesized reopen never exercises the real entry/bounce-fill/
pending_buys mechanism the canary is supposed to prove, so a "met" result
after a restage overstated coverage; (2) setting entry_time to the evening
restage moment meant a next-day exit's entry/exit dates never matched
(`get_closed_trades_for_ticker_on_date` requires same-calendar-day entry AND
exit), making a restaged lifecycle's real outcome (including a genuinely
WRONG exit reason) invisible to `coverage_check.py` and silently
auto-explained as "no signal" -- actively burying a real bug instead of
flagging it. Closing only and letting the real daemon re-enter organically
(canary entry z-thresholds are ~0.1%, hair-trigger, so a fresh cross happens
almost daily once the node is free to re-enter) avoids all of this: the next
entry is real, dated correctly, and fully visible to every existing check.
Matches this project's general preference (2026-08-08) for widening organic
coverage over manufacturing/forcing a specific test state.

Scope is deliberately canary-only. Restaging LIVE nodes was raised and
explicitly narrowed in an earlier conversation: only a small, deliberately-
chosen set of real live nodes (the V5 go-live checklist's staged real-order
tests -- SH id=135/post_fill_topup, GDXU id=108/post_fill_topup, DPST
id=136/market_buy_placement, see CLAUDE.md) exist to confirm mechanisms
nothing else (fake_broker/paper/canary) can confirm, and those are staged
and managed by hand per docs/design.md's "Staged real-order test protocol"
-- never broadly/automatically reset. This script must never touch anything
outside version='canary'.

Deliberately NOT wired into active_signals.py's intraday poll loop -- run by
hand, overnight/after close only.

Usage: .venv/bin/python scripts/restage_canary_nodes.py [--dry-run]
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db
import signals_compute as compute


def list_canary_nodes():
    with db._conn() as c:
        rows = c.execute("SELECT * FROM watch_list WHERE version='canary' ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def _open_position_for_node(wl_id):
    """wl_id-scoped lookup -- db.get_open_position() is ticker-only, which is
    fine for canary tickers today (all unique across canary nodes) but not
    safe to assume in general, so this scopes explicitly rather than
    borrowing that helper's ticker-only assumption."""
    with db._conn() as c:
        row = c.execute("SELECT * FROM open_positions WHERE wl_id=?", (wl_id,)).fetchone()
    return dict(row) if row else None


def restage(node, pos, dry_run=False):
    ticker = node['ticker']
    df_hourly, _ = compute._load_cache(ticker)
    price = float(df_hourly.iloc[-1]['Close']) if df_hourly is not None and not df_hourly.empty else None
    now = datetime.now()

    print(f"  {ticker} (wl_id={node['id']}): open since {pos['signal_time']} at "
          f"entry={pos['entry_price']:.4f}. Closing stale position (exit_reason=RESTAGED) "
          f"so it can re-enter organically.")
    if dry_run:
        print(f"    [dry-run] would close position id={pos['id']}")
        return True

    closed = db.close_position(pos['id'], exit_signal_price=price, exit_price=price,
                                exit_time=now, exit_reason='RESTAGED')
    if not closed:
        # Row disappeared between listing and closing (e.g. a real organic
        # exit raced this script) -- log it rather than fail silently, so a
        # repeated/unexpected occurrence is visible instead of just a printed
        # line nobody sees (2026-08-08 review finding).
        db.log_coverage_event('canary_restage', 'dry_run', ticker=ticker, node_id=node['id'],
                               result='close_failed', detail=f"position id={pos['id']} already gone")
        print(f"    ! {ticker}: close_position returned False (already gone?)")
        return False
    db.log_coverage_event('canary_restage', 'dry_run', ticker=ticker, node_id=node['id'],
                           result='restaged', detail=f"prior_entry={pos['entry_price']:.4f}")
    print(f"    closed. Flat now, will re-enter on the next real signal.")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true', help="report what would restage without changing anything")
    args = ap.parse_args()

    db.ensure_tables()
    nodes = list_canary_nodes()
    restaged, flat = 0, 0
    print(f"{len(nodes)} canary node(s):")
    for node in nodes:
        pos = _open_position_for_node(node['id'])
        if pos is None:
            flat += 1
            continue
        if not pos.get('is_dry_run_sim'):
            # A canary node whose position somehow isn't is_dry_run_sim would be
            # real money -- refuse rather than silently touching it.
            print(f"  ! {node['ticker']} (wl_id={node['id']}): open position is NOT "
                  f"is_dry_run_sim -- refusing to restage, this would be a real order.")
            continue
        if restage(node, pos, dry_run=args.dry_run):
            restaged += 1
    print(f"\n{restaged} restaged, {flat} already flat, {len(nodes) - restaged - flat} skipped.")


if __name__ == '__main__':
    main()
