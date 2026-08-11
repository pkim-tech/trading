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
import schwab_safety
from signals_compute import _current_price, _load_cache, _bars_held, check_sell_condition, compute_buy_signal
from signals_blocks import _post_message
from signals_helpers import buy_order_sizing, effectively_dry_run, log_poll, resolve_at_bar_close

# Same checkpoint-bar hours the backtest's find_drought_windows uses
# (scripts/drought_overlay_test.py's TARGET_H0/TARGET_H1) -- the 9:30 and
# 14:30 hourly bars, matching the two real daily signal-check windows
# (10:25-10:40 and 15:25-15:40 ET). Duplicated here (not imported) to avoid
# pulling that research script's heavier transitive imports (backtester,
# numba) into the live daemon's module graph for two constants.
_DROUGHT_TARGET_H0, _DROUGHT_TARGET_H1 = 9, 14


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


def _last_core_exit_time(wl_id, paper=True):
    """Most recent CLOSED core (position_source='core') trade_log exit for
    this node -- the drought overlay's "gap start", mirroring
    find_drought_windows' own gap_start = exit_bars[signal_bars[k]]. Returns
    None if this node has never closed a core trade yet (matching the
    backtest: find_drought_windows only ever creates a window BETWEEN two
    consecutive real trades, never before the first one). paper=False reads
    the real trade_log instead of paper_trade_log (Part 4.1, real drought
    entry shares this exact eligibility decision, not a reimplementation)."""
    table = 'paper_trade_log' if paper else 'trade_log'
    with db._conn() as c:
        row = c.execute(
            f"SELECT exit_time FROM {table} WHERE wl_id=? AND position_source='core' "
            f"AND exit_time IS NOT NULL ORDER BY exit_time DESC LIMIT 1",
            (wl_id,)
        ).fetchone()
    if row is None:
        return None
    return datetime.strptime(row['exit_time'], '%Y-%m-%d %H:%M:%S')


def _drought_trade_exists_for_gap(wl_id, gap_start_str, paper=True):
    """True if a drought_overlay trade already exists (open or closed) for
    this exact gap -- identified by drought_gap_start, which is constant for
    every entry attempt within the same gap (it only changes once a NEW core
    trade closes). Checks BOTH the positions table (still open) and the
    trade_log table (already closed) since a gap's one allowed trade could be
    in either state. paper=False reads the real open_positions/trade_log
    tables."""
    positions_table = 'paper_positions' if paper else 'open_positions'
    trade_log_table = 'paper_trade_log' if paper else 'trade_log'
    with db._conn() as c:
        row = c.execute(
            f"SELECT 1 FROM {positions_table} WHERE wl_id=? AND position_source='drought_overlay' "
            f"AND drought_gap_start=? LIMIT 1",
            (wl_id, gap_start_str)
        ).fetchone()
        if row is None:
            row = c.execute(
                f"SELECT 1 FROM {trade_log_table} WHERE wl_id=? AND position_source='drought_overlay' "
                f"AND drought_gap_start=? LIMIT 1",
                (wl_id, gap_start_str)
            ).fetchone()
    return row is not None


_IVOL_SERIES_CACHE = {}  # ticker -> (csv_mtime, ivol_dataframe)


def _cached_ivol_series(ticker):
    """Memoized wrapper around drought_overlay_sweep.get_ivol_series --
    that function re-reads and recomputes the ticker's ENTIRE hourly CSV
    from scratch on every call, with no caching of its own. Fine for a
    one-shot research script; a real cost if called every poll for every
    vol-gated node in the live daemon (found by an independent review of
    the wiring diff, 2026-08-09). Keyed on the CSV's own mtime, so a real
    intraday cache refresh still invalidates it -- never serves stale data."""
    import os
    path = f"cache/research/{ticker}_1h.csv"
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    cached = _IVOL_SERIES_CACHE.get(ticker)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    from scripts.drought_overlay_sweep import get_ivol_series
    series = get_ivol_series(ticker)
    _IVOL_SERIES_CACHE[ticker] = (mtime, series)
    return series


