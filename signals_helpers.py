"""Small shared helpers with no cross-dependency on blocks/charts/handlers."""
import sqlite3
from datetime import timedelta

import signals_db as db


def _add_trading_hours(start, hours):
    """Advance `start` by `hours` trading bars (market hours 9-15, Mon-Fri only)."""
    dt = start
    remaining = hours
    while remaining > 0:
        dt += timedelta(hours=1)
        if dt.weekday() < 5 and 9 <= dt.hour <= 15:
            remaining -= 1
    return dt


def _proximity_emoji(pct_away):
    if pct_away < 5:
        return "🔶"
    if pct_away < 15:
        return "🟡"
    return "⚪"


def _existing_position_note(ticker):
    """Formats the already-open position for a duplicate-attempt warning, so the
    user doesn't have to go run scripts/open_positions_status.py separately."""
    pos = db.get_open_position(ticker)
    if not pos:
        return "check `open_positions` if unsure what's live."
    return (f"currently open: `${pos['entry_price']:.4f}` x `{pos['shares']}` shares, "
            f"entered `{pos['entry_time']}` ({pos['account']}).")


def _last_sale_recovery(ticker):
    """Estimated next-buy notional: proceeds (exit_price * shares) from this ticker's
    most recent closed trade, so sizing roughly compounds off the last recycle instead
    of always assuming a flat $50k. Falls back to $50k if no closed trade has shares
    logged yet. A rough estimate, not a live capital feed -- doesn't know about other
    trades competing for the same account's cash in between."""
    with db._conn() as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT exit_price, shares FROM trade_log WHERE ticker=? AND exit_price IS NOT NULL "
            "AND shares IS NOT NULL ORDER BY exit_time DESC LIMIT 1", (ticker,)
        ).fetchone()
    if row and row['exit_price'] and row['shares']:
        return row['exit_price'] * row['shares']
    return 50_000


_PHASE_GREY, _PHASE_YELLOW, _PHASE_GREEN = '⚪', '🟡', '🟢'


def _phase_emoji(pos, pending_buy):
    """Four-bubble lifecycle strip, left to right: Signal / Filled / Armed / Sold.
    Each bubble is gray (not reached), yellow (in progress, awaiting confirmation),
    or green (confirmed complete) -- a position can be filled without being armed,
    so those get separate bubbles rather than one combined ball."""
    if pos is None:
        if pending_buy is None:
            return _PHASE_GREY * 4
        order_placed = pending_buy.get('order_placed')
        signal = _PHASE_GREEN if order_placed else _PHASE_YELLOW
        fill = _PHASE_YELLOW if order_placed else _PHASE_GREY
        return f"{signal}{fill}{_PHASE_GREY}{_PHASE_GREY}"

    trail_state = pos.get('trail_state') or {}
    if trail_state.get('trailing'):
        armed = _PHASE_GREEN if trail_state.get('order_placed') else _PHASE_YELLOW
    else:
        armed = _PHASE_GREY
    sold = _PHASE_YELLOW if trail_state.get('exit_pending') else _PHASE_GREY
    return f"{_PHASE_GREEN}{_PHASE_GREEN}{armed}{sold}"
