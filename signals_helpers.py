"""Small shared helpers with no cross-dependency on blocks/charts/handlers."""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import schwab_safety
import signals_config as cfg
import signals_db as db

_CORP_ACTION_ALERT_PATH = Path(__file__).parent / "cache" / "live" / "corporate_action_alerts.json"


def mode_tag(account):
    """'LIVE' / 'DRY-RUN' / 'UNKNOWN' display tag for an alert header -- shared
    by signals_notify.py and signals_blocks.py so every alert can show real
    vs. simulated status next to the account name, not the account name alone
    (found 2026-07-26: an account-only tag like '(ira)' reads identically
    whether it's real money or not -- the exact ambiguity this removes).
    Deliberately does NOT fall back to DRY-RUN for a None/unrecognized account
    the way signals_notify._coverage_mode does -- that helper only labels a
    log row, but this one labels human-facing risk, and a real-money position
    with a NULL account (e.g. a manual/legacy position) defaulting to a
    reassuring 'DRY-RUN' is the wrong failure direction (Opus review,
    2026-07-26). UNKNOWN is deliberately alarming instead."""
    if account is None:
        return "UNKNOWN"
    limits = schwab_safety.ACCOUNTS.get(account)
    if limits is None:
        return "UNKNOWN"
    return "DRY-RUN" if limits.dry_run else "LIVE"


def log_poll(msg):
    """Appends one [poll] trace line to VERBOSE_LOG_PATH -- every price/bar a
    live-trading decision point actually used, kept out of the human-readable
    log so day-to-day monitoring isn't buried. Built 2026-07-22 after a real
    stale-cache bug (HIBL paper trade) went unnoticed with no way to see what
    price/bar each poller had actually read at decision time."""
    try:
        with open(cfg.VERBOSE_LOG_PATH, "a") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} [poll] {msg}\n")
    except Exception:
        pass


def _load_corp_action_alerts():
    if not _CORP_ACTION_ALERT_PATH.exists():
        return {}
    try:
        return json.loads(_CORP_ACTION_ALERT_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def already_alerted_corp_action(ticker) -> bool:
    """Prevents re-alerting every ~30s poll while a held position's entry_price
    stays stale -- one alert per detected discontinuity, not one per check."""
    return ticker in _load_corp_action_alerts()


def mark_corp_action_alerted(ticker):
    state = _load_corp_action_alerts()
    state[ticker] = True
    _CORP_ACTION_ALERT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CORP_ACTION_ALERT_PATH.write_text(json.dumps(state))


def clear_corp_action_alert(ticker):
    """Called once the correction is applied -- lets a genuinely new,
    separate discontinuity for the same ticker alert again later."""
    state = _load_corp_action_alerts()
    state.pop(ticker, None)
    _CORP_ACTION_ALERT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CORP_ACTION_ALERT_PATH.write_text(json.dumps(state))


def _add_trading_hours(start, hours):
    """Advance `start` by `hours` trading bars (market hours 9-15, Mon-Fri only)."""
    dt = start
    remaining = hours
    while remaining > 0:
        dt += timedelta(hours=1)
        if dt.weekday() < 5 and 9 <= dt.hour <= 15:
            remaining -= 1
    return dt


# Known corporate-action ratios to match against, not an arbitrary magnitude
# cutoff -- a 3x leveraged ETF can plausibly crash >66% in one real extreme
# day (ratio > 3), so magnitude alone can't tell a real crash from a split.
# A split ratio is always a clean, round number; a real market move landing
# within tolerance of one by coincidence is vanishingly unlikely regardless
# of how large the move is.
_SPLIT_RATIOS = (1.5, 2, 2.5, 3, 4, 5, 10, 15, 20, 25, 30, 40, 50)


def detect_price_discontinuity(current_price, reference_price, tolerance=0.03):
    """Returns the reference/current ratio if it closely matches a known
    split-like factor (or its inverse, for a reverse split), None otherwise.
    Detection only -- callers decide what to do with a hit; this doesn't
    freeze/block/notify on its own. (Found live 2026-07-15: KORU's ~20:1
    split silently passed every SL/arm check since nothing compared current
    price against the stale reference price.)"""
    if not current_price or not reference_price:
        return None
    ratio = reference_price / current_price
    for r in _SPLIT_RATIOS:
        if abs(ratio - r) / r < tolerance or abs(ratio - 1 / r) / (1 / r) < tolerance:
            return ratio
    return None


def nearest_split_factor(ratio):
    """Given a raw ratio that already matched in detect_price_discontinuity,
    returns the clean round-number factor it matched (e.g. 20.0, not the
    noisy 20.34 an actual fill price would produce) -- used for the proposed
    correction, since guessing off the clean factor is more sensible than the
    raw ratio. Returns None if ratio doesn't match any known factor (shouldn't
    happen if only ever called after a positive detect_price_discontinuity)."""
    candidates = list(_SPLIT_RATIOS) + [1 / r for r in _SPLIT_RATIOS]
    return min(candidates, key=lambda r: abs(ratio - r))


def _proximity_emoji(pct_away):
    if pct_away < 5:
        return "🔶"
    if pct_away < 15:
        return "🟡"
    return "⚪"


def _pos_key(pos):
    """Dict/set key for last_seen_bar, shared between active_signals.py's main
    loop/_scan_pinned_exit_arm and paper_trading.check_paper_sells (one dict,
    passed by reference across both) -- pos['wl_id'] when available, else a
    (ticker, window) fallback. A bare None would collide across every legacy/
    unbackfillable position (wl_id=NULL, see docs/backlog_cache.md's wl_id
    refactor entry): one such position's last_seen_bar write would make a
    second, unrelated None-wl_id position's at_bar_close check silently read
    as 'already seen', skipping its real bar-close exit evaluation."""
    return pos['wl_id'] if pos.get('wl_id') is not None else (pos['ticker'], pos['window'])


def _existing_position_note(ticker, wl_id=None):
    """Formats the already-open position for a duplicate-attempt warning, so the
    user doesn't have to go run scripts/open_positions_status.py separately.
    Prefers wl_id (unambiguous) when the caller has it -- ticker alone
    arbitrarily picks the most-recently-entered position if 2+ nodes share
    a ticker."""
    pos = db.get_open_position_by_wl_id(wl_id) if wl_id is not None else db.get_open_position(ticker)
    if not pos:
        return "check `open_positions` if unsure what's live."
    return (f"currently open: `${pos['entry_price']:.4f}` x `{pos['shares']}` shares, "
            f"entered `{pos['entry_time']}` ({pos['account']}).")


def _last_sale_recovery(node):
    """Estimated next-buy notional: proceeds (exit_price * shares) from this node's
    most recent closed trade, so sizing roughly compounds off the last recycle. Falls
    back to `starting_notional` (the node's own watch_list.starting_notional column)
    only if no closed trade has shares logged yet -- callers must supply a node with
    it set (no hidden flat-$50k default here) so a new pilot with a different real
    book size (e.g. GDXD's $5k) can't silently get sized like everyone else's $50k.
    Narrows by (ticker, strategy, version, window, account) -- trade_log has no
    wl_id column, so this is the closest available match to "this node's own
    history", not just "this ticker's most recent trade regardless of which node
    it belonged to" (a real live-sizing bug once 2+ nodes share a ticker with
    different starting_notional/account). A rough estimate, not a live capital
    feed -- doesn't know about other trades competing for the same account's cash
    in between. Excludes is_dry_run_sim rows -- a synthesized dry-run fill's
    proceeds must never size a real order, including in the same account if its
    dry_run flag is later flipped to live (Opus review 2026-07-26)."""
    ticker = node['ticker']
    with db._conn() as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT exit_price, shares FROM trade_log WHERE ticker=? AND strategy=? AND version=? "
            "AND window=? AND COALESCE(account,'')=COALESCE(?,'') AND exit_price IS NOT NULL "
            "AND shares IS NOT NULL AND is_dry_run_sim=0 ORDER BY exit_time DESC LIMIT 1",
            (ticker, node.get('strategy'), node.get('version'), node.get('window'), node.get('account')),
        ).fetchone()
    if row and row['exit_price'] and row['shares']:
        return row['exit_price'] * row['shares']
    starting_notional = node.get('starting_notional')
    if starting_notional is None:
        raise ValueError(f"_last_sale_recovery({ticker}): no trade history and no starting_notional configured")
    return starting_notional