def evaluate_drought_entry(node, paper=True):
    """Pure eligibility decision for a drought-overlay entry -- the live
    equivalent of drought_overlay_test.find_drought_windows, which defines a
    "drought window" as confirm_days*2 checkpoint bars (9:30/14:30 hourly
    closes, the same TARGET_H0/TARGET_H1 pair) elapsing after a real trade's
    exit before the next real trade's signal. Live has no "next signal" to
    look ahead to -- that's exactly what check_paper_drought_handoff/
    signals_notify.check_drought_handoff detect as it happens, not a
    backstop this function can precompute.

    Shared by check_paper_drought_entry (paper) and
    signals_notify.check_drought_entry (real) -- both tracks run the SAME
    eligibility state machine, not two independently-maintained copies
    (docs/plans/real_order_execution_drought_addon.md 4.1). Deliberately does
    NOT check node.get('mode')/daily_sync_halted_at -- those are the
    CALLER's responsibility (paper vs. real dispatch happens one level up),
    so a real-mode caller isn't short-circuited by a paper-only guard.

    Returns {'price','shares','confirm_days','vol_gate','vol_pctile',
    'gap_start'} or None if not eligible. `shares` here is the naive
    starting_notional // price sizing (matches D4: generate_drought_trades is
    sizing-agnostic, so this is a legitimate real default too) -- real order
    placement re-sizes via buy_order_sizing's own worst-case-pad formula
    before actually placing an order, this is not the final real share count."""
    if not node.get('drought_overlay_enabled') or not node.get('drought_confirm_days'):
        return None
    wl_id, ticker = node['id'], node['ticker']
    pending = db.get_paper_pending_buy(wl_id) if paper else db.get_pending_buy_by_wl_id(wl_id)
    if db.get_open_position_by_wl_id(wl_id, paper=paper) or pending:
        return None
    last_exit = _last_core_exit_time(wl_id, paper=paper)
    if last_exit is None:
        return None
    gap_start_str = last_exit.strftime('%Y-%m-%d %H:%M:%S')
    if _drought_trade_exists_for_gap(wl_id, gap_start_str, paper=paper):
        # Fire-once-per-gap -- the backtest's find_drought_windows makes
        # exactly ONE trade per gap between consecutive core signals, even if
        # that trade stops out early (there's real time left before the next
        # core signal). Without this, the first version re-entered every poll
        # for the rest of the gap once confirmed, since `last_exit`/`eligible`
        # never move again until a NEW core trade closes (found by paired
        # Opus review, 2026-08-09).
        return None
    df_h, _ = _load_cache(ticker)
    if df_h is None or df_h.empty:
        return None
    hours = df_h.index.hour
    checkpoint_mask = (hours == _DROUGHT_TARGET_H0) | (hours == _DROUGHT_TARGET_H1)
    eligible = df_h.index[checkpoint_mask & (df_h.index > last_exit)]
    confirm_days = node['drought_confirm_days']
    if len(eligible) < confirm_days * 2:
        return None
    vol_gate = node.get('drought_vol_gate')
    vol_pctile = None
    if vol_gate is not None:
        # Local import -- see _DROUGHT_TARGET_H0's comment above, same reason
        # (avoid loading drought_overlay_sweep's heavier research-script
        # imports at daemon startup for a function only needed when this one
        # node actually has a vol gate configured).
        from scripts.drought_overlay_sweep import _entry_vol_pctile
        ivol_series = _cached_ivol_series(ticker)
        vol_pctile = _entry_vol_pctile(df_h.index[-1], ivol_series)
        # A None reading (too early in history for a full lookback window)
        # EXCLUDES the window, mirroring generate_drought_trades'
        # _apply_vol_gate exactly ("gated.append if pctile is not None and
        # pctile < vol_gate" -- unknown is never eligible). The first version
        # of this check had the polarity backwards: `vol_pctile is not None
        # and vol_pctile >= gate: return` let an unknown reading THROUGH
        # instead of excluding it (found by cold Opus review, 2026-08-09).
        if vol_pctile is None or vol_pctile >= vol_gate:
            log_poll(f"{ticker} drought_entry gated: vol_pctile={vol_pctile} gate={vol_gate}")
            return None
    # daily-track (paper_role='daily_sync') nodes price off the last closed
    # hourly bar's Close, exactly like compute_buy_signal's own daily_sync
    # branch and start_paper_buy/start_paper_market_buy's existing halt-check
    # convention -- without this, a daily-track drought node would silently
    # price off a live tick, the exact price-source confound the daily-track
    # split exists to isolate (found by both review passes, 2026-08-09).
    if node.get('paper_role') == 'daily_sync':
        price = float(df_h['Close'].iloc[-1])
    else:
        price, _ = _current_price(ticker)
    if price is None:
        return None
    starting_notional = node.get('starting_notional') or 50000
    shares = int(starting_notional // price)
    if shares < 1:
        return None
    return {'price': price, 'shares': shares, 'confirm_days': confirm_days, 'vol_gate': vol_gate,
            'vol_pctile': vol_pctile, 'gap_start': gap_start_str}


def check_paper_drought_entry(node):
    """Confirms and enters a drought-overlay position for a
    drought_overlay_enabled node whose core paper position has been flat for
    at least drought_confirm_days. Thin caller over evaluate_drought_entry
    (Part 4.1) -- this function owns only the paper-specific gating (mode,
    daily_sync_halted_at) and the actual paper_positions write.

    MUST be called AFTER this node's own core buy-signal scan in the same
    poll cycle (active_signals._scan_buy_signals or equivalent) -- if core's
    real signal fires this same poll, its own pending-buy/position gets
    created first, and evaluate_drought_entry's own pending/position check
    correctly sees that and returns None, so core always wins ties rather
    than this function racing it. Wired into active_signals.py,
    2026-08-09, after a paired review of that wiring (docs/CLAUDE.md's
    session-wrap mandate) found and fixed the ordering/gating issues noted
    inline in run_loop where this is actually called."""
    if node.get('state') != 'paper':
        # Paper-only mechanism -- a live node sharing open_position_keys with
        # its own paper drought row would silently suppress its REAL BUY
        # alerts (already_held in _scan_buy_signals unions real+paper by
        # wl_id) without ever placing a real order. Found by a paired review
        # of the wiring diff, 2026-08-09.
        return
    if node.get('daily_sync_halted_at'):
        return
    decision = evaluate_drought_entry(node, paper=True)
    if decision is None:
        return
    wl_id, ticker = node['id'], node['ticker']
    now = datetime.now()
    db.open_drought_overlay_position(node, decision['price'], now, decision['price'], now,
                                      confirm_days=decision['confirm_days'], vol_gate=decision['vol_gate'],
                                      gap_start=decision['gap_start'], vol_pctile=decision['vol_pctile'],
                                      shares=decision['shares'], paper=True)
    db.log_coverage_event("drought_entry", "paper", ticker=ticker, node_id=wl_id, result="filled",
                           detail=f"confirm_days={decision['confirm_days']} vol_pctile={decision['vol_pctile']} "
                                  f"shares={decision['shares']}")
    if node.get('paper_alert_verbose'):
        _post_message(f"🧪🌵 PAPER DROUGHT ENTRY — {ticker}  {decision['shares']}sh @ ${decision['price']:.4f} "
                       f"(confirm_days={decision['confirm_days']})")


def check_paper_drought_handoff(node):
    """Closes an open drought-overlay position the moment this node's own
    core signal fires again -- the design's HANDOFF transition (docs/
    design.md's 2026-08-07 state-machine section). Deliberately does NOT
    open the core position itself -- it only clears the drought row so this
    same poll's ordinary core buy-scan (start_paper_buy/start_paper_market_buy)
    sees a flat position and enters normally right after.

    MUST be called BEFORE every real core-entry path in the same poll cycle
    -- not just the two _scan_buy_signals calls, but also _scan_pinned_entry
    (the PRIMARY real entry path for every current drought candidate, all
    entry_timing='open_check' + automation-enabled -- a paired review of the
    wiring found this ordering violated at that third call site and it's now
    fixed at all three in active_signals.py's run_loop). Opposite ordering
    from check_paper_drought_entry above, and the resolution to the exact
    race docs/design.md's truth-table section names ("a core position's real
    entry signal firing on the exact same poll cycle a drought-overlay
    HANDOFF check would also fire"): HANDOFF closes drought first, then
    core's scan (running later in the same poll) opens fresh -- no cycle is
    lost to the race either way. Wired into active_signals.py, 2026-08-09."""
    wl_id = node['id']
    if node.get('state') != 'paper':
        # See check_paper_drought_entry's identical guard -- defense in
        # depth here too, though a live node should never have an open
        # drought position in the first place if the entry-side guard held.
        return
    pos = db.get_drought_overlay_position(wl_id, paper=True)
    if pos is None:
        return
    sig = compute_buy_signal(node)
    # CRITICAL fix (paired review, 2026-08-09, caught the moment this
    # function was actually wired into the daemon): compute_buy_signal
    # returns a real dict on almost EVERY poll (signal='HOLD' most of the
    # time, 'BUY' only when the z-score condition actually fires) -- it's
    # None only on a genuine data/discontinuity failure, never as "no
    # signal." The first version's `if sig is None: return` therefore
    # treated the ordinary HOLD case as a fired core signal and closed the
    # drought position on virtually every poll inside a signal window,
    # defeating the whole mechanism (combined with the once-per-gap guard,
    # this would have permanently starved every drought gap of a second
    # attempt). Every other real consumer of this function (_scan_buy_signals,
    # active_signals.py) checks sig['signal'] == 'BUY' specifically --
    # this one now does too.
    if sig is None or sig['signal'] != 'BUY':
        return
    now = datetime.now()
    price = sig['current_price']
    db.close_position(pos['id'], exit_signal_price=price, exit_price=price, exit_time=now,
                       exit_reason='HANDOFF', paper=True)
    db.log_coverage_event("drought_handoff", "paper", ticker=node['ticker'], node_id=wl_id,
                           result="closed", detail=f"price={price:.4f}")
    if node.get('paper_alert_verbose'):
        _post_message(f"🧪🔁 PAPER DROUGHT HANDOFF — {node['ticker']}  closed @ ${price:.4f}, "
                       f"core signal active again")


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


def _daily_track_comparison_terminal(actual_state, backtest_state, grace_ok=True):
    """Whether a specific backtest-trade comparison (bt_ref) has reached a
    conclusive verdict, so reconcile_daily_track_nodes' bookmark should
    advance past it rather than re-checking the same trade again. Added
    2026-08-10 alongside the bookmark fix; revised same day after a paired
    Opus review (independent-cold + contextual, with a rebuttal exchange)
    found the first version wrong on two of its four cases -- see
    reconcile_daily_track_nodes' docstring for the full false-positive bug
    this closes and the review history behind this specific boundary.

    Terminal: 'flat' AND grace_ok (a miss's fate is permanent -- daily-track
    can never retroactively enter a bar that's already in the past -- but
    ONLY once daily-track has genuinely had its full chance to enter: the
    grace gate, checked by the caller, requires the trade's signal bar to
    predate the current session, not just require any calendar day to have
    passed on the bookmark itself); 'closed' AND backtest_state=='closed'
    (both sides genuinely done).

    NOT terminal: actual_state=='open' (still in progress on daily-track's
    side); actual_state=='flat' with grace_ok=False (too soon to conclude a
    miss -- daily-track may still fire later this same session, off the
    fully-closed bar); actual_state=='closed' with backtest_state=='open'
    (exit_early -- daily-track's own side is done, but since only the
    backtest's LATEST trade can be OPEN, waiting turns this into the real
    closed/closed exit-bar comparison within a bounded number of nights
    (max_hold_hours) instead of permanently discarding it -- found by the
    contextual review to be the one case with zero analysis performed yet,
    corrected from the first version which wrongly treated 'closed'
    unconditionally as terminal regardless of the backtest side);
    actual_state=='open_different_trade' (a genuine unexplained ambiguity --
    deliberately left pinned so it keeps surfacing rather than silently
    advancing past an unresolved real gap)."""
    if actual_state == 'flat':
        return grace_ok
    if actual_state == 'closed':
        return backtest_state == 'closed'
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
    At most one log-writing PASS per (wl_id, check_date) -- a restart after
    16:05 re-running this function is a no-op for a node already reconciled
    today (see the bookmark-based idempotency check below, not a same-day
    per-row check -- a single pass can legitimately write multiple rows, see
    "Bookmark and catch-up" below).

    Entries: daily-track's signal check already prices off the closed hourly bar
    (compute_buy_signal, paper_role='daily_sync' branch). Exits: daily-track's
    exit checks stay fully real/reactive (check_paper_sells, untouched --
    "exits on paper trading should be real, and reconcile should figure out
    that it was a time price issue," user's words). A mid-bar reactive exit
    still lands inside some real hourly bar's window, resolved after the fact
    once that bar is cached.

    Bookmark and catch-up (added 2026-08-10, revised same day after a paired
    Opus review + rebuttal exchange): comparison targets the EARLIEST backtest
    trade after the node's `daily_track_bookmark_signal_bar` (a persisted
    signal-bar timestamp), NOT always the single latest trade -- the original
    version of this function always used `trades[-1]`, which is wrong whenever
    daily-track is still legitimately mid-trade on an EARLIER backtest trade
    (its own real hold duration differs from the backtest's theoretical one
    for that same entry -- exactly the timing divergence this tool exists to
    detect). That bug misclassified every such night as 'ambiguous_position',
    a false positive, not a real divergence.

    The bookmark only advances past a trade once its comparison reaches a
    TERMINAL verdict (see _daily_track_comparison_terminal) -- never a
    position-state mutation, purely tracks which comparison is in progress.
    Each call processes ONE NODE'S trades in a loop, catching up through
    every already-resolved trade in a single pass (not one trade per
    calendar day) -- an earlier version paced catch-up at one trade per day
    via an incidental interaction with the same-day idempotency guard below,
    which meant a node with e.g. 130 unreviewed historical trades (measured
    real case: a node whose backtest replay reaches back to the ticker's
    2023 cached history) would take ~130 calendar nights to reach anything
    describing CURRENT state -- found by the contextual review, confirmed by
    the cold review's independent DB measurement. The same-day check (does a
    log row for today already exist for this node) is still evaluated ONCE,
    before the catch-up loop starts, exactly as before -- so a same-day
    daemon restart still can't double-process a node. What changed is that
    the loop no longer stops after one trade once it's allowed to run at
    all; it keeps advancing through every already-terminal trade within that
    one pass, so a restart mid-catch-up isn't possible (each pass runs to
    completion or to the first non-terminal trade before returning).

    No bookmark yet means -1 (start from the node's very earliest backtest
    trade), not a special-cased "assume trades[-1]" -- an earlier draft tried
    to preserve the old trades[-1] behavior on a node's first run to avoid a
    disruptive cold catch-up, but that's genuinely indistinguishable from
    "brand new node, no history to disrupt" (both just read as bookmark=None)
    and actively reintroduced the same bug it was meant to avoid (caught by a
    test written against the fix itself). Combined with the single-pass
    catch-up above, the real cost of starting from -1 is one slower first
    run (replaying and logging every historical trade once), not a
    months-long drip.

    `_daily_track_comparison_terminal`'s terminal boundary (see its own
    docstring) requires a grace check on 'flat' (a missed entry isn't
    conclusively permanent until the trade's signal bar predates the CURRENT
    session -- daily-track may still fire later that same session, off the
    fully-closed bar, per the Open-leg note below) and treats 'closed' with
    backtest_state=='open' (exit_early) as NOT terminal (waiting a bounded
    number of nights, capped by max_hold_hours, turns a no-analysis row into
    the real closed/closed exit-bar comparison -- this tool's actual
    headline deliverable for exit-side timing divergence). The FIRST version
    of this fix got both of these wrong (terminal-on-any-'flat' reintroduced
    the exact ambiguous_position false positive for a narrow but real
    same-session race; terminal-on-any-'closed' silently discarded the
    highest-value comparison this function produces) -- caught by the
    required paired review + rebuttal exchange, not by the original author.

    Given `bt_ref`'s signal bar, look for daily-track's OWN record of that
    SAME trade (its open position, if the open position's signal bar
    matches; else a closed trade in its history at that same signal bar) --
    'flat' means no record of THIS trade specifically, not "nothing recent."
    When `bt_ref is None` (nothing left after the bookmark), that no longer
    means "the backtest has no trades at all" the way it did before this
    bookmark existed -- see the dedicated branch below, which distinguishes
    "genuinely nothing pending" from "the bookmark already advanced past the
    backtest's own still-open latest trade, which just hasn't resolved yet"
    (the latter used to silently collapse to a false 'match' -- maximum real
    divergence reported as clean -- found by the cold review).

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
    today_date = datetime.now().date()
    touched = 0
    for node in db.get_watchlist():
        # version=='v5' excludes 'v5-overlay-test*' staged combo clones
        # (2026-08-1x) from this real-node reconcile -- found by the cold
        # review's rebuttal pass, 2026-08-10: this function was missing the
        # same version filter its sibling scripts/paper_vs_backtest_reconcile
        # .get_daily_track_wl_ids already carries (there for the identical
        # reason -- without it, staged test clones get swept into the same
        # nightly diagnostic as real v5 daily-track nodes).
        if node.get('paper_role') != 'daily_sync' or node.get('version') != 'v5':
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

        # Catch-up loop -- processes every already-terminal trade for this
        # node in ONE pass (see the function docstring's "Bookmark and
        # catch-up" section), stopping at the first non-terminal trade or
        # once there's nothing left after the bookmark. any_logged tracks
        # whether a real (terminal-trade) row was already written tonight --
        # the "nothing left after the bookmark" outcome only gets its own
        # row when it's the WHOLE story for tonight (steady state, nothing
        # new since last time); appending it after real catch-up rows would
        # just be a redundant "still nothing new" echo of the row already
        # written, exactly the kind of log noise this design exists to avoid.
        any_logged = False
        while True:
            bookmark_str = db.get_daily_track_bookmark(node['id'])
            # _bar_index_for is contractually allowed to return None when a
            # timestamp doesn't land exactly on a cached bar -- comparing
            # that directly against an int with `>` raises TypeError and
            # would kill the whole nightly job for every remaining node
            # (found independently by both reviewers). Falls back to -1
            # (re-scan from the earliest trade) rather than crash; this is a
            # real, if rare, degraded-recovery path, not just belt-and-suspenders,
            # so it's logged rather than silent.
            if bookmark_str:
                resolved = _bar_index_for(df_h, bookmark_str)
                if resolved is None:
                    print(f"  [daily_track] {node['ticker']} wl_id={node['id']} bookmark "
                          f"{bookmark_str!r} not found in current cached data -- re-scanning "
                          f"from the earliest trade")
                    bookmark_i = -1
                else:
                    bookmark_i = resolved
            else:
                bookmark_i = -1
            bt_ref = next((t for t in trades if t['signal_i'] > bookmark_i), None)

            if bt_ref is None:
                # No longer means "the backtest has no trades at all" the way
                # it did before the bookmark existed -- it can also mean "the
                # bookmark already advanced past the backtest's own still-open
                # LATEST trade, which just hasn't resolved yet." Collapsing
                # both to a blanket 'match' would silently report maximum
                # real divergence (backtest holding an open position, daily-
                # track flat or already exited) as clean every night (found
                # by the cold review). Only the genuine "nothing pending at
                # all" case gets 'match'.
                if any_logged:
                    # A real row was already written this pass -- that row
                    # already tells tonight's story; this "nothing further to
                    # process" outcome is implied by it, not new information.
                    break
                still_watching_open = bool(trades) and trades[-1]['result'] == OPEN and trades[-1]['signal_i'] <= bookmark_i
                actual_open = db.get_open_position_by_wl_id(node['id'], paper=True)
                if still_watching_open:
                    touched += 1
                    db.log_daily_track_reconciliation(
                        wl_id=node['id'], ticker=node['ticker'], check_date=today,
                        actual_state='open_different_trade' if actual_open is not None else 'flat',
                        backtest_state='open', bar_match=False,
                        action='steady_state_watching_open_trade', explained_by_price=None,
                        detail="the backtest's most recent trade is still open beyond the bookmark -- "
                               "nothing new to compare yet, not a clean match",
                    )
                elif actual_open is not None:
                    # daily-track is holding a real position with NO
                    # corresponding backtest reference at all (not even a
                    # still-open one) -- a genuine unexplained gap, not a
                    # clean match. Preserves the pre-bookmark behavior for
                    # this case (a node whose backtest replay has zero
                    # trades at all, or whose bookmark has caught up past
                    # everything the backtest knows about).
                    touched += 1
                    db.log_daily_track_reconciliation(
                        wl_id=node['id'], ticker=node['ticker'], check_date=today,
                        actual_state='open_different_trade', backtest_state='flat', bar_match=False,
                        action='ambiguous_position', explained_by_price=False,
                        detail="daily-track is holding a position that matches neither the backtest's "
                               "current reference trade nor any resolvable flat/closed state -- unexplained",
                    )
                else:
                    db.log_daily_track_reconciliation(
                        wl_id=node['id'], ticker=node['ticker'], check_date=today,
                        actual_state='flat', backtest_state='flat', bar_match=True,
                        action='match', explained_by_price=None,
                    )
                break

            backtest_state = 'open' if bt_ref['result'] == OPEN else 'closed'
            target_signal_i = bt_ref['signal_i']

            actual_open = db.get_open_position_by_wl_id(node['id'], paper=True)
            actual_open_signal_i = (
                _bar_index_for(df_h, actual_open.get('signal_bar_time') or actual_open['signal_time'])
                if actual_open else None)

            actual_ref = actual_closed = None
            if actual_open is not None and actual_open_signal_i == target_signal_i:
                actual_ref, actual_state = actual_open, 'open'
            else:
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

            common = dict(
                wl_id=node['id'], ticker=node['ticker'], check_date=today,
                actual_state=actual_state, backtest_state=backtest_state,
                actual_entry_price=actual_ref['entry_price'] if actual_ref else None,
                actual_entry_time=actual_ref['entry_time'] if actual_ref else None,
                actual_signal_price=actual_ref['signal_price'] if actual_ref else None,
                actual_exit_time=actual_closed['exit_time'] if actual_closed else None,
                backtest_entry_price=bt_ref['entry_p'],
                backtest_entry_time=df_h.index[bt_ref['entry_i']].strftime('%Y-%m-%d %H:%M:%S'),
                backtest_signal_z=bt_ref['signal_z'],
                backtest_exit_time=(df_h.index[bt_ref['exit_i']].strftime('%Y-%m-%d %H:%M:%S')
                                     if backtest_state == 'closed' else None),
            )

            if actual_state == 'open_different_trade':
                touched += 1
                db.log_daily_track_reconciliation(
                    action='ambiguous_position', explained_by_price=False, bar_match=False,
                    detail="daily-track is holding a position that matches neither the backtest's "
                           "current reference trade nor any resolvable flat/closed state -- unexplained",
                    **common)
                any_logged = True
                break  # non-terminal -- a genuine unresolved gap, keep surfacing it

            if actual_state == 'flat':
                # Grace gate -- a miss is only conclusively permanent once
                # daily-track has genuinely had its full chance to enter:
                # the signal bar must predate the CURRENT session. Without
                # this, a trade whose signal bar is from earlier TODAY could
                # be marked terminal before daily-track has had its
                # structurally-guaranteed later chance to fire off the
                # fully-closed bar (see the entry-timing note in the
                # function docstring) -- found by the cold review; the
                # narrow real trigger is a daemon that's up at reconcile
                # time (16:05 ET) but missed an earlier signal window that
                # same day.
                signal_bar_date = df_h.index[target_signal_i].date()
                grace_ok = signal_bar_date < today_date
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
                if not grace_ok:
                    detail += " -- signal bar is from the current session, too soon to call this a permanent miss"
                db.log_daily_track_reconciliation(
                    action=action, explained_by_price=explained, backtest_lower_band=lower_band_val,
                    detail=detail, bar_match=False, **common)
                any_logged = True
                if not _daily_track_comparison_terminal(actual_state, backtest_state, grace_ok=grace_ok):
                    break
                # Advance AFTER a successful log write, not before -- a crash/
                # failure between the two used to leave the bookmark advanced
                # with no row to show for it, permanently skipping the trade
                # (found by the cold review).
                db.set_daily_track_bookmark(node['id'], df_h.index[target_signal_i].strftime('%Y-%m-%d %H:%M:%S'))
                continue

            if actual_state == 'open':
                if backtest_state == 'open':
                    db.log_daily_track_reconciliation(action='match', explained_by_price=None, bar_match=True, **common)
                    any_logged = True
                    break  # both still in progress -- non-terminal, keep watching
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
                any_logged = True
                break  # daily-track's own position is still open -- non-terminal, keep watching

            # actual_state == 'closed'
            if backtest_state == 'open':
                # exit_early -- daily-track's own side is done, but no
                # counterfactual has run yet. NOT terminal (revised from the
                # first version, which treated this as terminal and silently
                # discarded the exit-bar comparison): only the backtest's
                # LATEST trade can be OPEN, so a bounded number of nights
                # (capped by max_hold_hours) turns this into the real
                # closed/closed comparison below -- found by the contextual
                # review to be this tool's headline exit-side deliverable,
                # thrown away for nothing by advancing early.
                touched += 1
                db.log_daily_track_reconciliation(
                    action='exit_early', explained_by_price=None, bar_match=False,
                    detail="daily-track already closed this trade but the backtest replay still shows "
                           "it open -- exits aren't price-source-isolated, no counterfactual to run yet "
                           "(will be re-checked once the backtest's own reference trade closes)",
                    **common)
                any_logged = True
                break

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
            else:
                touched += 1
                db.log_daily_track_reconciliation(
                    action='exit_bar_mismatch', explained_by_price=False, bar_match=False,
                    detail=f"both sides closed this trade, but on different bars (daily-track exit "
                           f"resolved to bar {actual_exit_i}, backtest exit_i={bt_ref['exit_i']}) -- "
                           f"unexplained exit-side divergence", **common)
            any_logged = True
            # Both sides closed -- terminal either way (match or mismatch),
            # advance AFTER the log write.
            db.set_daily_track_bookmark(node['id'], df_h.index[target_signal_i].strftime('%Y-%m-%d %H:%M:%S'))
            continue
    return touched


