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
from signals_compute import _current_price, check_sell_condition
from signals_blocks import _post_message
from signals_helpers import buy_order_sizing, log_poll, _pos_key


def start_paper_buy(node, sig):
    """Called from active_signals._scan_buy_signals on a BUY for a research-mode,
    automation-enabled ticker. Dispatches to start_paper_market_buy for a
    non-trailing-buy node (Part 4, Section 7) -- a market order fills near-
    immediately, no bounce-fill phase to simulate, unlike a trailing buy."""
    ticker = sig['ticker']
    if not db._is_trailing_buy(node):
        start_paper_market_buy(node, sig)
        return
    if db.get_paper_pending_buy(node['id']) or db.get_open_position_by_wl_id(node['id'], paper=True):
        return
    db.add_paper_pending_buy(node, sig)
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
    if db.get_open_position_by_wl_id(node['id'], paper=True):
        return
    sizing = buy_order_sizing(node, sig)
    if sizing['shares'] < 1:
        return
    db.open_position(node, sig['current_price'], sig['last_bar'], sig['current_price'], datetime.now(),
                      shares=sizing['shares'], paper=True)
    db.log_coverage_event("entry_fill", "paper", ticker=ticker, node_id=node.get('id'), result="market_filled",
                           detail=f"shares={sizing['shares']} price={sig['current_price']:.4f}")
    _post_message(f"🧪 PAPER MARKET BUY — {ticker}  {sizing['shares']}sh @ ${sig['current_price']:.4f}")


def update_paper_buys():
    """Called unconditionally every poll (not gated to signal windows) -- a real
    trailing buy can fill any time after the signal fires."""
    for pb in db.get_paper_pending_buys():
        ticker = pb['ticker']
        node = pb['node']
        price, _ = _current_price(ticker)
        if price is None:
            continue
        running_low = min(pb['running_low'], price)
        trail_buy_pct = node.get('trail_buy_pct') or 0.0
        trigger = running_low * (1 + trail_buy_pct / 100)
        log_poll(f"{ticker} paper_update_buys price={price:.4f} running_low={running_low:.4f} trigger={trigger:.4f}")
        if price > running_low and price >= trigger:
            starting_notional = node.get('starting_notional') or 50000
            shares = int(starting_notional // price)
            if shares < 1:
                print(f"  [paper] {ticker} bounce-fill at ${price:.4f} too small to size a share — dropping pending buy")
                db.clear_paper_pending_buy(node['id'])
                continue
            db.open_position(node, pb['signal_price'], pb['signal_time'], price, datetime.now(),
                              shares=shares, paper=True)
            db.clear_paper_pending_buy(node['id'])
            db.log_coverage_event("entry_fill", "paper", ticker=ticker, node_id=node.get('id'), result="trailing_bounce_filled",
                                   detail=f"shares={shares} price={price:.4f}")
            _post_message(f"🧪 PAPER BUY FILLED — {ticker}  {shares}sh @ ${price:.4f}")
        elif running_low != pb['running_low']:
            db.update_paper_pending_buy_running_low(pb['id'], running_low)


def check_paper_sells(last_seen_bar, paper_sell_alerted, load_cache):
    """Mirrors the real open_positions exit-check block in active_signals.run_loop,
    sharing last_seen_bar with the real block (safe -- a ticker is never
    simultaneously live and research) but using its own dedup set since paper
    position ids are independent of real open_positions ids."""
    for pos in db.get_open_positions(paper=True):
        ticker = pos['ticker']
        df_hourly, _ = load_cache(ticker)
        if df_hourly is None or df_hourly.empty:
            continue
        last_bar_ts = df_hourly.index[-1]
        if (pos['id'], last_bar_ts) in paper_sell_alerted:
            continue
        at_bar_close = last_seen_bar.get(_pos_key(pos)) != last_bar_ts
        if at_bar_close:
            last_seen_bar[_pos_key(pos)] = last_bar_ts
            bar = df_hourly.iloc[-1]
            cp, low, high, op = float(bar['Close']), float(bar['Low']), float(bar['High']), float(bar['Open'])
        else:
            cp, _ = _current_price(ticker)
            if cp is None:
                continue
            low = high = op = cp
        log_poll(f"{ticker} paper_check_sells bar={last_bar_ts} at_bar_close={at_bar_close} "
                 f"cp={cp:.4f} low={low:.4f} high={high:.4f} op={op:.4f}")
        reason, target, just_activated_trailing = check_sell_condition(
            pos, cp, datetime.now(), at_bar_close=at_bar_close, low=low, high=high, open_price=op,
            df_hourly=df_hourly, paper=True)
        if just_activated_trailing:
            _post_message(f"🧪 PAPER trailing-sell armed — {ticker}")
        if reason:
            db.close_position(pos['id'], exit_signal_price=cp, exit_price=target,
                               exit_time=datetime.now(), exit_reason=reason, paper=True)
            db.log_coverage_event("exit_fill", "paper", ticker=ticker, position_id=pos['id'],
                                   node_id=pos.get('wl_id'), result=reason, detail=f"price={target:.4f}")
            _post_message(f"🧪 PAPER SELL — {ticker}  {reason} @ ${target:.4f}")
            paper_sell_alerted.add((pos['id'], last_bar_ts))
