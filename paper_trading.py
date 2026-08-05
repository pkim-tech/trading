"""
Paper-trading simulation for schwab_safety.AUTOMATION_ENABLED_TICKERS tickers
running in research mode: a continuous (every-poll) trailing-buy bounce-fill
tracker, plus the same exit state machine used for real positions
(signals_compute.check_sell_condition), writing to paper_positions/
paper_trade_log/paper_pending_buys instead of the real tables.

This never calls schwab_client/schwab_safety -- it's a pure simulation, run
independently of dry_run, meant to show what the automation engine would have
caught and produce real fill/P&L data before any ticker is flipped back to
real execution. See docs/backlog_cache.md for the granularity caveat: fills
are sampled at POLL_SECS cadence, not tick-perfect against a real broker.
"""
from datetime import datetime

import signals_db as db
from signals_compute import _current_price, _load_cache, _bars_held, check_sell_condition
from signals_blocks import _post_message
from signals_helpers import buy_order_sizing, log_poll, resolve_at_bar_close


def start_paper_buy(node, sig):
    """Called from active_signals._scan_buy_signals on a BUY for a research-mode,
    automation-enabled ticker. Dispatches to start_paper_market_buy for a
    non-trailing-buy node (Part 4, Section 7) -- a market order fills near-
    immediately, no bounce-fill phase to simulate, unlike a trailing buy."""
    ticker = sig['ticker']
    if node.get('daily_sync_halted_at'):
        return
    if not db._is_trailing_buy(node):
        start_paper_market_buy(node, sig)
        return
    if db.get_paper_pending_buy(node['id']) or db.get_open_position_by_wl_id(node['id'], paper=True):
        return
    db.add_paper_pending_buy(node, sig)
    if node.get('paper_alert_verbose'):
        _post_message(
            f"🧪 PAPER BUY SIGNAL — {ticker}  ${sig['current_price']:.4f}  z={sig['z_score']:+.2f}"
        )


def start_paper_market_buy(node, sig):
    """Market-buy mirror of the trailing-buy pending/running-low tracking above
    (Part 4, Section 7) -- a market order fills essentially at placement, so
    there's no bounce-fill phase to simulate. Sizes via the same
    buy_order_sizing real code path (market_pad_pct branch) so paper trading
    faithfully dry-runs the real sizing/pad, not a simplified stand-in, and
    opens the paper position directly."""
    ticker = sig['ticker']
    if node.get('daily_sync_halted_at'):
        return
    if db.get_open_position_by_wl_id(node['id'], paper=True):
        return
    # Explicit target_notional (starting_notional) -- buy_order_sizing's
    # default (_last_sale_recovery) queries the real trade_log, which paper
    # fills never land in (paper_trade_log instead); without this override a
    # research node sharing (ticker, strategy, version, window, account) with
    # a real live node (the deliberate DPST live+research pairing, see
    # CLAUDE.md) could size a paper position off unrelated real trade
    # proceeds. Found by review while fixing the identical hazard in the
    # trailing-buy sizing path below -- pre-existing here, not introduced
    # this session, but the same fix applies.
    sizing = buy_order_sizing(node, sig, target_notional=node.get('starting_notional') or 50000)
    if sizing['shares'] < 1:
        return
    db.open_position(node, sig['current_price'], sig['last_bar'], sig['current_price'], datetime.now(),
                      shares=sizing['shares'], paper=True)
    db.log_coverage_event("entry_fill", "paper", ticker=ticker, node_id=node.get('id'), result="market_filled",
                           detail=f"shares={sizing['shares']} price={sig['current_price']:.4f}")
    if node.get('paper_alert_verbose'):
        _post_message(f"🧪 PAPER MARKET BUY — {ticker}  {sizing['shares']}sh @ ${sig['current_price']:.4f}")