# ---------------------------------------------------------------------------
# reconcile_overlay_nodes -- nightly pure-observation reconcile for the
# drought/addon/skim paper mechanisms. Item 3.5 of docs/design.md's 2026-08-07
# staged checklist: must exist before any staged real-order testing, same
# reason reconcile_daily_track_nodes was built and trusted before core's own
# live-vs-backtest parity claims were.
#
# Deliberately SIMPLER than reconcile_daily_track_nodes' bar-level
# entry/exit price-explainability recheck (the "would Close alone have
# fired this" counterfactual) -- that machinery is real, validated,
# core-specific work, and re-deriving an equivalent for three different
# mechanisms with three different backtest functions is real future scope,
# not built here. What this DOES do, honestly: replay each mechanism's own
# validated backtest function against real cached data through today, and
# compare its most-recent-resolved-trade (or, for skim, its full implied
# event count) against what paper actually recorded. A logic bug (wrong
# entry, missed entry, wrong trade count) still shows up as 'unexplained' --
# what's NOT caught here is the finer "was this specific divergence just
# live-tick-vs-Close noise" question core's reconcile answers. Flagged as a
# known, deliberate scope reduction, not a silent gap.
# ---------------------------------------------------------------------------

def _overlay_mode(node):
    """Part 9 (docs/plans/real_order_execution_drought_addon.md): a non-paper
    node reconciles against the real trade_log/open_positions/addon_legs
    tables, not paper_trade_log/paper_positions/paper_addon_legs -- reuses
    the same 'live'/'dry_run' distinction schwab_safety._coverage_mode makes
    elsewhere in this codebase."""
    if node.get('state') != 'paper':
        return 'dry_run' if effectively_dry_run(node.get('account'), node) else 'live'
    return 'daily_sync' if node.get('paper_role') == 'daily_sync' else 'paper'