# Provisional placeholder, not derived from output/open_window_volatility_summary.csv
# as-is -- that data measures drift across the whole ambient-polling window and
# conflates detection drift (now eliminated for pinned open_check moments by
# schwab_client.get_session_open_price) with real fill-time execution slippage
# (the HIBL multi-leg-fill observation, still real). Follow-up: adapt
# scripts/sim_open_window_volatility.py (or a new variant) to isolate fill-time-only
# slippage from the openPrice reference, once real data exists (Part 4, Section 3).
DEFAULT_MARKET_ENTRY_PAD_PCT = 1.0


def buy_order_sizing(node, sig, pad_pct=1.0, market_pad_pct=DEFAULT_MARKET_ENTRY_PAD_PCT):
    """Worst-case trailing-buy sizing: a real trailing-buy order fills once price
    bounces trail_buy_pct% off a running low that can fall further before that, so
    the fill price is unbounded relative to the signal-time price. Sizing off
    trail_buy_pct + pad_pct (not trail_buy_pct alone) covers ordinary same-day
    slippage between signal detection and the order actually going live at the
    broker (Part 3, 2026-07-21) -- pad_pct=1.0 is the default same-day pad.
    The overnight gap-correction path (signals_notify.check_gap_resize, branch B)
    is a plain MARKET order, sized separately (flat pad on a live quote, not this
    trailing-bounce formula). The non-trailing (plain market-buy) branch gets its
    own, smaller market_pad_pct (Part 4, Section 3) -- a market order fills near
    the signal-time price, not an unbounded bounce, so it needs less padding than
    the trailing-buy branch. Shared by _build_buy_blocks and the
    automated-placement path so there's one sizing formula, not two."""
    ticker = sig['ticker']
    price = sig['current_price']
    target_notional = _last_sale_recovery(node)
    trailing_buy = db._is_trailing_buy(node)
    trail_buy_pct = node.get('trail_buy_pct') or 0.0
    if trailing_buy:
        shares = int(target_notional // (price * (1 + (trail_buy_pct + pad_pct) / 100)))
    else:
        shares = int(target_notional // (price * (1 + market_pad_pct / 100)))
    return {
        'shares': shares, 'target_notional': target_notional,
        'trailing_buy': trailing_buy, 'trail_buy_pct': trail_buy_pct, 'price': price,
    }


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