def update_paper_buys():
    """Called unconditionally every poll (not gated to signal windows) -- a real
    trailing buy can fill any time after the signal fires.

    Also enforces the same entry-abandon timeout as signals_notify.
    check_entry_abandon (see that docstring for the kernel-parity rationale)
    -- a paper trailing buy that never bounces has no real broker order to
    leak, but would otherwise wait forever and never produce the closed
    trade/P&L data paper trading exists to generate, silently understating
    how often this strategy shape simply gives up on an entry. Checked AFTER
    the bounce-fill check below, matching the kernel's own per-bar order
    (backtester.py's _simulate_trail_both: check the fill first, only fall
    through to `wait_bars >= max_hours_to_hold` if this bar didn't fill) --
    an earlier version checked abandon first, which could abandon a pending
    buy on the exact poll it would otherwise have filled (found by review
    before landing)."""
    for pb in db.get_paper_pending_buys():
        ticker = pb['ticker']
        node = pb['node']
        wl_id = node.get('id')
        price, _ = _current_price(ticker)
        # Abandon-eligibility (bars_held) only needs cached bar history, not
        # a fresh live price -- computed regardless of whether price below
        # is available, so a stale/unavailable price (compute._current_price's
        # 90min staleness guard) can't silently swallow an overdue abandon by
        # continuing past it before the check ever runs (found by review: an
        # earlier version's `if price is None: continue` sat before the
        # abandon check, meaning a position resting past max_hold_hours with
        # no fresh price data -- easily true after a long enough wait --
        # would never actually get abandoned).
        overdue = bool(wl_id) and _bars_held(
            _load_cache(ticker)[0], datetime.strptime(pb['signal_time'], '%Y-%m-%d %H:%M:%S')
        ) >= (node.get('max_hold_hours') or float('inf'))
        if price is None:
            if overdue:
                db.clear_paper_pending_buy(wl_id)
                db.log_coverage_event("entry_abandon_timeout", "paper", ticker=ticker, node_id=wl_id,
                                       result="abandoned", detail=f"max={node.get('max_hold_hours')} price_unavailable=1")
                live_node = db.get_watch_list_node_by_id(wl_id)
                if live_node and live_node.get('paper_alert_verbose'):
                    _post_message(f"🧪⏱️ {ticker} — paper trailing buy never bounced within "
                                  f"{node.get('max_hold_hours')}h — entry abandoned.")
            continue
        running_low = min(pb['running_low'], price)
        trail_buy_pct = node.get('trail_buy_pct') or 0.0
        trigger = running_low * (1 + trail_buy_pct / 100)
        log_poll(f"{ticker} paper_update_buys price={price:.4f} running_low={running_low:.4f} trigger={trigger:.4f}")
        if price > running_low and price >= trigger:
            # Flat starting_notional/price -- deliberately NOT buy_order_sizing's
            # worst-case padded formula (trail_buy_pct+pad_pct), even though
            # real/dry-run both use it at signal/placement time. That padding
            # exists because a REAL order is sized before the fill price is
            # known; here the fill price is already known, and the real
            # position's true end state (after _reconcile_fill's post-fill
            # top-up buy, signals_notify.py) converges to
            # target_notional/fill_price regardless of the initial pad -- this
            # flat formula already matches that real end state directly
            # (confirmed via review: applying the pad here, as a first
            # version of this fix did, would have undersized paper positions
            # relative to what a real position actually ends up holding,
            # the opposite of the intended alignment).
            starting_notional = node.get('starting_notional') or 50000
            shares = int(starting_notional // price)
            if shares < 1:
                print(f"  [paper] {ticker} bounce-fill at ${price:.4f} too small to size a share — dropping pending buy")
                db.clear_paper_pending_buy(node['id'])
                continue
            # hold-time origin: fill time for both signal_time and entry_time,
            # not the pending buy's original signal_time -- same fix and
            # rationale as signals_notify._reconcile_buy_fill (2026-07-31).
            fill_time = datetime.now()
            # signal_bar_time: the real bar-aligned signal timestamp (pb['signal_time'] --
            # paper_pending_buys rows are written with sig['last_bar'], see add_paper_pending_buy),
            # captured before the pending row is cleared below. signal_time itself must stay
            # fill_time (the 2026-07-31 hold-budget fix above) -- this is purely so
            # reconcile_daily_track_nodes can recover which hourly bar actually detected the
            # signal (found by Opus review, 2026-08-05: without it, every trailing-buy fill's
            # bar-match against a backtest replay was comparing a bar index against a wall-clock
            # timestamp and could never match).
            db.open_position(node, pb['signal_price'], fill_time, price, fill_time,
                              shares=shares, paper=True, signal_bar_time=pb['signal_time'])
            db.clear_paper_pending_buy(node['id'])
            db.log_coverage_event("entry_fill", "paper", ticker=ticker, node_id=node.get('id'), result="trailing_bounce_filled",
                                   detail=f"shares={shares} price={price:.4f}")
            # Re-read live, not the frozen node snapshot from when the pending buy was
            # created -- flipping paper_alert_verbose=1 after a buy signal fired but
            # before it filled should still produce this alert (found by Opus review 2026-07-26).
            live_node = db.get_watch_list_node_by_id(node['id']) if node.get('id') else None
            if live_node and live_node.get('paper_alert_verbose'):
                _post_message(f"🧪 PAPER BUY FILLED — {ticker}  {shares}sh @ ${price:.4f}")
            continue
        if overdue:
            db.clear_paper_pending_buy(wl_id)
            db.log_coverage_event("entry_abandon_timeout", "paper", ticker=ticker, node_id=wl_id,
                                   result="abandoned", detail=f"max={node.get('max_hold_hours')}")
            # Re-read live, same rationale as the fill-alert path above --
            # node here is the frozen signal-time snapshot, which never
            # carries paper_alert_verbose at all (not in
            # _PENDING_BUY_NODE_KEYS), so node.get('paper_alert_verbose') was
            # always None/falsy in a first version of this fix (found by
            # review before landing).
            live_node = db.get_watch_list_node_by_id(wl_id)
            if live_node and live_node.get('paper_alert_verbose'):
                _post_message(f"🧪⏱️ {ticker} — paper trailing buy never bounced within "
                              f"{node.get('max_hold_hours')}h — entry abandoned.")
            continue
        if running_low != pb['running_low']:
            db.update_paper_pending_buy_running_low(pb['id'], running_low)


def _backtest_replay_for_node(node):
    """Full RAW trade list (unfiltered -- includes a currently-still-open trade,
    result==OPEN) plus the prepped kernel arrays, so a specific bar's Open/Close/
    lower_band can be recomputed for the counterfactual re-check below. Node-scoped
    (builds kernel args directly from the real watch_list row already in hand)
    rather than scripts.drought_detection_test.load_nodes, which collapses to one
    arbitrary node per ticker via MIN(id)+GROUP BY -- wrong now that a ticker can
    have both a live-track and daily-track node (and, for GDXU, a 3rd pilot node)."""
    import numpy as np
    from backtester import prep_inputs
    from scripts.export_trades import load_hourly, simulate_trail_both_annotated, simulate_trail_exit_chaos
    from scripts.drought_detection_test import build_indicators
    from scripts.drought_overlay_test import TARGET_H0, TARGET_H1

    df_h = load_hourly(node['ticker'])
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    ind = build_indicators(node['strategy'], df_daily, node['window'])
    p = prep_inputs(df_h, ind)
    open_check = node.get('entry_timing') == 'open_check'
    z = node['z_score_threshold']
    if node['strategy'] == 'TrailingBothZScoreBreakout':
        trades = simulate_trail_both_annotated(
            p, node['arm_sell_pct'] / 100.0, node['fixed_sl'] / 100.0, node['max_hold_hours'],
            node['trail_buy_pct'] / 100.0, node['trail_sell_pct'] / 100.0,
            TARGET_H0, TARGET_H1, z, open_check=open_check)
    elif node['strategy'] == 'TrailingExitZScoreBreakout':
        rng = np.random.default_rng(0)
        arm_pct = node['take_profit']
        trades = simulate_trail_exit_chaos(
            p, arm_pct / 100.0, node['fixed_sl'] / 100.0, node['max_hold_hours'],
            node['trail_sell_pct'] / 100.0, TARGET_H0, TARGET_H1, z, rng, "drop", 0.0, "drop", 0.0,
            open_check=open_check)
    else:
        raise ValueError(f"reconcile_daily_track_nodes: unhandled strategy {node['strategy']}")
    return trades, df_h, p


def _bar_index_for(df_h, ts):
    """Exact bar lookup -- returns None (not a crash) when ts doesn't land exactly
    on a cached bar. Used for ENTRY signal bars specifically: a trailing-buy's real
    signal bar can be many bars before its eventual bounce-fill (a separate wait
    phase, not a simple 'which bar contains this wall-clock time' question), so it's
    captured explicitly at write-time (signal_bar_time) rather than derived here."""
    import pandas as pd
    if ts is None:
        return None
    try:
        return df_h.index.get_loc(pd.Timestamp(ts))
    except KeyError:
        return None


def _bar_containing(df_h, ts):
    """Which cached bar's window contains wall-clock ts -- the last bar timestamped
    at or before ts. Used for EXIT bar resolution: unlike an entry signal (which can
    lag its trigger by many bars during a trailing-buy wait phase), an exit reaction
    -- whether a genuine bar-close check or a mid-bar live-tick reactive one, both
    real for daily-track by design (2026-08-05, user's explicit call: exits on paper
    trading stay real; reconcile is what figures out whether a divergence is a
    time/price resolution issue) -- always falls within the SAME bar its trigger
    price action actually occurred in, so a most-recent-bar-at-or-before lookup is
    the correct match, not an exact-timestamp one."""
    import pandas as pd
    if ts is None:
        return None
    pos = df_h.index.searchsorted(pd.Timestamp(ts), side='right') - 1
    return int(pos) if pos >= 0 else None


def _close_alone_would_breach_sl(node, p, exit_i, entry_price):
    """Wick-exit counterfactual: at exit_i, would the bar's Close ALONE (not the
    intrabar Low the backtest's SL check uses) have also been below the fixed-SL
    trigger? If yes, the bar genuinely closed in breach -- not just a wick that
    recovered -- and daily-track's own live-tick polling would very likely have
    caught it too (not explained). If no, the breach was wick-only (price dipped
    below the stop then recovered by bar close) -- exactly a time/price
    resolution artifact daily-track's polling cadence can legitimately miss,
    explained. Only for a genuine (unarmed) SL exit, a fixed threshold known
    without peak-tracking state -- see _close_alone_would_breach_trail for the
    trailing-stop counterpart."""
    fixed_sl = node.get('fixed_sl')
    if fixed_sl is None:
        return None
    stop_price = entry_price * (1.0 - fixed_sl / 100.0)
    return bool(p['prices'][exit_i] <= stop_price)


def _close_alone_would_breach_trail(node, p, arm_i, exit_i):
    """Trailing-stop wick-exit counterfactual -- the multi-bar counterpart to
    _close_alone_would_breach_sl. The trailing stop's threshold moves with a
    running peak, which isn't captured in the trade dict, so it's reconstructed
    here the same way the kernel tracks it (backtester.py /
    scripts/export_trades.py's simulate_trail_both_annotated): peak starts at
    the arm bar's Close (the price at the moment take-profit armed it), then
    each subsequent bar updates peak = max(peak, that bar's High) before
    checking Low against the new trailing-stop level. This reconstructs that
    same process using ONLY Close (no High/Low) from arm_i to exit_i -- if it
    would ALSO trigger an exit at or before exit_i, daily-track's live-tick
    polling should have caught it too (not explained); if not, the real exit
    came specifically from an intrabar High/Low excursion invisible to Close
    (wick-only, explained). Returns None if trail_sell_pct is missing, or if
    the exit was TIME-forced while armed rather than a genuine price breach
    (held >= max_hold_hours -- a bar-count question, not a price one; callers
    should check that first, since this counterfactual doesn't apply to it)."""
    trail_pct = node.get('trail_sell_pct')
    if trail_pct is None or arm_i is None:
        return None
    trail_pct = trail_pct / 100.0
    peak = p['prices'][arm_i]
    for i in range(arm_i + 1, exit_i + 1):
        cp = p['prices'][i]
        if cp > peak:
            peak = cp
        if cp <= peak * (1.0 - trail_pct):
            return True
    return False


def reconcile_daily_track_nodes():
    """Nightly reconcile for every paper_role='daily_sync' node (docs/design.md's
    "Two-account paper trading" section) -- PURE OBSERVATION, no state mutation.

    Corrected 2026-08-05 (twice): first from a blind nightly force-close (broke
    multi-day holds), then to a reconcile-AND-resync engine (auto-corrected
    daily-track's paper state whenever a divergence was proven explained by
    price). The user reconsidered that second design mid-session: resyncing
    erases exactly the natural divergence that's most interesting to study over
    time, and a "halt on unexplained" policy would quiet nodes down whenever
    something interesting happened -- the opposite of what a long-running
    comparison is for. Final call: "reconcile" is one action (how far are we
    diverged, and can it be explained) -- a "sync" (force daily-track back in
    line, prepare for next day) is a conceptually separate action, not
    automatically invoked here. daily-track just keeps trading on its own real
    signals, forever; this function only classifies and logs, every night, in
    full (db.log_daily_track_reconciliation) -- "logs would be terrible to
    query" (user, 2026-08-05) is why this is a DB table, not print statements.
    At most one log row per (wl_id, check_date) -- a restart after 16:05
    re-running this function is a no-op for a node already logged today.

    Entries: daily-track's signal check already prices off the closed hourly bar
    (compute_buy_signal, paper_role='daily_sync' branch). Exits: daily-track's
    exit checks stay fully real/reactive (check_paper_sells, untouched --
    "exits on paper trading should be real, and reconcile should figure out
    that it was a time price issue," user's words). A mid-bar reactive exit
    still lands inside some real hourly bar's window, resolved after the fact
    once that bar is cached.

    Comparison is anchored on the BACKTEST's most recent trade (`bt_ref` --
    open or already closed, whichever `trades[-1]` is), not on daily-track's
    own most recent activity -- anchoring on daily-track's own history let a
    stale old trade permanently mask detection of a genuinely new miss once
    any trade history existed (found by the contextual Opus review). Given
    `bt_ref`'s signal bar, look for daily-track's OWN record of that SAME
    trade (its open position, if the open position's signal bar matches; else
    a closed trade in its history at that same signal bar) -- 'flat' means no
    record of THIS trade specifically, not "nothing recent."

    A still-resting paper pending buy (bounce-fill wait phase) is a fourth
    state, not flat/open/closed -- logged separately (action='pending_skip')
    rather than judged against that dichotomy.

    State combinations (bt_ref's state x daily-track's record of that trade)
    and the 'explained_by_price' classification for each, all purely
    informational -- nothing here changes daily-track's real state:
    - flat (no record), bt_ref open or closed: would Close ALONE have fired
      the signal at bt_ref's signal bar (open_check nodes check Open first,
      Close fallback -- see backtester.py / scripts/export_trades.py's
      simulate_trail_both_annotated)? If not, explained -- the backtest fired
      via the Open leg specifically, exactly the isolated variable daily-track
      exists to test. If Close would have fired too, unexplained -- a real
      gap in daily-track's own signal path. Note (clarified after the
      independent Opus review, 2026-08-05, flagged it as looking like a
      structural artifact): for an open_check node, EVERY Open-leg backtest
      entry will classify 'entry_miss_explained', with no exceptions --
      daily-track's live signal check (compute_buy_signal's daily_sync
      branch) never sees the pinned real-time Open price at all (it ignores
      price_override unconditionally, by design) and can't yet see the day's
      own bar at the pinned 9:31-9:40/14:31-14:40 window either (that bar
      hasn't closed/cached yet, so the staleness guard returns None) -- it
      only ever gets a chance to fire later, off the fully-closed bar's real
      Close. This is the correct, intended consequence of isolating
      price-source timing as the ONE variable under test, not a bug: a
      Close-only node structurally cannot participate in an Open-leg entry,
      ever, by construction. The interesting classification is the reverse
      case -- Close-based misses that come back unexplained, which are real
      gaps in daily-track's own live path, not an artifact of this design.
    - open, bt_ref open, same bar: match.
    - open, bt_ref closed (the backtest's intrabar Low/trailing-peak breached
      a stop daily-track's live-tick polling never sampled): would Close ALONE
      also have triggered the same exit? Fixed-SL exits use
      _close_alone_would_breach_sl (single-bar threshold); trailing-armed
      exits use _close_alone_would_breach_trail (multi-bar peak
      reconstruction) UNLESS the exit was TIME-forced (held >= max_hold_hours,
      armed or not -- a bar-count question, not a price one, always
      unexplained). Not breaching alone -> explained (wick-only). Breaching
      alone too -> unexplained (daily-track should have caught it). Two known,
      accepted classification-accuracy gaps here (contextual Opus review,
      2026-08-05), neither fixed -- both bias toward over-labeling "explained"
      on real gaps, so they're a source of false negatives (missed real
      divergences), not false alarms: (a) an overnight/opening GAP-through-SL
      (the kernel's op<=stop_price leg) reads as wick-only here since Close
      alone won't show it either, even though daily-track's exit path polls
      live and would likely have caught a real gap-down open; (b) a bar where
      BOTH a genuine trail breach and hold-time expiry are simultaneously true
      is classified TIME-forced/unexplained even though it's really a price
      breach too.
    - closed, bt_ref closed, same bar: compare exit bars via _bar_containing
      (last cached bar at-or-before daily-track's real wall-clock exit_time,
      or the exact _bar_index_for match when exit_bar_time was captured --
      see check_paper_sells). Same bar -- match, regardless of the exact
      intrabar price (that's the explained-by-resolution case itself).
      Different bar -- unexplained.
    - closed, bt_ref still open: daily-track exited for real before the
      backtest replay resolved the same trade -- logged (action='exit_early'),
      no further classification (exits aren't price-source-isolated the way
      entries are, so there's no clean counterfactual for "closed too soon").
    - daily-track holding a position matching NEITHER bt_ref's signal bar nor
      any flat/closed determination (action='ambiguous_position'): two
      independent divergences compounding, rare.
    - bt_ref exists but daily-track's own signal bar can't be resolved, or the
      backtest shows nothing at daily-track's signal bar at all
      (action='no_backtest_data')."""
    from backtester import OPEN

    today = datetime.now().strftime('%Y-%m-%d')
    touched = 0
    for node in db.get_watchlist():
        if node.get('paper_role') != 'daily_sync':
            continue
        if any(r['check_date'] == today for r in db.get_daily_track_reconciliation_log(node['id'], limit=5)):
            continue  # already reconciled today (e.g. a restart after 16:05)

        if db.get_paper_pending_buy(node['id']):
            db.log_daily_track_reconciliation(
                wl_id=node['id'], ticker=node['ticker'], check_date=today,
                actual_state='pending', backtest_state='unknown', bar_match=False,
                action='pending_skip', explained_by_price=None,
                detail="daily-track has a resting pending buy (bounce-fill wait phase) -- not "
                       "flat/open, skipping tonight's classification rather than judging a trade "
                       "that hasn't finished forming")
            continue

        try:
            trades, df_h, p = _backtest_replay_for_node(node)
        except Exception as e:
            print(f"  [daily_track] {node['ticker']} wl_id={node['id']} backtest replay failed: {e}")
            # Logged, not just printed -- a permanently-broken node (missing CSV,
            # unhandled strategy) would otherwise be silently absent from the table every
            # night, contradicting the "logs would be terrible to query" reason this table
            # exists (found by the independent Opus review, 2026-08-05).
            db.log_daily_track_reconciliation(
                wl_id=node['id'], ticker=node['ticker'], check_date=today,
                actual_state='unknown', backtest_state='unknown', bar_match=False,
                action='replay_failed', explained_by_price=None, detail=str(e))
            continue
        bt_ref = trades[-1] if trades else None
        backtest_state = 'open' if bt_ref and bt_ref['result'] == OPEN else ('closed' if bt_ref else 'flat')
        target_signal_i = bt_ref['signal_i'] if bt_ref else None

        actual_open = db.get_open_position_by_wl_id(node['id'], paper=True)
        actual_open_signal_i = (
            _bar_index_for(df_h, actual_open.get('signal_bar_time') or actual_open['signal_time'])
            if actual_open else None)

        actual_ref = actual_closed = None
        if actual_open is not None and target_signal_i is not None and actual_open_signal_i == target_signal_i:
            actual_ref, actual_state = actual_open, 'open'
        elif target_signal_i is not None:
            actual_closed = next(
                (t for t in db.get_trade_log_for_wl_id(node['id'], paper=True)
                 if t.get('exit_time')
                 and _bar_index_for(df_h, t.get('signal_bar_time') or t['signal_time']) == target_signal_i),
                None)
            if actual_closed is not None:
                actual_ref, actual_state = actual_closed, 'closed'
            elif actual_open is not None:
                actual_state = 'open_different_trade'
            else:
                actual_state = 'flat'
        elif actual_open is not None:
            actual_state = 'open_different_trade'
        else:
            actual_state = 'flat'

        common = dict(
            wl_id=node['id'], ticker=node['ticker'], check_date=today,
            actual_state=actual_state, backtest_state=backtest_state,
            actual_entry_price=actual_ref['entry_price'] if actual_ref else None,
            actual_entry_time=actual_ref['entry_time'] if actual_ref else None,
            actual_signal_price=actual_ref['signal_price'] if actual_ref else None,
            actual_exit_time=actual_closed['exit_time'] if actual_closed else None,
            backtest_entry_price=bt_ref['entry_p'] if bt_ref else None,
            backtest_entry_time=(df_h.index[bt_ref['entry_i']].strftime('%Y-%m-%d %H:%M:%S')
                                  if bt_ref else None),
            backtest_signal_z=bt_ref['signal_z'] if bt_ref else None,
            backtest_exit_time=(df_h.index[bt_ref['exit_i']].strftime('%Y-%m-%d %H:%M:%S')
                                 if bt_ref and backtest_state == 'closed' else None),
        )

        if actual_state == 'open_different_trade':
            touched += 1
            db.log_daily_track_reconciliation(
                action='ambiguous_position', explained_by_price=False, bar_match=False,
                detail="daily-track is holding a position that matches neither the backtest's "
                       "current reference trade nor any resolvable flat/closed state -- unexplained",
                **common)
            continue

        if actual_state == 'flat':
            bar_match = bt_ref is None
            common['bar_match'] = bar_match
            if bar_match:
                db.log_daily_track_reconciliation(action='match', explained_by_price=None, **common)
                continue
            touched += 1
            di = p['daily_idx'][target_signal_i]
            lower_band_val = None
            if di >= 0 and p['std_arr'][di] != 0.0:
                lower_band_val = p['sma_arr'][di] - p['std_arr'][di] * node['z_score_threshold']
                close_would_fire = p['prices'][target_signal_i] <= lower_band_val
                explained = not close_would_fire
                action = 'entry_miss_explained' if explained else 'entry_miss_unexplained'
                detail = ("backtest signal fired via bar Open, not Close -- explained" if explained else
                          "backtest signal would have fired on Close alone too -- daily-track should "
                          "have caught it, unexplained gap")
            else:
                # No prior-day indicator history (di<0) or a zero-Std day -- the counterfactual
                # itself can't be computed, distinct from "computed it and it wasn't explained"
                # (found by the contextual Opus review, 2026-08-05 -- the prior version defaulted
                # this to explained_by_price=False/'entry_miss_unexplained', misrepresenting
                # "unknown" as "checked and found unexplained").
                explained = None
                action = 'no_backtest_data'
                detail = "can't compute the entry counterfactual (no prior-day indicator history or a zero-Std day)"
            db.log_daily_track_reconciliation(
                action=action, explained_by_price=explained, backtest_lower_band=lower_band_val,
                detail=detail, **common)
            continue

        if actual_state == 'open':
            if backtest_state == 'open':
                db.log_daily_track_reconciliation(action='match', explained_by_price=None, bar_match=True, **common)
                continue
            # backtest's intrabar Low/trailing-peak breached a stop daily-track's polling
            # never sampled -- can we prove it's wick-only?
            touched += 1
            arm_i = bt_ref.get('arm_i')
            max_hold = node.get('max_hold_hours')
            # TIME-forced is a bar-count question, not a price one -- checked regardless of
            # arm_i, since the kernel produces a genuine TIME exit (TWIN/TLOSS) for a
            # never-armed trade too, not just the armed held>=max_hold_hours branch. An
            # earlier version required arm_i is not None here, which routed an unarmed
            # TIME exit into the SL wick counterfactual instead -- Close is nowhere near the
            # entry-based SL threshold for a trade that never got close to breaching it, so
            # it always resolved "explained" with an actively false "SL breach was wick-only"
            # detail, silently hiding a real bar-count divergence (found by the contextual
            # Opus review, 2026-08-05, reproduced live).
            time_forced = (max_hold is not None
                           and bt_ref.get('held') is not None and bt_ref['held'] >= max_hold)
            breach = None
            if time_forced:
                reason_none = "backtest exit was TIME-forced -- a bar-count question, not a price one"
            elif arm_i is None:
                breach = _close_alone_would_breach_sl(node, p, bt_ref['exit_i'], bt_ref['entry_p'])
                reason_none = None
            else:
                breach = _close_alone_would_breach_trail(node, p, arm_i, bt_ref['exit_i'])
                reason_none = None
            # breach may be a numpy.bool_ (array comparison) -- `is False` is an identity
            # check that silently never matches numpy.bool_(False) (found live while testing
            # this exact branch, 2026-08-05); compare by value instead.
            explained = breach is not None and not breach
            if explained:
                detail = ("backtest's SL breach was wick-only (Close alone would not have "
                           "triggered it) -- daily-track's live-tick polling couldn't have caught "
                           "it either") if arm_i is None else (
                          "backtest's trailing-stop breach was wick-only (a Close-only peak "
                          "reconstruction would not have triggered it) -- daily-track's live-tick "
                          "polling couldn't have caught it either")
                action = 'exit_wick_explained'
            else:
                detail = reason_none or (
                    "the bar's Close alone would also have breached the trigger -- not just a "
                    "wick, daily-track should have caught it")
                action = 'exit_wick_unexplained'
            db.log_daily_track_reconciliation(
                action=action, explained_by_price=explained, bar_match=False,
                detail=f"backtest already closed this trade but daily-track is still holding it -- "
                       f"{detail}", **common)
            continue

        # actual_state == 'closed'
        if backtest_state == 'open':
            touched += 1
            db.log_daily_track_reconciliation(
                action='exit_early', explained_by_price=None, bar_match=False,
                detail="daily-track already closed this trade but the backtest replay still shows "
                       "it open -- exits aren't price-source-isolated, no counterfactual to run",
                **common)
            continue

        # exit_bar_time (captured explicitly for a bar-close exit -- see check_paper_sells)
        # is an exact bar match; wall-clock exit_time alone (the mid-bar reactive case) needs
        # the at-or-before lookup instead, since it genuinely falls inside the bar the
        # trigger action occurred in.
        if actual_closed.get('exit_bar_time'):
            actual_exit_i = _bar_index_for(df_h, actual_closed['exit_bar_time'])
        else:
            actual_exit_i = _bar_containing(df_h, actual_closed['exit_time'])
        exit_bar_match = actual_exit_i is not None and actual_exit_i == bt_ref['exit_i']
        if exit_bar_match:
            db.log_daily_track_reconciliation(action='match', explained_by_price=None, bar_match=True, **common)
            continue
        touched += 1
        db.log_daily_track_reconciliation(
            action='exit_bar_mismatch', explained_by_price=False, bar_match=False,
            detail=f"both sides closed this trade, but on different bars (daily-track exit "
                   f"resolved to bar {actual_exit_i}, backtest exit_i={bt_ref['exit_i']}) -- "
                   f"unexplained exit-side divergence", **common)
    return touched


def check_paper_sells(last_seen_bar, paper_sell_alerted, load_cache):
    """Mirrors the real open_positions exit-check block in active_signals.run_loop,
    sharing last_seen_bar with the real block (safe -- a ticker is never
    simultaneously live and research) but using its own dedup set since paper
    position ids are independent of real open_positions ids."""
    for pos in db.get_open_positions(paper=True):
        ticker = pos['ticker']
        node = db.get_watch_list_node_by_id(pos['wl_id']) if pos.get('wl_id') else None
        verbose = bool(node and node.get('paper_alert_verbose'))
        df_hourly, _ = load_cache(ticker)
        if df_hourly is None or df_hourly.empty:
            continue
        last_bar_ts = df_hourly.index[-1]
        if (pos['id'], last_bar_ts) in paper_sell_alerted:
            continue
        at_bar_close = resolve_at_bar_close(pos, last_bar_ts, last_seen_bar)
        if at_bar_close:
            bar = df_hourly.iloc[-1]
            cp, low, high, op = float(bar['Close']), float(bar['Low']), float(bar['High']), float(bar['Open'])
        else:
            # daily-track exits stay real/reactive here too (2026-08-05 -- user's explicit
            # call: exits on paper trading should be real, and reconcile is what figures out
            # whether a divergence is a time/price resolution issue, not the exit check
            # itself). A mid-bar exit still lands inside some real hourly bar's window --
            # reconcile_daily_track_nodes resolves it after the fact (once that bar is
            # cached) and compares against the backtest's own exit bar, same "explained by
            # price" framing as the entry-side Open-vs-Close case.
            cp, _ = _current_price(ticker)
            if cp is None:
                continue
            low = high = op = cp
        log_poll(f"{ticker} paper_check_sells bar={last_bar_ts} at_bar_close={at_bar_close} "
                 f"cp={cp:.4f} low={low:.4f} high={high:.4f} op={op:.4f}")
        reason, target, just_activated_trailing = check_sell_condition(
            pos, cp, datetime.now(), at_bar_close=at_bar_close, low=low, high=high, open_price=op,
            df_hourly=df_hourly, paper=True)
        if just_activated_trailing and verbose:
            _post_message(f"🧪 PAPER trailing-sell armed — {ticker}")
        if reason:
            # exit_bar_time: the real graded bar's timestamp, captured explicitly here for
            # the at_bar_close branch -- exit_time itself is wall-clock, and for a bar-close
            # exit specifically that wall-clock moment always falls chronologically inside
            # the NEXT bar's window (bar N ends exactly when bar N+1 begins, and this poll
            # runs shortly after), so reconcile_daily_track_nodes' _bar_containing(exit_time)
            # lookup would misattribute the exit to the wrong bar without this (found by Opus
            # review, 2026-08-05). NULL on the mid-bar branch -- there, exit_time's wall clock
            # genuinely does fall inside the bar the trigger action occurred in, and
            # _bar_containing on it is the correct lookup.
            db.close_position(pos['id'], exit_signal_price=cp, exit_price=target,
                               exit_time=datetime.now(), exit_reason=reason, paper=True,
                               exit_bar_time=last_bar_ts if at_bar_close else None)
            db.log_coverage_event("exit_fill", "paper", ticker=ticker, position_id=pos['id'],
                                   node_id=pos.get('wl_id'), result=reason, detail=f"price={target:.4f}")
            if verbose:
                _post_message(f"🧪 PAPER SELL — {ticker}  {reason} @ ${target:.4f}")
            paper_sell_alerted.add((pos['id'], last_bar_ts))