def _overlay_paper_flag(mode):
    """True if this reconcile mode reads the paper_* tables, False if it
    reads the real tables (Part 9)."""
    return mode not in ('live', 'dry_run')


def _with_arm_pct(node):
    """get_trades_and_bars/generate_drought_trades/generate_addon_trades all
    read node['arm_pct'] and node['z'] directly (mirrors
    drought_detection_test.load_nodes' own normalization) -- a raw
    watch_list row has neither key as-is (arm_pct: TrailingBothZScoreBreakout
    stores it in arm_sell_pct, everything else in take_profit, per
    signals_db._tp_or_arm_pct; z: the real column is z_score_threshold).
    Found live running reconcile_overlay_nodes for the first time -- both
    replay functions raised KeyError until this normalization was added."""
    node = dict(node)
    node['arm_pct'] = node['arm_sell_pct'] if node['strategy'] == 'TrailingBothZScoreBreakout' else node['take_profit']
    node['z'] = node['z_score_threshold']
    return node


def _reconcile_drought(node, check_date, mode):
    from scripts.stacked_model.drought import generate_drought_trades
    wl_id, ticker = node['id'], node['ticker']
    node = _with_arm_pct(node)
    common = dict(wl_id=wl_id, ticker=ticker, mechanism='drought', mode=mode, check_date=check_date)
    try:
        bt_trades, df_h = generate_drought_trades(
            node, confirm_days=node.get('drought_confirm_days') or 10,
            vol_gate=node.get('drought_vol_gate'), sl_pct=node.get('drought_sl_pct_override'),
            arm_pct=node.get('drought_arm_pct_override'), trail_pct=node.get('drought_trail_pct_override'),
        )
    except Exception as e:
        db.log_overlay_reconciliation(actual_state='unknown', backtest_state='replay_failed',
                                       match=False, action='replay_failed', detail=str(e), **common)
        return
    if not bt_trades:
        db.log_overlay_reconciliation(actual_state='n/a', backtest_state='no_drought_windows',
                                       match=True, action='no_backtest_data',
                                       detail='no fully-resolved drought window exists yet for this node',
                                       **common)
        return
    bt_ref = bt_trades[-1]
    bt_entry_time = df_h.index[bt_ref['entry_i']]
    backtest_state = (f"entry_i={bt_ref['entry_i']} entry_time={bt_entry_time} "
                       f"exit_reason={bt_ref['exit_reason']} ret={bt_ref['ret']:.4f}")
    # Match by proximity to the backtest's entry timestamp, not an exact
    # equality -- generate_drought_trades' entry_bar is the Open leg
    # immediately after confirmation, which is the same real bar paper's own
    # entry targets, but wall-clock float/string formatting differences
    # between the two sides make an exact string match brittle.
    # Part 9: a live/dry_run node reconciles against the real trade_log, not
    # paper_trade_log.
    trade_log_table = 'paper_trade_log' if _overlay_paper_flag(mode) else 'trade_log'
    with db._conn() as c:
        row = c.execute(
            f"SELECT entry_time, exit_reason, pnl_pct FROM {trade_log_table} WHERE wl_id=? "
            f"AND position_source='drought_overlay' ORDER BY entry_time DESC LIMIT 1",
            (wl_id,)
        ).fetchone()
    if row is None:
        db.log_overlay_reconciliation(
            actual_state='flat', backtest_state=backtest_state, match=False, action='entry_miss_unexplained',
            detail='backtest shows a resolved drought window with no matching paper trade at all',
            **common)
        return
    actual_entry_time = datetime.strptime(row['entry_time'], '%Y-%m-%d %H:%M:%S')
    same_window = abs((actual_entry_time - bt_entry_time).total_seconds()) < 3600 * 4
    actual_state = f"entry_time={row['entry_time']} exit_reason={row['exit_reason']} pnl_pct={row['pnl_pct']}"
    if same_window:
        db.log_overlay_reconciliation(actual_state=actual_state, backtest_state=backtest_state,
                                       match=True, action='match', **common)
    else:
        db.log_overlay_reconciliation(
            actual_state=actual_state, backtest_state=backtest_state, match=False,
            action='entry_bar_mismatch',
            detail=f"paper's most recent drought trade entered at {row['entry_time']}, backtest's "
                   f"most recent resolved window entered at {bt_entry_time} -- more than 4h apart",
            **common)


