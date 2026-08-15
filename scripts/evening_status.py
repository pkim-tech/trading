"""Evening account check-in (docs/plans/account_checkin_process.md). 4 parts, callable individually.

Usage:
  python scripts/evening_status.py 1   # log warnings, today's trading hours
  python scripts/evening_status.py 2   # real capital-at-stake node states
  python scripts/evening_status.py 3   # trades-vs-kernel + unexplained deviations
  python scripts/evening_status.py 4   # readiness for tomorrow
  python scripts/evening_status.py all
"""
import contextlib
import io
import json
import re
import sqlite3
import subprocess
import sys
import os
import time as time_mod
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Every relative path in this file (and every signals_config/schwab_* default this script's
# imports rely on) assumes CWD == repo root -- real bug, found live 2026-08-13: running from
# scripts/ directly (`cd scripts && python evening_status.py`) broke sqlite3.connect with
# "unable to open database file". chdir once, up front, before anything else touches disk.
os.chdir(ROOT)

import numpy as np

import active_signals as a
import signals_db as db
import signals_helpers as helpers
import signals_compute as compute
import strategies
import schwab_client
import schwab_safety
import scripts.verify_real_trades_vs_kernel as verify
from scripts.coverage_registry import REGISTRY, compute_status, STATUS_ORDER
from scripts.coverage_regression_watch import last_run_statuses, log_run, staleness_for
from scripts.capital_scaling_gate import _git_state
from scripts.coverage_proof_matrix import classify as proof_classify
from scripts.coverage_registry import fake_venue_proof_for
from scripts.paper_vs_backtest_reconcile import resolve_live_track_nodes_by_activity, get_paper_trades
import scripts.daemon_status as daemon_status
import scripts.verify_live_parity as parity

TODAY = datetime.now().strftime('%Y-%m-%d')
ACCOUNT_ORDER = {'brokerage': 0, 'roth': 1, 'ira': 2, 'soxl_ira': 3}
LIVE_DB = "cache/live/trading_live.db"
TOKEN_PATH = Path("cache/live/schwab_token.json")
# Schwab's refresh token is a hard 7-day cap from initial interactive login (schwab_auth.py's
# docstring); schwab-py's TokenMetadata.creation_timestamp deliberately does NOT move on a
# silent refresh, so it really is "when the human last logged in."
REFRESH_TOKEN_DAYS = 7
REAUTH_WARN_DAYS = 2
# Part 3 sub-part 2: a TODAY..TODAY window compares almost nothing (most nodes don't trade
# every day), so a trailing window is what actually gives this check something to compare.
PAPER_WINDOW_DAYS = 7


_DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
_TIME_RE = re.compile(r'^\[(\d{2}):(\d{2}):(\d{2})\]')
_DATE_TIME_RE = re.compile(r'\d{4}-\d{2}-\d{2}\s+(\d{2}):(\d{2})')
_WINDOW_START, _WINDOW_END = dt_time(9, 30), dt_time(16, 0)


def part1():
    print(f"=== Part 1: log warnings ({TODAY} 09:30-16:00 ET) ===")
    log = Path("logs/active_signals.log")
    if not log.exists():
        print("no log file")
        return

    # The log has no per-line date, and warning lines (⚠️) are unprefixed continuation
    # lines under an earlier timestamped block -- so date/time have to be tracked
    # forward as state, updated only by non-schwab_stream lines (that JSON blob embeds
    # many unrelated historical dates/times that would otherwise corrupt the tracker).
    current_date, current_time = None, None
    saw_today = False
    hits = []
    for line in log.read_text(errors='ignore').splitlines():
        if 'schwab_stream' in line:
            continue
        # [cooldown] lines (active_signals.py:483, added 2026-08-14) embed a real
        # timestamp mid-line ("...last exit bar 2026-08-13 09:30:54") that isn't this
        # line's own date/time -- _DATE_TIME_RE's unanchored search would otherwise
        # latch onto that stale, embedded date and corrupt current_date/current_time
        # for every unprefixed line that follows (found by Opus review, 2026-08-14).
        if '[cooldown]' in line:
            continue
        dt_match = _DATE_TIME_RE.search(line)
        if dt_match:
            current_date = dt_match.group(0)[:10]
            current_time = dt_time(int(dt_match.group(1)), int(dt_match.group(2)))
        else:
            d_match = _DATE_RE.search(line)
            if d_match:
                current_date = d_match.group(0)
            t_match = _TIME_RE.match(line)
            if t_match:
                current_time = dt_time(int(t_match.group(1)), int(t_match.group(2)))
        if current_date == TODAY:
            saw_today = True
        if '⚠️' in line:
            if current_date == TODAY and current_time is not None and _WINDOW_START <= current_time <= _WINDOW_END:
                hits.append(line.strip())

    if not saw_today:
        print(f"NOT CHECKED -- no log lines dated {TODAY} found (log may not cover today)")
        return
    print(f"{len(hits)} warning(s)" if hits else "clean, no warnings")
    for h in hits:
        print(f"  {h}")


def real_capital_nodes():
    """Every state='live' node clearing helpers.has_capital_at_stake, across ALL watchlists.

    Direct sqlite scan on purpose. db.get_watchlist() is watchlist-scoped and real live nodes
    are spread across more than one watchlist, so it can't answer this. The previous
    `db.get_watchlist(False)` attempt here was dead code: get_watchlist only special-cases
    `watchlist_id is None`, so False bound watchlist_id=0 and returned [] on every run --
    the "fallback" scan was unconditionally the only path that ever executed.
    """
    con = sqlite3.connect(LIVE_DB)
    con.row_factory = sqlite3.Row
    live_nodes = [dict(r) for r in con.execute("SELECT * FROM watch_list WHERE state='live'")]
    con.close()
    nodes = [n for n in live_nodes if helpers.has_capital_at_stake(n)]
    nodes.sort(key=lambda n: (ACCOUNT_ORDER.get(n.get('account'), 99), n['ticker']))
    return nodes


def effective_notional(node, market_value=None):
    """The real number for 'how much capital is this node actually working with' --
    3-tier priority (user's call, 2026-08-13): (1) market value of an actual OPEN position
    (shares x current price), if one exists -- pass it in via market_value, since knowing
    that requires a position-state lookup this function doesn't do itself; (2)
    helpers._last_sale_recovery -- starting_notional_override, else the last closed
    trade's proceeds; (3) the static starting_notional column, only if nothing has
    happened yet at all. The static column alone goes stale the moment a node closes
    its first real trade, and doesn't reflect an open position's real size either."""
    if market_value is not None:
        return market_value
    try:
        return helpers._last_sale_recovery(node)
    except Exception:
        return node.get('starting_notional', 0)


def _broker_order_check(node, state, pending):
    """Broker order-side reconciliation for all three states, not just pending_entry.

    holding -> is a protective SELL actually resting at the broker right now, and is it the
               one open_positions.sl_order_id claims (plan Scope point 1).
    flat    -> the broker should show NOTHING resting; anything there is an orphan nobody
               is tracking locally.

    Returns (short_flag, detail): short_flag goes in the table row; detail is printed as a
    sub-line ONLY when it's non-boring, so the expected case costs one column, not one line.
    """
    # Ratio format (local/broker), matching sub-part 1's Pos column convention -- a bare "ok"
    # doesn't say what was actually compared (user's call, 2026-08-13).
    acct = node.get('account')
    if not acct:
        return "n/a", None
    if helpers.effectively_dry_run(acct, node):
        # roth/brokerage can carry real state='live' nodes at trading_enabled=False -- their
        # orders are synthesized locally and never reach Schwab, so "broker shows nothing"
        # is correct here, not a finding.
        return "dry", None
    try:
        resting = schwab_client.filter_resting_orders(schwab_client.get_real_orders(acct, node['ticker']))
    except Exception as e:
        return "ERR", f"broker order fetch failed ({e})"

    if pending is not None:
        local_order_id = pending.get('order_id')
        broker_order = next((o for o in resting if o['orderId'] == local_order_id), None)
        # local qty isn't stored on pending_buys (a trailing buy's fill quantity is only
        # fixed once it fills) -- approximate what local expected using the same sizing
        # formula the real order was placed with, at the signal-time price.
        try:
            local_qty = helpers.buy_order_sizing(node, {'ticker': node['ticker'], 'current_price': pending['signal_price']})['shares']
        except Exception:
            local_qty = float('nan')
        broker_qty = broker_order['quantity'] if broker_order else 0
        ratio = f"{local_qty:g}/{broker_qty:g}"
        if broker_order is None:
            return ratio, f"BUY #{local_order_id} NOT resting at broker (local expects {local_qty:g}sh)"
        if broker_qty != local_qty:
            return ratio, f"BUY #{local_order_id} qty local {local_qty:g} vs broker {broker_qty:g}"
        return ratio, None

    sells = [o for o in resting if str(o.get('instruction') or '').upper().startswith('SELL')]
    if state['status'] == 'holding':
        sl_id = (state['real_position'] or {}).get('sl_order_id')
        matched = any(o['orderId'] == sl_id for o in sells)
        ratio = f"1/{1 if matched else 0}"
        if matched:
            return ratio, None
        if sells:
            return ratio, (f"local sl_order_id={sl_id} but broker resting SELLs are "
                            f"{[o['orderId'] for o in sells]}")
        return ratio, f"no resting SELL at broker (local sl_order_id={sl_id})"

    ratio = f"0/{len(resting)}"
    if resting:
        return ratio, (f"flat locally but {len(resting)} order(s) resting at broker: "
                       + ", ".join(f"#{o['orderId']} {o.get('instruction')} {o.get('status')}"
                                   for o in resting))
    return ratio, None


def _part2_activity(nodes):
    """Sub-part 2 -- a plain 'what actually happened today' activity listing across the real
    capital-at-stake nodes: core trade_log entries/exits, add-on legs (their own table, see
    signals_db.ensure_tables' addon_legs comment -- an add-on leg is deliberately NOT an
    open_positions/trade_log row), and resting-order placements. Also states the kernel side
    and a plain conclusion (no issue / MISMATCH / UNKNOWN) directly here, not just a bare
    pointer -- the old version ONLY pointed elsewhere ("see Part 3"), which was ambiguous
    since Part 2 has its own sub-item numbered "3" too (user's call, 2026-08-13). Part 3's
    live-vs-kernel section is still separately referenced for the deeper per-trade breakdown
    (matched/PHANTOM/MISSED) when there IS real activity to break down -- that's a genuine
    "more detail over there" pointer, not the sole source of the yes/no conclusion anymore."""
    print("\n--- 2. Today's real activity (trade_log + add-on legs + pending orders) ---")
    wl_ids = [n['id'] for n in nodes]
    if not wl_ids:
        print("no capital-at-stake nodes to report on")
        return
    ph = ",".join("?" * len(wl_ids))
    con = sqlite3.connect(LIVE_DB)
    con.row_factory = sqlite3.Row
    events = []

    for r in con.execute(
            f"SELECT * FROM trade_log WHERE wl_id IN ({ph}) AND is_dry_run_sim=0 "
            f"AND (entry_time LIKE ? OR exit_time LIKE ?)", wl_ids + [f"{TODAY}%", f"{TODAY}%"]):
        r = dict(r)
        if r['entry_time'] and r['entry_time'].startswith(TODAY):
            events.append((r['entry_time'], f"{r['ticker']:6s} {r['account'] or '':10s} "
                                            f"ENTRY {r['shares'] or 0:g}sh @ ${r['entry_price']:.4f} "
                                            f"({r['position_source']}, drift {r['entry_drift_pct']})"))
        if r['exit_time'] and r['exit_time'].startswith(TODAY):
            events.append((r['exit_time'], f"{r['ticker']:6s} {r['account'] or '':10s} "
                                           f"EXIT  {r['exit_reason']} @ ${r['exit_price']:.4f} "
                                           f"pnl {r['pnl_pct']:+.2f}% (drift {r['exit_drift_pct']})"))

    for r in con.execute(
            f"SELECT * FROM addon_legs WHERE wl_id IN ({ph}) AND is_dry_run_sim=0 "
            f"AND (entry_time LIKE ? OR exit_time LIKE ?)", wl_ids + [f"{TODAY}%", f"{TODAY}%"]):
        r = dict(r)
        if r['entry_time'] and r['entry_time'].startswith(TODAY):
            events.append((r['entry_time'], f"{r['ticker']:6s} {r['account'] or '':10s} "
                                            f"ADDON-LEG ENTRY {r['shares']:g}sh @ ${r['entry_price']} "
                                            f"(status={r['status']}/{r['entry_status']})"))
        if r['exit_time'] and r['exit_time'].startswith(TODAY):
            events.append((r['exit_time'], f"{r['ticker']:6s} {r['account'] or '':10s} "
                                           f"ADDON-LEG EXIT {r['exit_reason']} @ ${r['exit_price']} "
                                           f"pnl {r['pnl_pct']}"))

    for r in con.execute(
            f"SELECT * FROM pending_buys WHERE wl_id IN ({ph}) AND signal_time LIKE ?",
            wl_ids + [f"{TODAY}%"]):
        r = dict(r)
        events.append((r['signal_time'], f"{r['ticker']:6s} "
                                         f"PENDING BUY placed (order #{r['order_id']}, "
                                         f"signal ${r['signal_price']:.4f}, {r['position_source']})"))
    con.close()

    # kernel_skipped/kernel_failed are tracked separately, not silently folded into a "0
    # signals" count (found by review 2026-08-13: a node skipped as unsupported, or one whose
    # get_backtest_trades_in_window call raised, previously contributed 0 either way -- making
    # "checked, found nothing" and "never actually checked" print identically as "no issue").
    kernel_checkable, kernel_skipped = verify.resolve_nodes(wl_ids, min_notional=0)
    kernel_signal_count = 0
    kernel_failed = []
    for wl_id, knode in kernel_checkable.items():
        try:
            kernel_signal_count += len(verify.get_backtest_trades_in_window(knode, TODAY, TODAY))
        except Exception as e:
            kernel_failed.append((knode['ticker'], type(e).__name__))
    fully_checked = not kernel_skipped and not kernel_failed
    gap_note = ""
    if kernel_skipped or kernel_failed:
        parts = []
        if kernel_skipped:
            parts.append(f"{len(kernel_skipped)} node(s) skipped ({'; '.join(sorted(set(kernel_skipped.values())))})")
        if kernel_failed:
            parts.append(f"{len(kernel_failed)} node(s) failed ({', '.join(f'{t}: {e}' for t, e in kernel_failed)})")
        gap_note = f"  [KERNEL CHECK INCOMPLETE: {'; '.join(parts)}]"

    if not events:
        if kernel_signal_count == 0 and fully_checked:
            conclusion = "Conclusion: no issue -- both sides genuinely checked and agree, nothing happened."
        elif kernel_signal_count > 0:
            conclusion = "Conclusion: MISMATCH -- kernel predicted activity the real system never took, see Part 3."
        else:
            conclusion = "Conclusion: UNKNOWN -- kernel side not fully checked, cannot claim 'no issue'."
        print(f"Real side: none today. Kernel side: {kernel_signal_count} signal(s) today.{gap_note} " + conclusion)
        return
    print(f"Real side: {len(events)} event(s). Kernel side: {kernel_signal_count} signal(s) today"
          f"{gap_note} (see Part 3's 'live vs kernel' for the per-trade match).")
    for ts, line in sorted(events):
        print(f"  {ts}  {line}")


def part2():
    print(f"=== Part 2: real capital-at-stake nodes ({TODAY}) ===")
    nodes = real_capital_nodes()

    print(f"Scope: {len(nodes)} real capital-at-stake nodes")
    print("Not implemented: cash-movement metrics, execution-drift alert (both deferred, out of scope)\n")

    rows = []
    node_state = {}  # wl_id -> (status, real_position or None, current_price), reused by sub-part 5
    for n in nodes:
        try:
            sig = a.compute_buy_signal(n)
        except Exception as e:
            # cache/research/<ticker>_1h.csv is rewritten in place by the live daemon -- a read
            # here can transiently hit a half-written file. Report the node, don't drop the run.
            print(f"{n['ticker']:6s} {n['account'] or '':10s} signal NOT COMPUTED "
                  f"({type(e).__name__}: {e}) -- row omitted below")
            continue
        if sig is None:
            continue
        # Real broker price, not sig['current_price'] (yfinance, via compute_buy_signal's
        # ambient _live_tick_price) -- found by review 2026-08-13: the two sources can
        # disagree by several percent, and this section vs Part 4 (which already used the
        # broker price) printed OPPOSITE answers for whether SOXS's one real resting order
        # was above or below its fill trigger. The broker price is authoritative for a real
        # order question; fall back to sig's price only if the broker fetch fails.
        try:
            cur = schwab_client.get_current_price(n['ticker'])
        except Exception:
            cur = sig['current_price']
        state = db.get_real_position_state(n['id'])
        node_state[n['id']] = (state['status'], state['real_position'], cur)
        pending = state['pending_buy']
        if pending is not None:
            _, tb_trigger = a._trailing_buy_status(pending)
            trigger = tb_trigger if tb_trigger is not None else sig['lower_band']
            status = f"pending_entry, {pending['signal_time'][5:16]}"
            local_shares = 0.0
        elif state['status'] == 'holding':
            trigger = sig['lower_band']
            pos = state['real_position']
            local_shares = pos['shares']
            # Unrealized P&L: sub-parts 6/7 count CLOSED trades only (pnl_pct IS NOT NULL),
            # so without this a node sitting on a large open winner/loser reads as if nothing
            # ever happened. Same basis the live exit checks use: open_positions.entry_price.
            entry = pos['entry_price']
            unreal = (cur - entry) / entry * 100 if entry else None
            status = (f"holding, {local_shares:g}sh, entry ${entry:.4f}, "
                      f"unrealized {unreal:+.2f}%" if unreal is not None else
                      f"holding, {local_shares:g}sh, entry ${entry}")
        else:
            trigger = sig['lower_band']
            local_shares = 0.0
            status = "flat"
        pct = (cur - trigger) / trigger * 100

        # Sub-part 1: local DB state (open_positions AND pending orders) vs a fresh
        # (never cached) broker read of BOTH positions and orders -- checking share
        # count alone missed the order side entirely (a resting order could have been
        # cancelled/filled at the broker with local pending_buys never told).
        try:
            broker_shares = schwab_client.get_real_position(n['account'], n['ticker'])
            pos_check = f"{local_shares:g}/{broker_shares:g}"
        except Exception as e:
            pos_check = f"fetch failed ({e})"

        order_flag, order_detail = _broker_order_check(n, state, pending)

        mv = local_shares * cur if state['status'] == 'holding' else None
        rows.append((n['ticker'], n['account'], effective_notional(n, mv), trigger, cur, pct,
                     status, pos_check, order_flag, order_detail))
    rows.sort(key=lambda r: (ACCOUNT_ORDER.get(r[1], 99), r[0]))

    print("--- 1. State reconciliation (local DB vs real broker) ---")
    print(f"{'Ticker':<6} {'Acct':<10} {'Pos l/b':<8} {'Ord':<8} {'Notional':>9} {'EntryTrig':>10} "
          f"{'Current':>8} {'EntryDist%':>11}  State")
    for t, acct, notional, trig, cur, pct, status, pos_check, order_flag, order_detail in rows:
        print(f"{t:<6} {acct or '':<10} {pos_check:<8} {order_flag:<8} ${notional:>7,.0f} "
              f"{trig:>10.2f} {cur:>8.2f} {pct:>10.2f}%  {status}")
        if order_detail:  # only when there's something to act on
            print(f"{'':<6} !! {order_detail}")

    _part2_activity(nodes)

    accounts = sorted({n['account'] for n in nodes if n.get('account')}, key=lambda x: ACCOUNT_ORDER.get(x, 99))

    # Per-ticker holdings detail dropped for TRACKED tickers (2026-08-13, user's call) --
    # sub-part 1 already shows shares/entry/unrealized for the 10 capital-at-stake nodes.
    # Still checks for an UNTRACKED real holding (a ticker not among those 10) -- sub-part 1
    # can't surface that at all, since it only iterates the tracked node list, and
    # get_all_real_positions() queries the broker's actual full holdings, not our node list.
    tracked_tickers = {n['ticker'] for n in nodes}
    print("\n--- 3. Real portfolio state per account ---")
    print(f"{'Account':<10} {'Cash':>14} {'Holdings':>12} {'Total':>14} {'BuyPower':>14} {'Gap':>12}")
    for acct in accounts:
        try:
            cash = schwab_client.get_account_balance(acct)
            bp = schwab_client.get_account_buying_power(acct)
            holdings = schwab_client.get_all_real_positions(acct)
        except Exception as e:
            print(f"{acct:<10} fetch failed ({e})")
            continue
        holdings_value = 0.0
        untracked = []
        for t, q in sorted(holdings.items()):
            try:
                px = schwab_client.get_current_price(t)
                holdings_value += q * px
                if t not in tracked_tickers:
                    untracked.append(f"{t}:{q:g}sh @ ${px:,.2f}")
            except Exception:
                pass  # can't price it; still counted if untracked, just without a $ value
                if t not in tracked_tickers:
                    untracked.append(f"{t}:{q:g}sh (price fetch failed)")
        # gap = the exact number behind the real addon_buying_power_check alert on brokerage.
        gap = bp - cash
        print(f"{acct:<10} ${cash:>13,.2f} ${holdings_value:>11,.2f} ${cash + holdings_value:>13,.2f} "
              f"${bp:>13,.2f} ${gap:>11,.2f}")
        if untracked:
            print(f"           UNTRACKED real holding(s), not among the 10 capital-at-stake nodes: "
                  + "; ".join(untracked))

    # margin_req/leveraged_buying_power per ticker was mostly noise for non-margin accounts
    # (roth/ira: leveraged_buying_power == plain cash, nothing ticker-specific to show) --
    # replaced with per-account utilization. Per-ticker notional dropped (2026-08-13,
    # user's call) -- already visible in sub-part 1's table, redundant here.
    print("\n--- 4. Account utilization (sum of node notionals / buying power) ---")
    for acct in accounts:
        acct_notional = 0
        for n in nodes:
            if n['account'] != acct:
                continue
            ns = node_state.get(n['id'])
            mv = ns[1]['shares'] * ns[2] if ns and ns[0] == 'holding' else None
            acct_notional += effective_notional(n, mv)
        has_addon = any(n['account'] == acct and n.get('addon_enabled') for n in nodes)
        try:
            bp = schwab_client.get_account_buying_power(acct)
        except Exception as e:
            print(f"{acct:10s} fetch failed ({e})")
            continue
        util = acct_notional / bp * 100 if bp else float('inf')
        # The 50% headroom check only means something where add-on can double a position's
        # notional -- only brokerage's real nodes (ETHU/AGQ/JNUG) have addon_enabled among
        # roth/ira/brokerage; roth/ira nodes never add-on, so "watch at 50%" there is noise
        # (user's call, 2026-08-13). >100% is flagged everywhere -- nodes are sized against
        # real available capital, so this crossing at all means something is misconfigured,
        # not just "elevated."
        if util > 100:
            flag = "OVER-COMMITTED -- nodes sized beyond real capacity, investigate"
        elif has_addon:
            flag = "good" if util < 50 else "watch (add-on could double notional past capacity)"
        else:
            flag = "ok (no add-on exposure here)"
        print(f"{acct:10s} {acct_notional:>10,.0f} / {bp:>10,.0f}  =  {util:>5.1f}%  {flag}")

    # Underlying move != money made: real P&L is nonzero only where a position is actually open.
    print("\n--- 5. Realized and unrealized gains (today) ---")
    def _daily_pct(ticker):
        """Returns (pct, stale) -- stale=True means the cache's last daily bar isn't
        today, so this is a multi-day change silently mislabeled as 'today' unless
        flagged (found live 2026-08-13: TQQQ's cache was a day behind SPY/SOXL)."""
        _, df_daily = compute._load_cache(ticker)
        if df_daily is None or len(df_daily) < 2:
            return None, None
        last_bar_date = df_daily.index[-1].strftime('%Y-%m-%d')
        stale = last_bar_date != TODAY
        prev_close = float(df_daily['Close'].iloc[-1] if stale else df_daily['Close'].iloc[-2])
        try:
            cur = schwab_client.get_current_price(ticker)
        except Exception:
            cur = float(df_daily['Close'].iloc[-1])
        return (cur - prev_close) / prev_close * 100, stale

    spy_pct, spy_stale = _daily_pct('SPY')
    tqqq_pct, tqqq_stale = _daily_pct('TQQQ')
    spy_s = f"{spy_pct:+.2f}%" + ("  [STALE cache]" if spy_stale else "") if spy_pct is not None else "no data"
    tqqq_s = f"{tqqq_pct:+.2f}%" + ("  [STALE cache]" if tqqq_stale else "") if tqqq_pct is not None else "no data"
    print(f"(benchmarks: SPY {spy_s}, TQQQ {tqqq_s})")

    con = sqlite3.connect(LIVE_DB)
    con.row_factory = sqlite3.Row
    print(f"{'Ticker':6s} {'Realized today':>16s}  {'Unrealized':>28s}")
    for n in nodes:
        # fetchall, not fetchone (found by review 2026-08-13): a node can close MORE than
        # one core trade in a day -- exactly the same-bar re-entry shape (RETL, the real
        # incident this whole session started from) this report exists to catch. fetchone
        # silently reported only the first, understating "Realized today" on that exact case.
        realized_rows = con.execute(
            "SELECT entry_price, exit_price, shares, pnl_pct FROM trade_log "
            "WHERE wl_id=? AND is_dry_run_sim=0 AND position_source='core' AND exit_time LIKE ?",
            (n['id'], f"{TODAY}%")
        ).fetchall()
        if realized_rows:
            usd = sum((r['exit_price'] - r['entry_price']) * (r['shares'] or 0) for r in realized_rows)
            compounded = (float(np.prod([1 + r['pnl_pct'] / 100.0 for r in realized_rows])) - 1) * 100
            n_str = f" x{len(realized_rows)}" if len(realized_rows) > 1 else ""
            realized_s = f"{compounded:+.2f}% = ${usd:+,.2f}{n_str}"
        else:
            realized_s = "$0 (no close today)"

        status, pos, cur = node_state.get(n['id'], (None, None, None))
        if status == 'holding' and pos and pos.get('entry_price'):
            ep, sh = pos['entry_price'], (pos.get('shares') or 0)
            pnl_pct = (cur - ep) / ep * 100
            pnl_usd = (cur - ep) * sh
            unreal_s = f"{pnl_pct:+.2f}% = ${pnl_usd:+,.2f} ({sh:g}sh)"
        elif status == 'pending_entry':
            unreal_s = "$0 (order not filled)"
        else:
            unreal_s = "$0 (flat)"
        print(f"{n['ticker']:6s} {realized_s:>16s}  {unreal_s:>28s}")
    con.close()

    print("\n--- 6/7. Per-ticker and portfolio performance, real trade_log only ---")
    # '1yr' dropped -- was a literal duplicate of '12m' (both 365 days back), found by review.
    windows = {
        '3m': 90, '6m': 182, '12m': 365, 'YTD': (datetime.now() - datetime(datetime.now().year, 1, 1)).days,
        '2yr': 730, 'all-time': 10_000,
    }
    con = sqlite3.connect(LIVE_DB)
    con.row_factory = sqlite3.Row
    all_trades = {}  # keyed by wl_id, not ticker -- 2 real nodes could share a ticker across accounts
    for n in nodes:
        rows_t = con.execute(
            "SELECT exit_time, pnl_pct FROM trade_log WHERE wl_id=? AND is_dry_run_sim=0 "
            "AND position_source='core' AND exit_time IS NOT NULL AND pnl_pct IS NOT NULL",
            (n['id'],)
        ).fetchall()
        all_trades[n['id']] = [(datetime.strptime(r['exit_time'], '%Y-%m-%d %H:%M:%S'), r['pnl_pct']) for r in rows_t]
    con.close()

    def _compounded(trades, days_back):
        cutoff = datetime.now() - timedelta(days=days_back)
        rets = [pnl / 100.0 for ts, pnl in trades if ts >= cutoff]
        if not rets:
            return None
        return (float(np.prod([1 + r for r in rets])) - 1) * 100

    def _since_inception(trades, added_at):
        """Anchored to the node's real added_at, not a large days_back window --
        previously identical to 'all-time' under a different label (found by review)."""
        if not added_at:
            return None
        try:
            anchor = datetime.strptime(added_at[:19], '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return None
        rets = [pnl / 100.0 for ts, pnl in trades if ts >= anchor]
        if not rets:
            return None
        return (float(np.prod([1 + r for r in rets])) - 1) * 100

    header = f"{'Ticker':6s} " + " ".join(f"{w:>9s}" for w in windows) + f" {'inception':>10s}"
    print(header)
    per_node_results = []  # (notional, {window: pct or None}, since_incept)
    for n in nodes:
        trades = all_trades.get(n['id'], [])
        cells, results = [], {}
        for w, days in windows.items():
            r = _compounded(trades, days)
            results[w] = r
            cells.append(f"{r:+8.2f}%" if r is not None else f"{'-':>9s}")
        since_incept = _since_inception(trades, n.get('added_at'))
        incept_s = f"{since_incept:+.2f}%" if since_incept is not None else "-"
        print(f"{n['ticker']:6s} " + " ".join(cells) + f" {incept_s:>10s}")
        ns = node_state.get(n['id'])
        mv = ns[1]['shares'] * ns[2] if ns and ns[0] == 'holding' else None
        per_node_results.append((effective_notional(n, mv), results, since_incept))

    # Notional-weighted average of each ticker's OWN compounded return per window -- not a
    # chained product of unrelated trades across 3 separate accounts (the prior version's
    # bug, flagged by review: that treats simultaneous positions as sequential capital use).
    print("\nPortfolio (notional-weighted average across real capital-at-stake tickers):")
    cells = []
    for w in windows:
        weighted = [(notional, res[w]) for notional, res, _ in per_node_results if res[w] is not None]
        if not weighted:
            cells.append(f"{'-':>9s}")
            continue
        total_notional = sum(notional for notional, _ in weighted)
        avg = sum(notional * pct for notional, pct in weighted) / total_notional if total_notional else 0
        cells.append(f"{avg:+8.2f}%")
    incept_weighted = [(notional, si) for notional, _, si in per_node_results if si is not None]
    if incept_weighted:
        total_notional = sum(notional for notional, _ in incept_weighted)
        incept_avg = sum(notional * si for notional, si in incept_weighted) / total_notional if total_notional else 0
        incept_s = f"{incept_avg:+.2f}%"
    else:
        incept_s = "-"
    print(f"{'ALL':6s} " + " ".join(cells) + f" {incept_s:>10s}")


# verify_live_parity.replay() drives compute_buy_signal once per bar and opens the position on
# that same bar. It has no equivalent of the real multi-bar resting trailing-buy "wait for the
# bounce above the running low" state machine, so these two strategies cannot be replayed by
# that harness at all -- see its module docstring. Not a config choice here, a structural gap.
PARITY_UNSUPPORTED = {'TrailingBuyZScoreBreakout', 'TrailingBothZScoreBreakout'}


def _deep_live_parity():
    """Plan Part 3 sub-part 3 -- live CODE vs kernel, which is a different question from the
    outcome-vs-kernel check above: it replays active_signals.py's own compute_buy_signal/
    check_sell_condition bar-by-bar against the numba kernel, so it catches silent drift
    between the two codebases even on days with no real trades at all."""
    print("\n--- 4. Live CODE vs kernel (scripts/verify_live_parity.py bar-by-bar replay) ---")
    if '--skip-deep-parity' in sys.argv:
        print("NOT CHECKED this run (--skip-deep-parity passed)")
        return

    nodes = real_capital_nodes()
    supported = [n for n in nodes if n['strategy'] not in PARITY_UNSUPPORTED]
    unsupported = [n for n in nodes if n['strategy'] in PARITY_UNSUPPORTED]

    if unsupported:
        print(f"NOT CHECKED, {len(unsupported)}/{len(nodes)} nodes "
              f"({', '.join(sorted({n['ticker'] for n in unsupported}))}): "
              f"{'/'.join(sorted({n['strategy'] for n in unsupported}))} has no replay path "
              f"(no resting trailing-buy state machine)")
    if not supported:
        print("0 nodes checkable -- live-code-vs-kernel parity NOT verified this run")
        return

    # The "expected look-ahead bias" framing was retired 2026-08-14 -- that bias was fixed
    # in the kernel back on 2026-07-03 (backtester.prep_inputs), and AGQ/NUGT's mismatches
    # here traced to a real bug in THIS harness (replay() never passed open_price=,
    # silently defeating the gap-through-trigger fill logic), now fixed. AGQ reports a
    # clean MATCH; any mismatch reported below is real and worth investigating, not noise
    # to filter by "did the index move."
    print("(a MATCH here is the expected outcome now -- a mismatch is real, not noise; "
          "investigate it directly)")
    for n in supported:
        uses_fixed = strategies.uses_fixed_sl(n['strategy'])
        sl = (n.get('fixed_sl') if uses_fixed else n.get('stop_loss')) or 0
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                kt = parity.kernel_trades(n['ticker'], n['strategy'], n['window'],
                                          n['z_score_threshold'], n['take_profit'], sl,
                                          n['max_hold_hours'], trail_pct=n.get('trail_sell_pct'))
                rt = parity.replay(n['ticker'], n['strategy'], n['window'],
                                   n['z_score_threshold'], n['take_profit'], sl,
                                   n['max_hold_hours'], trail_pct=n.get('trail_sell_pct'))
        except Exception as e:
            print(f"  {n['ticker']:6s} wl_id={n['id']:4d}  NOT CHECKED -- replay failed ({e})")
            continue
        closed = ('WIN', 'LOSS', 'TWIN', 'TLOSS')
        kt = [t for t in kt if t['Result'] in closed]
        rt = [t for t in rt if t['Result'] in closed]
        first = None
        for i in range(min(len(kt), len(rt))):
            k, r = kt[i], rt[i]
            if (k['Entry Time'] != r['Entry Time'] or k['Exit Time'] != r['Exit Time']
                    or k['Result'] != r['Result'] or abs(k['Return'] - r['Return']) > 1e-6):
                first = (i, k, r)
                break
        if first is None and len(kt) == len(rt):
            print(f"  {n['ticker']:6s} MATCH, {len(kt)} trades identical")
        elif first is None:
            print(f"  {n['ticker']:6s} count differs (kernel {len(kt)} vs replay {len(rt)}), "
                  f"first {min(len(kt), len(rt))} identical")
        else:
            i, k, _ = first
            print(f"  {n['ticker']:6s} first mismatch #{i} entry {k['Entry Time']:%Y-%m-%d}, "
                  f"kernel {len(kt)}/replay {len(rt)} trades")


def part3():
    print(f"=== Part 3: coverage trend, paper vs kernel, live vs kernel ({TODAY}) ===")
    nodes_all_capital = real_capital_nodes()

    print("--- 1. Coverage/Grid trend (vs last logged run) ---")
    today_rows = {r['id']: compute_status(r) for r in REGISTRY}

    # Today's own state, independent of whether a prior baseline exists to diff against --
    # user's call, 2026-08-13: "even if it can't do a regression, it should say what today's
    # coverage was." Split DAILY (fires routinely -- collapse to one rollup line, nobody
    # needs to re-read the same 40-50 rows every night) from EDGE CASES (infrequent -- these
    # are what's actually worth a human's attention, per the user's explicit framing:
    # "honestly i don't care about the stuff firing every day, i want to know about the
    # edge cases"). Classification is real, not guessed: a coverage_events-mechanism row is
    # "daily" if it fired on >=7 of the last 14 calendar days; a scenario_expectations-
    # mechanism row uses its own real expected_frequency column ('daily'/'informational').
    con = sqlite3.connect(LIVE_DB)
    event_days = dict(con.execute(
        "SELECT scenario_key, COUNT(DISTINCT date(ts)) FROM coverage_events "
        "WHERE ts >= date('now', '-14 days') GROUP BY scenario_key"
    ).fetchall())
    # Scoped to exactly TODAY (localtime, matching coverage_check.py's own UTC-vs-local
    # fix) -- event_days above only tells us this scenario fires daily IN GENERAL, not
    # that it actually fired today specifically. compute_status()/proof_classify() are
    # both ALL-TIME status, so "not currently red" was being read as "confirmed repeated
    # today" even when today itself was silent (found by Opus review, 2026-08-14).
    today_events = {r[0] for r in con.execute(
        "SELECT DISTINCT scenario_key FROM coverage_events WHERE date(ts, 'localtime') = ?", (TODAY,)
    ).fetchall()}
    scenario_freq = dict(con.execute(
        "SELECT scenario_key, expected_frequency FROM scenario_expectations WHERE active=1"
    ).fetchall())
    con.close()

    # Moved up from below (was computed after this loop) so a scenario_expectations daily
    # row can be judged against TODAY's own check_date specifically, not compute_status's
    # all-time 'deviation-unexplained' status (same staleness bug as coverage_events above).
    unexplained_by_key = {}
    for d in db.get_deviations(unexplained_only=True):
        unexplained_by_key.setdefault(d['scenario_key'], []).append(d)

    daily_rows, snoozed_rows, accepted_rows, edge_rows = [], [], [], []
    for row in REGISTRY:
        tier, status, detail, _gap = proof_classify(row)
        red = STATUS_ORDER.get(status, 99) <= 1.5
        sk = row['scenario_key']
        is_daily = (scenario_freq.get(sk) == 'daily') or (event_days.get(sk, 0) >= 7)
        if is_daily:
            # A not-prod-required row is an accepted/demoted status regardless of firing
            # cadence -- "did it fire today" doesn't apply to it (found live 2026-08-14:
            # position_lock, hasn't fired since market hasn't opened yet, was printing as
            # "NOT repeating" despite being a deliberate accepted status, not a gap).
            # Otherwise: red if the all-time status was already bad (unchanged from
            # before), OR if it's a currently-good status that simply hasn't happened yet
            # today (the actual gap this fix closes).
            if status == 'not-prod-required':
                red_today = False
            elif row['check_mechanism'] == 'coverage_events':
                red_today = red or (sk not in today_events)
            else:
                red_today = red or any(d['check_date'] == TODAY for d in unexplained_by_key.get(sk, []))
            daily_rows.append((row['id'], tier, status, red_today, detail, sk))
            continue
        # not-prod-required: a deliberate, already-made decision (cost of forcing this
        # scenario to a higher proof tier exceeds the value) -- not a gap, must not sit
        # invisibly inside the SIMULATOR tier's red/total counts (user's call, 2026-08-14,
        # after asking "34 total 27 red, where are the other 7" -- they were here, uncounted).
        if status == 'not-prod-required':
            accepted_rows.append((row['id'], tier))
            continue
        # Real code-change-aware snooze (plan's actual sub-part 1 ask, user's call
        # 2026-08-13: "live coverage needs to be snoozed too -- don't need to prove
        # top_up every day"). A LIVE/CANARY-tier row that's already proven AND whose
        # code hasn't changed since that proof (staleness_for, reused from
        # coverage_regression_watch.py) doesn't need re-reading every night.
        stale_result = staleness_for(row, status, detail) if not red and tier in ('LIVE', 'CANARY') else None
        is_stale = stale_result[0] if stale_result else False
        if tier in ('LIVE', 'CANARY') and not red and not is_stale:
            snoozed_rows.append((row['id'], tier, status))
        else:
            fb = fake_venue_proof_for(row['scenario_key'])[0] == 'event-asserted'
            edge_rows.append((row['id'], tier, status, red, is_stale, fb))

    print(f"Accountability Grid: {len(REGISTRY)} total scenarios "
          f"({len(daily_rows)} daily, {len(snoozed_rows)} snoozed, "
          f"{len(accepted_rows)} accepted, {len(edge_rows)} edge cases)")

    daily_red = sum(1 for _, _, _, red, _detail, _sk in daily_rows if red)
    print(f"Daily-firing scenarios: {len(daily_rows) - daily_red}/{len(daily_rows)} confirmed repeated today"
          + (f" ({daily_red} NOT repeating, see below)" if daily_red else ""))
    for rid, tier, status, red, detail, sk in sorted(daily_rows):
        if not red:
            continue
        print(f"  {rid:38s} {tier:10s} {status}: {detail}")
        for d in unexplained_by_key.get(sk, [])[:3]:
            print(f"      {d['check_date']} {d.get('ticker') or '':6s} {d['actual_summary']}")

    print(f"\nSnoozed (already proven live/canary, code unchanged since -- {len(snoozed_rows)} total, "
          f"not re-listed nightly)")
    print(f"Accepted (not-prod-required -- deliberate decision, not a gap -- {len(accepted_rows)} total)")

    print(f"\nEdge cases needing actual attention -- {len(edge_rows)} total:")
    print(f"{'Tier':<12} {'Total':>6} {'Red':>5} {'Stale':>6} {'FakeBroker':>11}")
    tier_counts, tier_red, tier_stale, tier_fb = {}, {}, {}, {}
    for _rid, tier, status, red, stale, fb in edge_rows:
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        if red:
            tier_red[tier] = tier_red.get(tier, 0) + 1
        if stale:
            tier_stale[tier] = tier_stale.get(tier, 0) + 1
        if fb:
            tier_fb[tier] = tier_fb.get(tier, 0) + 1
    for tier in ('LIVE', 'CANARY', 'PAPER', 'SIMULATOR', 'UNIT-TEST', 'NONE', 'N/A'):
        n = tier_counts.get(tier, 0)
        if n:
            print(f"{tier:<12} {n:>6} {tier_red.get(tier, 0):>5} {tier_stale.get(tier, 0):>6} {tier_fb.get(tier, 0):>11}")
    total_edge_red = sum(tier_red.values())
    total_stale = sum(tier_stale.values())
    if total_stale:
        print(f"{total_stale} row(s) have LIVE/CANARY proof that PREDATES a code change -- re-verify, don't trust as-is:")
        for rid, tier, status, red, stale, fb in sorted(edge_rows):
            if stale:
                print(f"  {rid:38s} {tier:10s} {status}")
    if total_edge_red:
        # 'NONE' tier is nearly always empty in practice -- a red row almost always
        # already has SOME proof (SIMULATOR/UNIT-TEST), just not enough to clear this
        # bucket. Pointing at a fixed, usually-empty tier here was a real bug (found
        # 2026-08-14): list the tiers that actually have red rows, from the table
        # just printed above, instead of a hardcoded guess.
        red_tiers = ' '.join(t for t in ('LIVE', 'CANARY', 'PAPER', 'SIMULATOR', 'UNIT-TEST', 'NONE', 'N/A')
                              if tier_red.get(t))
        print(f"{total_edge_red} edge-case row(s) currently red -- run "
              f"`scripts/coverage_proof_matrix.py --tier <TIER>` for each of: {red_tiers}")

    # --- 1b. Stream fast-path parse health ---
    # Added 2026-08-15, same session as the _parse_activity_message shape fix -- the
    # fast path had 0 successful parses for 13 days (2026-08-02 to 2026-08-15) with the
    # only evidence being a manual grep of logs/active_signals.log. That's the actual
    # gap this section closes: a real, computed metric instead of something only
    # discoverable by hand. Window matches the 14-day lookback the daily-vs-edge-case
    # classification above already uses, for consistency.
    #
    # Wired into the SAME coverage_run_snapshot/regression mechanism as the Grid rows
    # below, not left as a bare print -- a print-only metric has exactly zero protection
    # against silently regressing back to 0% and nobody noticing, which is the precise
    # failure shape that let the parser sit dead for 13 days in the first place (found
    # live, 2026-08-15, when asked "checklist for coverage and evening report?" after
    # this section first shipped as print-only). Reuses coverage_registry.STATUS_ORDER's
    # existing scale (100% -> 'verified-live', a real live degradation -> 'live-attempt-
    # failed', nothing observed -> 'wired-never-fired') so it participates in the exact
    # same "N regressed since run #X" comparison below with zero new logic.
    con2 = sqlite3.connect(LIVE_DB)
    parse_rows = con2.execute(
        "SELECT result, COUNT(*) FROM coverage_events WHERE scenario_key='stream_message_parsed' "
        "AND ts >= date('now', '-14 days') GROUP BY result"
    ).fetchall()
    con2.close()
    parse_counts = dict(parse_rows)
    parsed = parse_counts.get('parsed', 0)
    failed = parse_counts.get('missing_field', 0) + parse_counts.get('exception', 0)
    total = parsed + failed
    print(f"\n--- 1b. Stream fast-path parse health (last 14 days) ---")
    if total == 0:
        print("no OrderFillCompleted stream messages seen in this window -- can't compute a rate "
              "(not itself an error: a quiet 14 days with no real fills is possible)")
        parse_status = 'wired-never-fired'
        parse_detail = 'no OrderFillCompleted stream messages in the last 14 days'
    else:
        rate = parsed / total * 100
        flag = "" if rate == 100 else "  <-- was silently 0% for 13 days before the 2026-08-15 fix, watch this"
        print(f"{parsed}/{total} ({rate:.0f}%) real fill messages parsed successfully{flag}")
        parse_status = 'verified-live' if rate == 100 else 'live-attempt-failed'
        parse_detail = f"{parsed}/{total} ({rate:.0f}%) parsed"
    today_rows['stream_parse_health'] = (parse_status, parse_detail)

    prior_run_id, prior_ts, prior_statuses = last_run_statuses()
    if prior_run_id is None:
        print("no prior baseline -- this run establishes the first one")
    else:
        regressed = []
        for k, (status, _detail) in today_rows.items():
            old = prior_statuses.get(k)
            if old is not None and old != status and STATUS_ORDER.get(status, 99) < STATUS_ORDER.get(old, 99):
                regressed.append((k, old, status))
        print(f"{len(regressed)} regressed since run #{prior_run_id} ({prior_ts})")
        for k, old, new in sorted(regressed):
            print(f"  {k:38s} {old} -> {new}")
    # Logs today's own snapshot as the new baseline for next time -- previously Part 3 only
    # ever READ the last logged run, never wrote one, so the first (or any post-gap) run
    # always dead-ended into "go run scripts/coverage_regression_watch.py yourself" instead
    # of just doing it (user's call, 2026-08-13 -- this is exactly the fragmentation this
    # whole evening_status.py script was built to get away from).
    # Only ONE baseline per calendar day -- watch_evening_status.sh runs this every 60s by
    # default; logging unconditionally would flood coverage_run_snapshot (~50 rows/minute)
    # and make "regressed since run #N" always diff against a snapshot from a minute ago
    # instead of the real day-over-day signal this section exists for (found by Opus
    # review, 2026-08-14).
    if prior_ts and prior_ts[:10] == TODAY:
        print(f"already logged today as run #{prior_run_id} ({prior_ts}) -- not re-logging")
    else:
        git_commit, _dirty = _git_state()
        run_id = log_run(git_commit, today_rows)
        print(f"logged as run #{run_id} (commit={git_commit or '?'})")

    # A TODAY..TODAY window compares essentially nothing -- most nodes don't trade on any
    # given day, so nearly every node short-circuits on "no activity either side" and the
    # old "of N checked" count counted those skips as if they'd been compared.
    paper_start = (datetime.now() - timedelta(days=PAPER_WINDOW_DAYS)).strftime('%Y-%m-%d')
    active_wl = db.get_active_watchlist_id()
    print(f"\n--- 2. Paper vs kernel (live-track nodes, watchlist {active_wl}, {paper_start}..{TODAY}) ---")
    paper_nodes = resolve_live_track_nodes_by_activity(active_wl)
    # Two passes -- collect everything first so the summary line can lead (user's call,
    # 2026-08-14: had to read every printed row before knowing the totals; a scan-only
    # tool's most useful line is the one you'd otherwise compute by hand at the end).
    results = []
    for node in paper_nodes:
        try:
            paper = get_paper_trades(node['id'], paper_start, TODAY)
            bt = verify.get_backtest_trades_in_window(node, paper_start, TODAY)
        except Exception as e:
            # cache/research/<ticker>_1h.csv is rewritten in place by the live daemon, so a
            # read here can transiently hit a half-written file -- don't fail the whole report.
            results.append((node, None, None, 'error', type(e).__name__))
            continue
        if len(paper) == 0 and len(bt) == 0:
            results.append((node, 0, 0, 'quiet', None))
            continue
        # resolve_live_track_nodes_by_activity's own fallback (see its docstring) returns
        # the lowest-id node for a ticker with NO real paper_trade_log/paper_positions/
        # paper_pending_buys activity at all -- for a ticker only run as dry_run/canary/
        # live, that's structurally never going to have paper rows, so paper=0 here is
        # not evidence of anything. Comparing it against real kernel activity produced a
        # false MISMATCH for FAZ/JDST/QID (2026-08-14, user caught it) -- only a genuine
        # state='paper' node can actually diverge from what paper trading should have done.
        if node.get('state') != 'paper':
            results.append((node, len(paper), len(bt), 'not-paper', None))
            continue
        mismatch = abs(len(paper) - len(bt)) > 2
        results.append((node, len(paper), len(bt), 'MISMATCH' if mismatch else 'ok', None))

    quiet = sum(1 for r in results if r[3] == 'quiet')
    not_paper = sum(1 for r in results if r[3] == 'not-paper')
    errored = sum(1 for r in results if r[3] == 'error')
    flagged = sum(1 for r in results if r[3] == 'MISMATCH')
    matched = sum(1 for r in results if r[3] == 'ok')
    print(f"{len(paper_nodes)} total: {quiet} no activity either side, {not_paper} not a paper node "
          f"(dry_run/canary/live -- not comparable here), {matched} matched, {flagged} issues"
          + (f", {errored} errored" if errored else ""))
    for node, paper_n, kernel_n, tag, err in results:
        if tag == 'MISMATCH':
            print(f"  {node['ticker']:6s} wl_id={node['id']:4d}  paper={paper_n} kernel={kernel_n}  MISMATCH")
        elif tag == 'error':
            print(f"  {node['ticker']:6s} wl_id={node['id']:4d}  NOT CHECKED ({err})")

    print(f"\n--- 3. Live vs kernel (all real capital-at-stake nodes today, not just ones that traded) ---")
    # Previously scoped to wl_ids derived from TODAY's real trades only -- a node with ZERO
    # real activity today never got checked against the kernel at all, so a real missed
    # signal (kernel predicted, daemon never acted) on an otherwise-quiet node was invisible.
    # Found live 2026-08-13: today happened to be genuinely quiet for all 10 nodes (kernel
    # confirmed 0 signals via direct check), but the report itself never verified that --
    # now it does, every run, not just when someone thinks to check by hand.
    real = verify.get_real_trades(TODAY, TODAY, accounts=None)
    real = [r for r in real if r['wl_id'] and r['wl_id'] > 0]
    all_wl_ids = sorted({n['id'] for n in nodes_all_capital} | {r['wl_id'] for r in real})
    nodes, skipped = verify.resolve_nodes(all_wl_ids, min_notional=5000)
    out_of_scope = {k: v for k, v in skipped.items() if v.startswith("starting_notional below")}
    unchecked = {k: v for k, v in skipped.items() if k not in out_of_scope}
    quiet_count, active_count = 0, 0
    for wl_id, node in nodes.items():
        node_real = [r for r in real if r['wl_id'] == wl_id]
        try:
            bt = verify.get_backtest_trades_in_window(node, TODAY, TODAY)
        except Exception as e:
            print(f"  {node['ticker']:6s} wl_id={wl_id:4d}  NOT CHECKED ({type(e).__name__})")
            continue
        if not node_real and not bt:
            quiet_count += 1
            continue
        active_count += 1
        matched, unmatched_real, unmatched_bt = verify.match_trades(
            [{"signal_time": r["signal_time"], "entry_time": r["entry_time"],
              "ticker": r["ticker"], "exit_reason": r["exit_reason"]} for r in node_real],
            [{"entry_time": str(t["entry_time"])} for t in bt], 4)
        genuine = [r for r in unmatched_real if not verify.is_staged_or_manual(r['ticker'], r['entry_time'], r['exit_reason'])]
        # Bidirectional: a kernel trade the real daemon NEVER TOOK is exactly as much of a
        # "trades must equal backtest" violation as a real trade the kernel never predicted.
        flags = []
        if genuine:
            flags.append(f"{len(genuine)} PHANTOM real trade(s) the kernel never predicted")
        if unmatched_bt:
            flags.append(f"{len(unmatched_bt)} MISSED kernel trade(s) the daemon never took")
        flag = "; ".join(flags) if flags else f"matches kernel ({len(matched)} matched)"
        print(f"  {node['ticker']:6s} {node['account'] or '':10s} wl_id={wl_id:4d}  {flag}")
        for r in genuine:
            print(f"      PHANTOM: entry={r['entry_time']} exit_reason={r['exit_reason']}")
        for t in unmatched_bt:
            print(f"      MISSED : kernel entry={t['entry_time']}")
    for wl_id, reason in sorted(unchecked.items()):
        print(f"  wl_id={wl_id:4d}  UNCHECKED -- {reason}")
    print(f"{active_count} node(s) had activity to compare, {quiet_count} confirmed quiet on BOTH "
          f"real and kernel sides (checked, not assumed)")

    _deep_live_parity()

    devs = [d for d in db.get_deviations(unexplained_only=True) if d.get('check_date') == TODAY]
    print(f"\n{len(devs)} unexplained coverage_deviation(s) today")
    for d in devs:
        print(f"  {d['scenario_key']:28s} {d['ticker'] or '':6s} {d['actual_summary']}")


def _token_reauth_status():
    """Schwab's refresh token is a hard 7-day cap from the last INTERACTIVE browser login --
    not a sliding window (schwab_auth.py's docstring). schwab-py stores that moment as
    creation_timestamp and deliberately leaves it unchanged on every silent refresh
    (schwab.auth.TokenMetadata), so it's the real "when must a human log in again" clock."""
    if not TOKEN_PATH.exists():
        print(f"\ntoken: MISSING at {TOKEN_PATH} -- interactive reauth required before open")
        return
    try:
        blob = json.loads(TOKEN_PATH.read_text())
        created = float(blob['creation_timestamp'])
    except Exception as e:
        print(f"\ntoken: unreadable ({e}) -- treat as reauth required")
        return
    expires = created + REFRESH_TOKEN_DAYS * 86400
    days_left = (expires - time_mod.time()) / 86400
    if days_left <= 0:
        verdict = "EXPIRED, REAUTH NOW"
    elif days_left <= REAUTH_WARN_DAYS:
        verdict = f"REAUTH DUE SOON ({days_left:.1f}d)"
    else:
        verdict = f"ok ({days_left:.1f}d)"
    print(f"\ntoken: reauth by {datetime.fromtimestamp(expires):%a %Y-%m-%d %H:%M} [{verdict}]")


def _next_triggers():
    """Plan Part 4 points 1 and 2 -- for each real open position its ACTUAL next exit trigger,
    and for each flat node its distance to a fresh entry signal. The exit-trigger math is
    derived the same way the live exit path derives it (strategies.uses_fixed_sl +
    db._tp_or_arm_pct + entry_price/trail peak), not re-guessed here, so it can't drift from
    what check_sell_condition will actually do."""
    print("\n--- 2. Next real triggers ---")
    nodes = real_capital_nodes()
    for n in nodes:
        state = db.get_real_position_state(n['id'])
        try:
            cur = schwab_client.get_current_price(n['ticker'])
        except Exception as e:
            print(f"{n['ticker']:6s} {n['account'] or '':10s} price fetch failed ({e})")
            continue

        if state['status'] == 'holding':
            pos = state['real_position']
            ep = pos['entry_price']
            sl_pct = (pos.get('fixed_sl') if strategies.uses_fixed_sl(pos['strategy'])
                      else pos.get('stop_loss')) or 0.0
            stop_price = ep * (1 - sl_pct / 100.0)
            arm_pct = db._tp_or_arm_pct(pos)
            arm_price = ep * (1 + (arm_pct or 0) / 100.0)
            trail_state = pos.get('trail_state') or {}
            if trail_state.get('trailing'):
                peak = trail_state.get('peak', ep)
                trail_stop = peak * (1 - (pos.get('trail_sell_pct') or 3.0) / 100.0)
                second = f"ARMED trail ${trail_stop:.2f} ({(cur - trail_stop) / cur * 100:+.2f}%)"
            else:
                second = f"arm ${arm_price:.2f} ({(arm_price - cur) / cur * 100:+.2f}%)"
            print(f"{n['ticker']:6s} {n['account'] or '':10s} HOLD {pos['shares']:g}sh @${ep:.2f} "
                  f"cur ${cur:.2f} ({(cur - ep) / ep * 100:+.2f}%) | SL ${stop_price:.2f} "
                  f"({(cur - stop_price) / cur * 100:+.2f}%) | {second} | "
                  f"TIME {pos['max_hold_hours']}h from {pos['signal_time'][:16]}")
            continue

        try:
            sig = a.compute_buy_signal(n)
        except Exception as e:
            # The live daemon rewrites cache/research/<t>_1h.csv in place, so a read here can
            # transiently land on a half-written/empty file. One node's bad read must not take
            # down the whole readiness report.
            print(f"{n['ticker']:6s} {n['account'] or '':10s} signal NOT COMPUTED ({type(e).__name__}: {e})")
            continue
        if sig is None:
            print(f"{n['ticker']:6s} {n['account'] or '':10s} no signal computed")
            continue
        pending = state['pending_buy']
        if pending is not None:
            _, tb = a._trailing_buy_status(pending)
            trig = tb if tb is not None else sig['lower_band']
            print(f"{n['ticker']:6s} {n['account'] or '':10s} PENDING fills ${trig:.2f}, "
                  f"cur ${cur:.2f} ({(cur - trig) / trig * 100:+.2f}%)")
        else:
            lb = sig['lower_band']
            print(f"{n['ticker']:6s} {n['account'] or '':10s} flat, needs "
                  f"{(lb - cur) / cur * 100:+.2f}% to entry z-band ${lb:.2f} (cur ${cur:.2f})")


def part4():
    print("=== Part 4: readiness for tomorrow ===")
    print("--- 1. Operational readiness (daemon, token) ---")
    pid = daemon_status._find_daemon_pid()
    if not pid:
        print("daemon: NOT RUNNING")
    else:
        start_epoch = int(subprocess.run(["stat", "-c", "%Y", f"/proc/{pid}"], capture_output=True, text=True).stdout.strip())
        newest_mtime = max((Path(f).stat().st_mtime for f in daemon_status.LIVE_SOURCE_FILES if Path(f).exists()), default=0)
        stale = newest_mtime > start_epoch
        print(f"daemon: RUNNING (pid {pid}), {'STALE -- restart to pick up code changes' if stale else 'current'}")

    _token_reauth_status()
    _next_triggers()

    incidents = [i for i in db.get_incidents() if i.get('resolved_ts') is None]
    print(f"\n{len(incidents)} open trading_incident(s)")
    for i in incidents:
        print(f"  #{i['id']} {i['ts']} — {i.get('title', '')}")


PARTS = {'1': part1, '2': part2, '3': part3, '4': part4}


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if arg == 'all':
        for p in PARTS.values():
            p()
            print()
    elif arg in PARTS:
        PARTS[arg]()
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