def _reconcile_addon(node, check_date, mode):
    from scripts.stacked_model.add_on import generate_addon_trades
    from scripts.drought_overlay_test import get_trades_and_bars
    wl_id, ticker = node['id'], node['ticker']
    node = _with_arm_pct(node)
    common = dict(wl_id=wl_id, ticker=ticker, mechanism='addon', mode=mode, check_date=check_date)
    try:
        core_trades, df_h = get_trades_and_bars(node)
        bt_addon_trades = generate_addon_trades(core_trades, df_h)
    except Exception as e:
        db.log_overlay_reconciliation(actual_state='unknown', backtest_state='replay_failed',
                                       match=False, action='replay_failed', detail=str(e), **common)
        return
    if not bt_addon_trades:
        db.log_overlay_reconciliation(actual_state='n/a', backtest_state='no_armed_core_trades',
                                       match=True, action='no_backtest_data',
                                       detail='no core trade in this node\'s history has ever armed', **common)
        return
    bt_ref = bt_addon_trades[-1]
    bt_arm_time = df_h.index[bt_ref['entry_i']]  # add_on.py stores the arm bar in entry_i
    backtest_state = f"arm_time={bt_arm_time} raw_ret={bt_ref['raw_ret']:.4f}"
    # Part 9: a live/dry_run node reconciles against the real addon_legs
    # table, not paper_addon_legs.
    addon_legs_table = 'paper_addon_legs' if _overlay_paper_flag(mode) else 'addon_legs'
    with db._conn() as c:
        row = c.execute(
            f"SELECT entry_time, exit_reason, pnl_pct, status FROM {addon_legs_table} WHERE wl_id=? "
            f"ORDER BY entry_time DESC LIMIT 1", (wl_id,)
        ).fetchone()
    if row is None:
        db.log_overlay_reconciliation(
            actual_state='flat', backtest_state=backtest_state, match=False, action='entry_miss_unexplained',
            detail='backtest shows a core trade that armed (add-on eligible) with no matching paper leg at all',
            **common)
        return
    actual_entry_time = datetime.strptime(row['entry_time'], '%Y-%m-%d %H:%M:%S')
    same_window = abs((actual_entry_time - bt_arm_time).total_seconds()) < 3600 * 4
    actual_state = f"entry_time={row['entry_time']} status={row['status']} pnl_pct={row['pnl_pct']}"
    action = 'match' if same_window else 'entry_bar_mismatch'
    db.log_overlay_reconciliation(actual_state=actual_state, backtest_state=backtest_state,
                                   match=same_window, action=action,
                                   detail=None if same_window else
                                   f"paper's most recent add-on leg opened at {row['entry_time']}, backtest's "
                                   f"most recent armed core trade armed at {bt_arm_time} -- more than 4h apart",
                                   **common)


def _reconcile_skim(node, check_date, mode):
    """Replays skim_only (NOT manual_redeploy_overlay) against the SAME
    equity series check_paper_skim itself derives from real closed core
    trades -- skim_only is the reference's own "skim math only, redeploy
    structurally disabled" variant (skim_reserve.py's own docstring: "serves
    as the upper bound... never redeployed"), which is the correct match to
    what paper actually does: paper's redeploy is alert-only and NEVER
    automatically moves money, unlike manual_redeploy_overlay, which
    executes a real redeploy on a delay. Comparing against that function
    instead would count real reference redeploys paper never performs,
    making the skim count noisy for no reason. This is a genuine
    self-consistency check of check_paper_skim's incremental per-trade-close
    translation against the reference's own full-array loop, using real
    accumulated trade history every night, not a synthetic curve."""
    from scripts.stacked_model.skim_reserve import skim_only
    wl_id, ticker = node['id'], node['ticker']
    common = dict(wl_id=wl_id, ticker=ticker, mechanism='skim', mode=mode, check_date=check_date)
    with db._conn() as c:
        rows = c.execute(
            "SELECT pnl_pct FROM paper_trade_log WHERE wl_id=? AND position_source='core' "
            "AND pnl_pct IS NOT NULL ORDER BY exit_time", (wl_id,)
        ).fetchall()
    n = len(rows) + 1
    if n < 3:
        db.log_overlay_reconciliation(actual_state='n/a', backtest_state='insufficient_trade_history',
                                       match=True, action='no_backtest_data',
                                       detail=f"only {n - 1} closed core trades so far", **common)
        return
    import numpy as np
    equity = np.ones(n)
    for i, r in enumerate(rows):
        equity[i + 1] = equity[i] * (1 + r['pnl_pct'] / 100.0)
    skim_step = node.get('skim_step')
    skim_frac = node.get('skim_frac')
    kwargs = {}
    if skim_step is not None:
        kwargs['skim_step'] = skim_step
    if skim_frac is not None:
        kwargs['skim_frac'] = skim_frac
    # Passing `equity` as BOTH strategy_equity and spy_equity is deliberate,
    # not "the arg is unused" (an earlier comment here said that -- wrong,
    # corrected by review, 2026-08-09: skim_redeploy_overlay's r_spy IS
    # applied every step). Forcing r_spy == r_strat keeps reserve_frac_curve
    # flat between skims, which is exactly what makes a plain np.diff(...)
    # count clean skim events below -- any real SPY series would make the
    # reserve fraction drift on its own between skims and corrupt the count.
    _total_curve, reserve_frac_curve = skim_only(equity, equity, **kwargs)
    bt_skim_count = int((np.diff(reserve_frac_curve) > 1e-9).sum())
    with db._conn() as c:
        actual_skim_count = c.execute(
            "SELECT COUNT(*) AS n FROM skim_reserve_log WHERE wl_id=? AND action='skim'", (wl_id,)
        ).fetchone()['n']
    backtest_state = f"bt_skim_count={bt_skim_count}"
    actual_state = f"actual_skim_count={actual_skim_count}"
    match = bt_skim_count is None or bt_skim_count == actual_skim_count
    db.log_overlay_reconciliation(
        actual_state=actual_state, backtest_state=backtest_state, match=match,
        action='match' if match else 'skim_count_mismatch',
        detail=None if match else
        f"reference implies {bt_skim_count} skims against this node's real trade history, "
        f"paper's own incremental engine recorded {actual_skim_count}",
        **common)


def reconcile_overlay_nodes():
    """Nightly pure-observation reconcile for every drought_overlay_enabled/
    addon_enabled/skim_enabled node, across both live-track and daily-track
    (paper_role='daily_sync') paper nodes. Never mutates any real state --
    same reasoning as reconcile_daily_track_nodes: auto-correcting divergence
    erases exactly the signal this comparison exists to produce. Idempotent
    per (wl_id, mechanism, mode, check_date) via overlay_reconciliation_log's
    UNIQUE constraint -- log_overlay_reconciliation uses INSERT OR REPLACE, so
    a same-day rerun after a restart refreshes that night's row with the
    latest result rather than duplicating it.

    Each node/mechanism call below is individually try/excepted -- found by
    a paired review (2026-08-09) that only the inner generate_*_trades replay
    was guarded inside _reconcile_drought/_reconcile_addon (and _reconcile_
    skim had no guard at all), so a malformed row on ONE node (a bad
    _with_arm_pct lookup, an unparseable entry_time, etc.) would raise past
    active_signals._guarded's single wrap around this whole function and
    silently abort reconciliation for every node still left to check that
    night. Same failure shape reconcile_daily_track_nodes already has
    (not fixed here, out of scope) -- fixed here since it's new code.

    KNOWN LIMITATION (documented, not silently glossed -- found by an
    independent review, 2026-08-09): each _reconcile_* function replays its
    backtest function over the node's ENTIRE cached price history and
    compares against paper's own recorded state, with no "only compare from
    when this mechanism was actually enabled" anchor. For a node enabled
    mid-history (the common case -- these mechanisms attach to nodes that
    have already been core-paper-trading for a while), this can produce a
    persistent, not-self-resolving mismatch: the backtest's single
    most-recent-ever resolved trade/window may predate enablement entirely,
    with nothing in paper's own history to match it against.
    check_paper_skim's own seeding (skim_ref/peak/strategy_value seeded from
    real current equity on first call, not a flat 1.0/starting_notional
    baseline) fixes the live mechanism's OWN behavior -- it no longer fires
    one false giant skim capturing pre-enablement history -- but does not by
    itself make _reconcile_skim's full-history comparison line up with
    paper's enablement-onward one. A real fix needs a persisted
    "mechanism enabled at" timestamp per node to anchor the backtest replay
    window; not built this session (real schema work, deferred deliberately
    rather than rushed at session-wrap)."""
    check_date = datetime.now().strftime('%Y-%m-%d')
    for node in db.get_watchlist():
        mode = _overlay_mode(node)
        if node.get('drought_overlay_enabled'):
            try:
                _reconcile_drought(node, check_date, mode)
            except Exception as e:
                print(f"  [warn] reconcile_overlay_nodes: drought check failed for {node['ticker']}: {e}")
        if node.get('addon_enabled'):
            try:
                _reconcile_addon(node, check_date, mode)
            except Exception as e:
                print(f"  [warn] reconcile_overlay_nodes: addon check failed for {node['ticker']}: {e}")
        if node.get('skim_enabled'):
            try:
                _reconcile_skim(node, check_date, mode)
            except Exception as e:
                print(f"  [warn] reconcile_overlay_nodes: skim check failed for {node['ticker']}: {e}")


def check_paper_addon_trigger(node, pos, price, now):
    """Opens a margin add-on-at-arm leg the moment a core paper position's
    trailing-sell just armed -- hooks the SAME just_activated_trailing event
    check_paper_sells already detects (mirrors notify_trailing_activated's
    real-money equivalent), rather than inventing a separate detection path,
    per docs/design.md's 2026-08-07 state machine ("TRIGGERED (core's
    trail_state['trailing'] flips True)").

    Sizes the leg to match the core position's CURRENT dollar value at this
    same price (shares = pos['shares'] -- the add-on buys the same share
    count at the same price the arm event fired at, which is exactly "100%
    of the current position's market value" since both legs are priced
    identically at this instant). Deliberately does NOT gate on account/
    margin-eligibility here -- that is a real-order-placement concern for
    the live wiring layer (AGQ's live node is the only one in a real margin
    account; SOXL/KORU can't actually borrow), not something paper
    simulation should hide behind. Paper trading shows what the mechanism
    would do everywhere it's enabled; eligibility filtering happens once
    real orders are involved."""
    if not node.get('addon_enabled'):
        return
    if pos.get('position_source') != 'core':
        # docs/design.md's truth-table scopes addon_leg to "core state: armed"
        # only -- a drought_overlay position arming is not an add-on trigger
        # (it's a different position, not the strategy's normal armed core
        # trade the design validated the sizing math against).
        return
    if db.get_open_addon_leg_by_parent(pos['id'], paper=True):
        return
    leg_id = db.open_addon_leg(pos, shares=pos['shares'], entry_price=price, entry_time=now,
                               paper=True, is_dry_run_sim=bool(pos.get('is_dry_run_sim')))
    db.log_coverage_event("addon_entry_fill", "paper", ticker=pos['ticker'], node_id=pos.get('wl_id'),
                           position_id=pos['id'], result="filled",
                           detail=f"leg_id={leg_id} shares={pos['shares']} price={price:.4f}")
    if node.get('paper_alert_verbose'):
        _post_message(f"🧪➕ PAPER ADD-ON — {pos['ticker']}  {pos['shares']}sh @ ${price:.4f} (arm)")


def close_paper_addon_leg_if_open(pos_id, ticker, wl_id, exit_price, exit_time, exit_reason, verbose):
    """Closes a parent core position's add-on leg (if one is open) in
    lockstep with the parent's own close -- per the design's state machine,
    an add-on leg NEVER independently triggers its own SL/TRAIL check, it
    only ever closes because its parent just did. Call this immediately
    after db.close_position() for the parent, same reason/exit_price/
    exit_time, every time (a no-op if no leg exists)."""
    leg = db.get_open_addon_leg_by_parent(pos_id, paper=True)
    if leg is None:
        return
    db.close_addon_leg(leg['id'], exit_price, exit_time, exit_reason, paper=True)
    db.log_coverage_event("addon_exit_fill", "paper", ticker=ticker, node_id=wl_id,
                           position_id=pos_id, result=exit_reason, detail=f"leg_id={leg['id']} price={exit_price:.4f}")
    if verbose:
        _post_message(f"🧪➕ PAPER ADD-ON CLOSED — {ticker}  {exit_reason} @ ${exit_price:.4f}")


# Defaults applied when a node's own skim_step/skim_frac columns are NULL --
# mirror scripts/stacked_model/skim_reserve.py's validated SKIM_STEP/SKIM_FRAC
# module constants exactly (not duplicated as a hardcoded fallback so a
# future re-validated default only needs changing in one place).
def _skim_defaults():
    from scripts.stacked_model.skim_reserve import SKIM_STEP, SKIM_FRAC, REDEPLOY_THRESHOLDS
    return SKIM_STEP, SKIM_FRAC, REDEPLOY_THRESHOLDS


def _paper_core_equity(wl_id):
    """Compounded equity multiplier from every closed CORE paper trade for
    this node, in exit order -- recomputed on demand rather than persisted,
    mirroring skim_reserve.daily_equity_from_trades' own definition (realized
    trade returns compounded, flat between trades) without float-drift risk
    from an incrementally-updated running total. Starts at 1.0 (no trades
    yet), matching manual_redeploy_overlay's strategy_equity[0]=1.0.

    This is the UNDILUTED benchmark -- what the strategy would be worth if
    100% of capital had stayed deployed the whole time, regardless of any
    skims actually taken. Used ONLY to drive the skim-trigger/peak/decline
    timing (matching manual_redeploy_overlay's own use of strategy_equity[i]
    for exactly that, and nothing else) -- the REAL deployed dollar value
    (which shrinks as skims accumulate) is tracked separately via
    watch_list.skim_strategy_value, not derived from this."""
    with db._conn() as c:
        rows = c.execute(
            "SELECT pnl_pct FROM paper_trade_log WHERE wl_id=? AND position_source='core' "
            "AND pnl_pct IS NOT NULL ORDER BY exit_time",
            (wl_id,)
        ).fetchall()
    equity = 1.0
    for r in rows:
        equity *= (1 + r['pnl_pct'] / 100.0)
    return equity


def _latest_core_trade_pnl_pct(wl_id):
    """The most recently closed CORE trade's own realized pnl_pct -- the
    single trade that triggered THIS call to check_paper_skim (called once
    per core close). None if this node has never closed a core trade."""
    with db._conn() as c:
        row = c.execute(
            "SELECT pnl_pct FROM paper_trade_log WHERE wl_id=? AND position_source='core' "
            "AND pnl_pct IS NOT NULL ORDER BY exit_time DESC LIMIT 1",
            (wl_id,)
        ).fetchone()
    return row['pnl_pct'] if row else None


def _spy_price_at(ts):
    """Real SPY Close nearest at-or-before ts, from the same hourly cache
    every other cached-data lookup in this codebase reads -- used to mark
    the skim reserve to market (see check_paper_skim). None if no cached SPY
    data exists at or before ts."""
    df_h, _ = _load_cache('SPY')
    if df_h is None or df_h.empty:
        return None
    prior = df_h[df_h.index <= ts]
    if prior.empty:
        return None
    return float(prior['Close'].iloc[-1])


def check_paper_skim(node):
    """One incremental step of scripts/stacked_model/skim_reserve.py's
    validated manual_redeploy_overlay loop, translated from "iterate a full
    equity array" to "given the equity implied by trades closed so far,
    advance the persisted state by one step" -- called after every CORE
    paper trade close (equity only changes at a trade close; between closes
    it's flat, exactly the loop's own between-bar assumption). All state
    persists on the watch_list row instead of the loop's local variables,
    since a live daemon restarts between calls where the backtest's single
    process never did.

    Tracks REAL dollar values (skim_strategy_value/skim_reserve_balance),
    not the reference's normalized equity-unit weights (w_strategy/w_spy) --
    equivalent economics, just expressed in dollars using this node's
    starting_notional as the anchor, which is more directly useful for
    everything downstream (Slack alerts, the eventual real redeploy amount)
    than a unitless fraction. The reserve is marked to a REAL SPY price at
    every call (fixing a gap found by review: the first version never
    modeled the reserve actually earning/losing anything).

    Skim itself (moving skim_frac of the CURRENTLY-DEPLOYED strategy value,
    not the full undiluted notional -- fixed 2026-08-09, see
    skim_strategy_value's schema comment for the bug this replaced) is
    real-shape-identical to the backtest and fires automatically here.
    Redeploy is deliberately alert-only (never automated) -- this only ever
    calls record_skim_event(..., 'redeploy_alert', ...) and posts a Slack
    message; a human decides whether/how much to actually move back,
    exactly per the design's "reconcile as one action vs sync" framing
    reused from core's own daily-track design. Already reachable from the
    live daemon -- called from check_paper_sells (below) on every CORE paper
    trade close, and check_paper_sells is unconditionally polled by
    active_signals.py's run_loop, so no separate wiring was needed here
    (unlike check_paper_drought_entry/check_paper_drought_handoff, which
    required new call sites and their own paired review)."""
    if not node.get('skim_enabled'):
        return
    wl_id, ticker, account = node['id'], node['ticker'], node.get('account')
    equity = _paper_core_equity(wl_id)
    default_step, default_frac, (thresh_80, thresh_100) = _skim_defaults()
    skim_step = node.get('skim_step') if node.get('skim_step') is not None else default_step
    skim_frac = node.get('skim_frac') if node.get('skim_frac') is not None else default_frac
    starting_notional = node.get('starting_notional') or 50000

    # First-ever call for this node (skim_strategy_value still NULL) seeds
    # EVERYTHING from the node's actual current equity, not a flat 1.0/
    # starting_notional baseline -- a node can have real paper-trading
    # history predating skim_enabled=1 being set, and `equity` already
    # reflects all of it. Seeding flat would otherwise fire one giant
    # false skim capturing the entire pre-existing gain in a single shot
    # (found by review, 2026-08-09) and make reconcile_overlay_nodes'
    # skim-count comparison permanently wrong (the reference implies many
    # incremental skims across that same history; a flat seed only ever
    # produces one). On a subsequent call, strategy_value instead compounds
    # by just the LATEST closed trade's own return -- mirrors
    # manual_redeploy_overlay's `val_strategy = total * w_strategy *
    # (1 + r_strat)` step, which happens every bar before that bar's skim
    # check (also found missing entirely in an earlier version of this fix:
    # strategy_value only ever shrank via skims and never grew with the
    # strategy's own realized gains).
    is_first_call = node.get('skim_strategy_value') is None
    if is_first_call:
        strategy_value = starting_notional * equity
        skim_ref = equity
        peak = equity
        min_since_peak = equity
    else:
        strategy_value = node['skim_strategy_value']
        latest_pnl_pct = _latest_core_trade_pnl_pct(wl_id)
        if latest_pnl_pct is not None:
            strategy_value *= (1 + latest_pnl_pct / 100.0)
        skim_ref = node['skim_ref']
        peak = node['skim_peak_before_decline']
        min_since_peak = node['skim_min_since_peak']
    declining = bool(node.get('skim_declining'))
    alert_80_sent = bool(node.get('skim_alert_80_sent'))
    alert_100_sent = bool(node.get('skim_alert_100_sent'))
    reserve_value = node.get('skim_reserve_balance') or 0.0
    now = datetime.now()
    spy_price_now = _spy_price_at(now)

    # Mark the reserve to market since the last mark, using real SPY price
    # drift -- a no-op (r_spy=0) if there's no reserve yet or no prior mark.
    last_mark_str = node.get('skim_last_mark_time')
    if reserve_value > 0 and last_mark_str and spy_price_now is not None:
        spy_price_last = _spy_price_at(datetime.strptime(last_mark_str, '%Y-%m-%d %H:%M:%S'))
        if spy_price_last:
            reserve_value *= (spy_price_now / spy_price_last)

    # -- Skim: a fraction of the DEPLOYED strategy value moves to the
    # reserve, mirroring manual_redeploy_overlay's `moved = w_strategy *
    # skim_frac` (the sleeve shrinks by skim_frac at every skim, so later
    # skims move less in absolute terms than the first -- the earlier
    # version always recomputed off the full undiluted notional instead,
    # diverging from the validated model in the wrong direction). --
    if equity >= skim_ref * (1 + skim_step):
        amount = strategy_value * skim_frac
        strategy_value -= amount
        reserve_value += amount
        skim_ref = equity
        shares_delta = amount / spy_price_now if spy_price_now else None
        db.record_skim_event(wl_id, account, 'skim', amount, reserve_shares_delta=shares_delta,
                              reserve_price=spy_price_now, reference_value=equity,
                              detail=f"equity={equity:.4f} step={skim_step} frac={skim_frac}",
                              new_balance_override=reserve_value)
        if account and spy_price_now and shares_delta:
            db.update_skim_reserve_pool(account, shares_delta, spy_price_now)
        db.log_coverage_event("skim_fire", "paper", ticker=ticker, node_id=wl_id, result="fired",
                               detail=f"equity={equity:.4f} amount={amount:.2f}")
        if node.get('paper_alert_verbose'):
            _post_message(f"🧪💰 PAPER SKIM — {ticker}  moved ${amount:,.2f} to reserve (equity={equity:.4f})")

    # -- Redeploy alert (mirrors the declining/peak/min_since_peak/armed-set
    # logic exactly -- the 2026-08-08 CRITICAL fix: a threshold may only fire
    # on RECOVERY past it, and only if it was genuinely crossed on the way
    # DOWN first (min_since_peak below peak*thr), not a wiggle at the peak.
    # `has_reserve` mirrors the reference's `w_spy > 0` guard, dropped from
    # the first version -- without it, a node that never skimmed can still
    # alert "consider redeploying" against a genuinely empty reserve, found
    # by both review passes (independent review differentially confirmed
    # this is 100% of the divergence from the reference across 500 random
    # equity paths). --
    has_reserve = reserve_value > 0
    new_alert_80, new_alert_100 = alert_80_sent, alert_100_sent
    alert_just_fired = False
    if equity > peak:
        if declining and min_since_peak < peak and not alert_100_sent and has_reserve:
            db.record_skim_event(wl_id, account, 'redeploy_alert', 0.0, reference_value=equity,
                                  detail=f"threshold={thresh_100} equity={equity:.4f} peak={peak:.4f}",
                                  new_balance_override=reserve_value)
            db.log_coverage_event("skim_redeploy_alert", "paper", ticker=ticker, node_id=wl_id,
                                   result="alerted", detail=f"threshold={thresh_100}")
            if node.get('paper_alert_verbose'):
                _post_message(f"🧪🔁 PAPER REDEPLOY ALERT — {ticker}  recovered to {thresh_100:.0%} of "
                               f"pre-decline peak (${peak:.4f} equity-units) -- consider redeploying reserve")
            alert_just_fired = True
        # Full round trip past the old peak -- re-arm both thresholds for the
        # NEXT decline cycle, matching armed = set(thresholds).
        declining, peak, min_since_peak = False, equity, equity
        new_alert_80, new_alert_100 = False, False
    else:
        min_since_peak = min(min_since_peak, equity)
        if equity < peak * 0.999:
            declining = True
        if declining and not alert_80_sent and has_reserve and min_since_peak < peak * thresh_80 <= equity:
            db.record_skim_event(wl_id, account, 'redeploy_alert', 0.0, reference_value=equity,
                                  detail=f"threshold={thresh_80} equity={equity:.4f} peak={peak:.4f}",
                                  new_balance_override=reserve_value)
            db.log_coverage_event("skim_redeploy_alert", "paper", ticker=ticker, node_id=wl_id,
                                   result="alerted", detail=f"threshold={thresh_80}")
            if node.get('paper_alert_verbose'):
                _post_message(f"🧪🔁 PAPER REDEPLOY ALERT — {ticker}  recovered to {thresh_80:.0%} of "
                               f"pre-decline peak (${peak:.4f} equity-units) -- consider redeploying reserve")
            new_alert_80 = True
            alert_just_fired = True

    state_kwargs = dict(skim_ref=skim_ref, skim_peak_before_decline=peak,
                         skim_min_since_peak=min_since_peak, skim_declining=1 if declining else 0,
                         skim_alert_80_sent=1 if new_alert_80 else 0, skim_alert_100_sent=1 if new_alert_100 else 0,
                         skim_strategy_value=strategy_value, skim_reserve_balance=reserve_value)
    if spy_price_now is not None:
        state_kwargs['skim_last_mark_time'] = now.strftime('%Y-%m-%d %H:%M:%S')
    if alert_just_fired:
        state_kwargs['skim_alert_sent_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
    db.set_skim_state(wl_id, **state_kwargs)


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
        if just_activated_trailing and node:
            check_paper_addon_trigger(node, pos, cp, datetime.now())
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
            exit_dt = datetime.now()
            db.close_position(pos['id'], exit_signal_price=cp, exit_price=target,
                               exit_time=exit_dt, exit_reason=reason, paper=True,
                               exit_bar_time=last_bar_ts if at_bar_close else None)
            # The core exit's own observability (coverage event + Slack alert)
            # is recorded BEFORE the add-on/skim follow-on calls below, not
            # after -- found by review, 2026-08-09: the first version ran
            # those calls first, so an exception in either one (or a stale DB
            # missing paper_addon_legs) would silently lose this exit's own
            # coverage event and alert entirely, on top of whatever the
            # follow-on call itself failed to do.
            db.log_coverage_event("exit_fill", "paper", ticker=ticker, position_id=pos['id'],
                                   node_id=pos.get('wl_id'), result=reason, detail=f"price={target:.4f}")
            if verbose:
                _post_message(f"🧪 PAPER SELL — {ticker}  {reason} @ ${target:.4f}")
            try:
                close_paper_addon_leg_if_open(pos['id'], ticker, pos.get('wl_id'), target, exit_dt, reason, verbose)
                if pos.get('position_source') == 'core' and node:
                    # Equity (and therefore skim/redeploy state) only changes when
                    # a CORE trade closes -- a drought_overlay close doesn't feed
                    # _paper_core_equity's query (filtered to position_source='core'),
                    # so skipping this call for that case is an optimization, not a
                    # correctness guard, but it's cheap to be explicit about which
                    # close actually matters here.
                    check_paper_skim(node)
            except Exception as e:
                print(f"  [warn] {ticker} addon/skim follow-on failed after a real core exit "
                      f"already closed and alerted: {e} — not re-raising")
            paper_sell_alerted.add((pos['id'], last_bar_ts))
