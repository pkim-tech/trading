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
from concurrent.futures import ProcessPoolExecutor, as_completed
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
import signals_invariants
import scripts.verify_real_trades_vs_kernel as verify
from scripts.coverage_registry import REGISTRY, compute_status, STATUS_ORDER
from scripts.coverage_regression_watch import last_run_statuses, log_run, staleness_for
from scripts.capital_scaling_gate import _git_state
from scripts.coverage_proof_matrix import classify as proof_classify
from scripts.coverage_registry import fake_broker_proof_for
from scripts.paper_vs_backtest_reconcile import resolve_live_track_nodes_by_activity, get_paper_trades
import scripts.daemon_status as daemon_status
import scripts.verify_live_parity as parity
import k1_tax

TODAY = datetime.now().strftime('%Y-%m-%d')
ACCOUNT_ORDER = {'brokerage': 0, 'roth': 1, 'ira': 2, 'soxl_ira': 3}
LIVE_DB = "cache/live/trading_live.db"
DEEP_PARITY_CACHE_PATH = Path("cache/live/deep_parity_cache.json")
TOKEN_PATH = Path("cache/live/schwab_token.json")
# Schwab's refresh token is a hard 7-day cap from initial interactive login (schwab_auth.py's
# docstring); schwab-py's TokenMetadata.creation_timestamp deliberately does NOT move on a
# silent refresh, so it really is "when the human last logged in."
REFRESH_TOKEN_DAYS = 7
REAUTH_WARN_DAYS = 2
# Part 3 sub-part 2: a TODAY..TODAY window compares almost nothing (most nodes don't trade
# every day), so a trailing window is what actually gives this check something to compare.
PAPER_WINDOW_DAYS = 7
# Part 3 sub-part 4 (2026-08-15): real compounded-return divergence needs more trades to be
# meaningful than the trade-COUNT check above -- this project's real live footprint is small
# (1-5 real trades/month/node, confirmed via scripts/verify_real_trades_vs_kernel.py's actual
# output), so PAPER_WINDOW_DAYS=7 would almost always show 0-1 trades, too thin to compute a
# real compounded-return comparison. 30 days trailing is deliberately wider for this reason.
DIVERGENCE_WINDOW_DAYS = 30
# Provisional (2026-08-15) -- calibrated off the real spread observed the day this was built:
# SOXL -0.28pp, RETL +1.12pp, LABD +2.76pp (real BETTER), YINN -2.49pp, SOXS -0.01pp (all real
# minus backtest, negative = real worse). 10pp comfortably clears that whole real spread without
# firing on any of it -- revisit once more real divergence data accumulates.
DIVERGENCE_THRESHOLD_PP = 10.0
DIVERGENCE_MIN_TRADES = 2


def compute_divergence(real_rets, bt_rets):
    """Pure compounded-return comparison, split out for testability. Not a
    loss-streak circuit breaker (deliberately rejected 2026-08-15 -- the
    backtest's own validated returns depend on the strategy staying in the
    market through loss streaks, so a raw consecutive-loss counter would
    fight the exact behavior the backtest proved out). The real signal worth
    catching is DIVERGENCE: real compounded return meaningfully worse than
    what a kernel replay of the SAME period says should be happening -- a
    real loss in line with backtest is expected and should be absorbed, not
    flagged. Reuses scripts/verify_real_trades_vs_kernel.py's own
    real_compounded_pct/backtest_compounded_pct computation (already
    correct, just never wired into an automated report before now).

    Returns (real_comp_pct, bt_comp_pct, delta_pp) or None if either side has
    fewer than DIVERGENCE_MIN_TRADES. delta_pp = real - bt; negative means
    real did worse than the kernel replay predicted for the same period."""
    if len(real_rets) < DIVERGENCE_MIN_TRADES or len(bt_rets) < DIVERGENCE_MIN_TRADES:
        return None
    real_comp = (float(np.prod([1 + r for r in real_rets])) - 1) * 100
    bt_comp = (float(np.prod([1 + r for r in bt_rets])) - 1) * 100
    return real_comp, bt_comp, real_comp - bt_comp


def event_days_by_scenario(con):
    """{scenario_key: distinct ET calendar days it fired on, over the trailing
    ~14 days} -- feeds part3's daily-vs-edge-case classification (>=7 of 14 =
    "daily"). Split out for testability, same rationale as compute_divergence
    above.

    coverage_events.ts is stored UTC (SQLite's datetime('now') default) -- the
    per-day grouping uses date(ts, 'localtime') (system TZ confirmed ET),
    matching part3's today_events query a few lines below this call site's
    original location, so a scenario firing close to midnight ET buckets into
    the correct calendar day instead of a raw-UTC one (same UTC-vs-ET bug
    class as coverage_check.py's run_check and coverage_ticket_table.py's
    timing check -- see docs/deep_backlog.md's 2026-08-21 entries; this
    function's grouping was itself fixed 2026-08-21, previously
    `COUNT(DISTINCT date(ts))` with no 'localtime' conversion).

    The WHERE bound also converts via 'localtime' now (fixed same session,
    after review found the original "leave it UTC, the skew is harmless"
    reasoning was empirically wrong against the real DB): a plain
    `date('now','-14 days')` bound resolves to UTC midnight of day-14, which
    is 20:00 ET on day-15 -- not a rounding-error-scale skew, an extra ~4-hour
    ET slice (20:00-23:59 ET) that real coverage_events data actually lands
    in (a real local spike at hour 20 ET), and since several real scenarios
    sit at exactly the >=7-day "daily" threshold, that extra slice could
    flip a genuine edge-case scenario to falsely read as daily -- exactly the
    kind of thing this report exists to surface, not hide. It also made the
    window's width itself vary by up to a full ET day depending on what time
    of evening this ET-named script happens to run. `date('now','localtime',
    '-14 days')` fixes both: an exact, run-time-stable 14 ET-day window."""
    return dict(con.execute(
        "SELECT scenario_key, COUNT(DISTINCT date(ts, 'localtime')) FROM coverage_events "
        "WHERE date(ts, 'localtime') >= date('now', 'localtime', '-14 days') "
        "GROUP BY scenario_key"
    ).fetchall())


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
    # archived_at IS NULL -- same real bypass of get_watchlist() as
    # signals_db.get_live_nodes(), a raw scan that needs its own explicit
    # archive filter (docs/design.md's "Node archive state" entry). An
    # archived real-capital node must stop showing in the evening report.
    live_nodes = [dict(r) for r in con.execute(
        "SELECT * FROM watch_list WHERE state='live' AND archived_at IS NULL")]
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
            local_qty = helpers.buy_order_sizing(
                node, {'ticker': node['ticker'], 'current_price': pending['signal_price']})['shares']
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


def _cheap_signal_cross_check(ticker, signal_or_entry_time_str, window, z_score_threshold, entry_timing):
    """Independent, lightweight cross-check for a real PHANTOM flag -- computes the
    real prior-day SMA/Std/band directly from cache/research/{ticker}_1h.csv (the
    daily-collector-refreshed CSV, current through today -- NOT massive_hourly_
    derived, which has no automated refresh and silently went 4 real trading days
    stale, 2026-08-27) and checks whether the real bar genuinely breached it.

    Caller MUST pass trade_log.signal_time when available, entry_time only as a
    fallback (2026-08-28 fix, paired-review CONFIRMED HIGH finding): for
    TrailingBothZScoreBreakout (broker-tracked trailing-buy bounce fill), the real
    fill routinely lands 1+ hourly bars after the real signal bar -- passing
    entry_time reads the WRONG bar for the majority of real live nodes (10/17 are
    this strategy). signal_time is already a real bar-owning timestamp, no
    bucketing ambiguity.

    Built 2026-08-27 after a real false-PHANTOM incident on DPST: Part 3's kernel
    replay silently succeeded on stale data (never erroring, so the RuntimeError
    staleness guard never caught it) and reported a real, correct trade as a
    PHANTOM. A conditional 'run this only when something looks wrong' design
    wouldn't have caught that case either -- Part 3 never signaled anything was
    wrong. So this runs unconditionally for every real PHANTOM flag, not as a
    fallback gated on an explicit failure.

    Returns (breached: bool, detail: str) or (None, reason) if it can't check
    (e.g. CSV itself missing/stale, or entry_time isn't a real bar-owning hour)."""
    import pandas as pd
    try:
        df_h = pd.read_csv(f"cache/research/{ticker}_1h.csv", index_col=0, parse_dates=True)
    except FileNotFoundError:
        return None, f"no cache/research/{ticker}_1h.csv on file"
    entry_ts = pd.Timestamp(signal_or_entry_time_str)
    bar_ts = entry_ts.floor("h") - pd.Timedelta(minutes=30) if entry_ts.minute < 30 \
        else entry_ts.floor("h") + pd.Timedelta(minutes=30)
    if df_h.index.max() < entry_ts.normalize():
        return None, f"_1h.csv itself is stale (last bar {df_h.index.max()}, entry was {entry_ts})"
    if bar_ts not in df_h.index:
        return None, f"no {bar_ts} bar in _1h.csv (holiday/half-day/data gap?)"
    daily = df_h.resample("D").last().dropna(subset=["Close"])
    sma = daily["Close"].rolling(window).mean().shift(1)
    std = daily["Close"].rolling(window).std().shift(1)
    day = bar_ts.normalize()
    if day not in sma.index or pd.isna(sma.loc[day]) or pd.isna(std.loc[day]):
        return None, f"insufficient prior-day history for a real {window}-day SMA/Std as of {day}"
    lower_band = sma.loc[day] - z_score_threshold * std.loc[day]
    bar = df_h.loc[bar_ts]
    if isinstance(bar, pd.DataFrame):
        return None, f"duplicate {bar_ts} rows in _1h.csv -- can't pick one unambiguously"
    check_price = bar["Open"] if entry_timing == "open_check" else bar["Close"]
    if pd.isna(check_price):
        return None, f"bar={bar_ts} has no real {'Open' if entry_timing == 'open_check' else 'Close'} " \
                      f"(empty/no-trade bar) -- NaN can't be compared against the band, not a real no-breach"
    breached = check_price <= lower_band
    return breached, (f"bar={bar_ts} {'Open' if entry_timing == 'open_check' else 'Close'}="
                       f"${check_price:.2f} vs lower_band=${lower_band:.2f} "
                       f"(SMA={sma.loc[day]:.2f} Std={std.loc[day]:.2f} z={z_score_threshold}) "
                       f"-> {'REAL BREACH' if breached else 'no breach'}")


def _pattern_open_check_pinned_price(node, r):
    """Known non-issue: entry_timing='open_check' nodes pin signal_price to the
    real session-open print (schwab_client.get_session_open_price's quote.openPrice,
    active_signals.py:587), not a live tick sampled at signal_time -- root-caused
    2026-08-28 (SOXL wl_id=249, Task #7; see docs/deep_backlog.md's 2026-08-28
    entry). A signal_time that lands right at market open (09:31-09:40/14:31-14:40,
    active_signals._OPEN_CHECK_WINDOWS) is itself part of this normal pattern, not
    an anomaly -- the recorded signal_price should match the real 9:30/14:30
    session-open bar, not a live tick near signal_time.

    Returns None if this pattern doesn't apply (not an open_check node); otherwise
    (matched: bool, detail) or (None, reason) if it applies but can't be checked."""
    if node.get('entry_timing') != 'open_check':
        return None
    sig_price = r.get('signal_price')
    if sig_price is None:
        return None, "no signal_price on this real trade row"
    import pandas as pd
    sig_ts = pd.Timestamp(r.get('signal_time') or r['entry_time'])
    open_hour = 9 if sig_ts.hour < 12 else 14
    open_bar_ts = sig_ts.normalize() + pd.Timedelta(hours=open_hour, minutes=30)
    try:
        df_h = pd.read_csv(f"cache/research/{node['ticker']}_1h.csv", index_col=0, parse_dates=True)
    except FileNotFoundError:
        return None, f"no cache/research/{node['ticker']}_1h.csv on file"
    if open_bar_ts not in df_h.index:
        return None, f"no {open_bar_ts} bar in _1h.csv (holiday/half-day/data gap?)"
    real_open = df_h.loc[open_bar_ts, 'Open']
    if isinstance(real_open, pd.Series):
        return None, f"duplicate {open_bar_ts} rows in _1h.csv -- can't pick one unambiguously"
    if pd.isna(real_open):
        return None, f"bar={open_bar_ts} has no real Open (empty/no-trade bar)"
    real_open = float(real_open)
    diff_pct = abs(sig_price - real_open) / real_open * 100
    matched = diff_pct <= 0.1
    detail = (f"signal_price=${sig_price:.4f} vs real session-open bar={open_bar_ts} "
              f"Open=${real_open:.4f} (diff={diff_pct:.4f}%)")
    return matched, detail


def _pattern_topup_restamp(node, r):
    """Known non-issue: a top-up fill can restamp trade_log's signal_time/
    entry_time to the top-up's own (later) event while signal_price keeps the
    ORIGINAL trigger's price -- root-caused 2026-08-26 (DFEN wl_id=247, see
    docs/research_log.md's 2026-08-26 entry: real trigger 2026-08-25 09:31:22,
    restamped forward to a 2026-08-26 top-up). Applies only to open_check
    nodes: checks whether signal_price matches, tightly, that SAME pinned
    open_check window (9:30 or 14:30) on one of the 3 PRIOR calendar days
    instead of today's -- deliberately narrower than "any earlier bar in the
    lookback window": a real top_up event fires on essentially every real
    open_check fill (confirmed directly -- SOXL's clean, non-restamped
    wl_id=249 case has one too), so top_up presence alone isn't distinctive
    enough to gate on; the specific prior-day-pinned-window match is.

    Returns None if inapplicable (not open_check); otherwise (matched: bool,
    detail) or (None, reason)."""
    if node.get('entry_timing') != 'open_check':
        return None
    sig_price = r.get('signal_price')
    if sig_price is None:
        return None, "no signal_price on this real trade row"
    import pandas as pd
    sig_ts = pd.Timestamp(r.get('signal_time') or r['entry_time'])
    open_hour = 9 if sig_ts.hour < 12 else 14
    try:
        df_h = pd.read_csv(f"cache/research/{node['ticker']}_1h.csv", index_col=0, parse_dates=True)
    except FileNotFoundError:
        return None, f"no cache/research/{node['ticker']}_1h.csv on file"
    for days_back in (1, 2, 3):
        open_bar_ts = sig_ts.normalize() - pd.Timedelta(days=days_back) + pd.Timedelta(hours=open_hour, minutes=30)
        if open_bar_ts not in df_h.index:
            continue
        real_open = df_h.loc[open_bar_ts, 'Open']
        if isinstance(real_open, pd.Series) or pd.isna(real_open):
            continue
        real_open = float(real_open)
        diff_pct = abs(sig_price - real_open) / real_open * 100
        if diff_pct <= 0.1:
            # Corroborating (not gating -- see docstring): a top_up near signal_time
            # is consistent with the restamping fill, but present on ordinary fills too.
            con = sqlite3.connect(LIVE_DB)
            con.row_factory = sqlite3.Row
            has_topup = con.execute(
                "SELECT 1 FROM coverage_events WHERE ticker=? AND node_id=? AND scenario_key='top_up' "
                "AND datetime(ts, 'localtime') BETWEEN ? AND ? LIMIT 1",
                (node['ticker'], node['id'],
                 (sig_ts - pd.Timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S'),
                 (sig_ts + pd.Timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')),
            ).fetchone()
            con.close()
            topup_note = "corroborated by a same-window top_up event" if has_topup \
                else "(no corroborating top_up event found near signal_time)"
            detail = (f"signal_price=${sig_price:.4f} vs {open_bar_ts} (PRIOR-day, {days_back}d earlier, "
                      f"same {open_hour}:30 open_check window) Open=${real_open:.4f} (diff={diff_pct:.4f}%) "
                      f"{topup_note} -- recorded signal_time={sig_ts} likely restamped forward")
            return True, detail
    return None, "no tight match at any prior-day (1-3d back) pinned open_check window"


def _pattern_stale_duplicate_open_price(node, r):
    """Known BUG, not yet code-fixed (still open in docs/backlog_cache.md):
    schwab_client.get_session_open_price's SECOND same-day open_check pinned
    call (the 14:30 window) has been observed returning the exact same price
    as its FIRST (9:30) call instead of a fresh one -- root-caused 2026-08-26
    (DPST), confirmed directly via open_price_quality_log showing identical
    9:30/14:30 prices. This check classifies a matching flag as 'known bug,
    not investigation-needed' -- it does NOT fix the underlying bug, which
    stays separately tracked and open; don't conflate the two.

    Returns None if inapplicable (not open_check, or not a 14:30-window
    signal); otherwise (matched: bool, detail) or (None, reason)."""
    if node.get('entry_timing') != 'open_check':
        return None
    import pandas as pd
    sig_ts = pd.Timestamp(r.get('signal_time') or r['entry_time'])
    if sig_ts.hour != 14:
        return None
    day = sig_ts.strftime('%Y-%m-%d')
    con = sqlite3.connect(LIVE_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT target_h, price FROM open_price_quality_log WHERE ticker=? AND ts LIKE ? "
        "AND target_h IN (9, 14) AND target_m=30 ORDER BY ts",
        (node['ticker'], f"{day}%"),
    ).fetchall()
    con.close()
    by_hour = {row['target_h']: row['price'] for row in rows}
    if 9 not in by_hour or 14 not in by_hour:
        return None, f"missing open_price_quality_log rows for {day} (9:30 present={9 in by_hour}, 14:30 present={14 in by_hour})"
    matched = abs(by_hour[9] - by_hour[14]) < 1e-6
    detail = f"9:30 logged open=${by_hour[9]:.4f} vs 14:30 logged open=${by_hour[14]:.4f}"
    return matched, detail


# Ordered library of known-explained non-issue patterns for a real PHANTOM/
# signal-mismatch flag, checked after the existing stale-kernel-data breach
# check (_cheap_signal_cross_check) comes back with no real breach. Each entry
# is (label, check_fn); check_fn returns None if inapplicable, (None, reason)
# if applicable but unable to verify, or (matched: bool, detail) once checked.
# Extend this list when a new root cause gets explained, rather than
# re-deriving it by hand next time the same pattern recurs (Task #8, 2026-08-28).
_KNOWN_SIGNAL_MISMATCH_PATTERNS = [
    ("open_check pinned-open-price", _pattern_open_check_pinned_price),
    # Same-day stale-duplicate checked BEFORE the cross-day restamp pattern: a
    # same-day exact 9:30==14:30 duplicate (DPST's case) is a stronger, more
    # direct signature than a cross-day approximate match, and the two can
    # otherwise collide (found testing 2026-08-28: DPST's real case also
    # happens to sit within the restamp pattern's 0.1% prior-day tolerance).
    ("get_session_open_price stale-duplicate 2nd pinned call", _pattern_stale_duplicate_open_price),
    ("same-day top-up restamp", _pattern_topup_restamp),
]


def _known_pattern_checks(node, r):
    """Runs the known-non-issue pattern library in order; returns (label, detail)
    for the first pattern that matches, or None if none matched (falls through
    to 'genuinely unexplained' at the call site)."""
    for label, fn in _KNOWN_SIGNAL_MISMATCH_PATTERNS:
        result = fn(node, r)
        if result is None:
            continue
        matched, detail = result
        if matched is None:
            continue  # applicable but couldn't be verified -- try the next pattern
        if matched:
            return label, detail
    return None


def _part2_daily_sweep(nodes, node_state):
    """Sub-part, added 2026-08-26 after the real SOXL/DPST/DFEN investigation (see
    docs/research_log.md's 2026-08-26 entries): cheap, routine, per-node/per-day check,
    split from the full instrumented kernel trace (scripts/trace_real_vs_kernel_
    divergence.py, reserved for when something's already flagged suspicious).

    Two branches per currently-`holding` real node, by whether its open_positions
    entry_time is TODAY or a prior day:
      - PRIOR day, still open: only a limit check (not a full signal re-derivation) --
        has the current price breached the real fixed_sl/take_profit trigger, or has
        the position been open longer than max_hold_hours' worth of hourly bars, in
        either case implying it should already have exited and something's stuck.
      - TODAY: same-day stop-out-and-re-entry is real (confirmed on DPST 2026-08-25:
        3 real trades in one day) -- print every real BUY/SELL leg from today's broker
        order history (schwab_client.get_real_orders) directly, not just today's
        trade_log rows (_part2_activity above already does the trade_log side; this
        adds the broker-order-level view so a same-day sequence is visible even before
        a leg closes into trade_log)."""
    print("\n--- 2b. Per-position daily sweep (prior-day limit check / today's real branch walk) ---")
    any_printed = False
    for n in nodes:
        status, real_position, cur = node_state.get(n['id'], (None, None, None))
        if status != 'holding' or real_position is None or cur is None:
            continue
        any_printed = True
        entry_time_str = real_position['entry_time']
        entry_date = entry_time_str[:10]
        label = f"{n['ticker']:6s} {n['account'] or '':10s}"

        if entry_date == TODAY:
            print(f"{label} entered TODAY -- real broker order history for {TODAY}:")
            try:
                orders = schwab_client.get_real_orders(n['account'], n['ticker'])
            except Exception as e:
                print(f"    order history fetch failed ({e})")
                continue
            todays = sorted((o for o in orders if str(o.get('enteredTime') or '')[:10] == TODAY
                              and o.get('status') == 'FILLED'),
                             key=lambda o: o['enteredTime'])
            if not todays:
                print("    no FILLED orders found today (may still be resting/pending)")
            for o in todays:
                print(f"    {o['enteredTime']}  {o['instruction']:4s} {o['orderType']:12s} "
                      f"qty={o['quantity']:g}")
            continue

        # PRIOR day, still open -- limit check only.
        entry_price = real_position['entry_price']
        fixed_sl = real_position.get('fixed_sl')
        take_profit = real_position.get('take_profit')
        flags = []
        if fixed_sl is not None:
            sl_price = entry_price * (1 - fixed_sl / 100)
            if cur <= sl_price:
                flags.append(f"cur ${cur:.4f} <= SL trigger ${sl_price:.4f} ({fixed_sl}%) -- "
                              f"should already have stopped out")
        if take_profit is not None:
            tp_price = entry_price * (1 + take_profit / 100)
            if cur >= tp_price:
                if strategies.uses_arm_trail_exit(n['strategy']):
                    # take_profit is an ARM threshold for these strategies (crossing it
                    # starts trailing, it's never a direct sell trigger -- see
                    # TrailingExitZScoreBreakout/TrailingBothZScoreBreakout.check_exit in
                    # strategies.py). Confirmed live 2026-09-08: AGQ (TrailingExit) was
                    # false-flagged here as "should already have taken profit" while
                    # correctly showing ARMED/trailing in Part 4 of the same run. Only a
                    # real problem if it crossed the arm trigger but trail_state still
                    # shows not-armed.
                    trailing = (real_position.get('trail_state') or {}).get('trailing')
                    if not trailing:
                        flags.append(f"cur ${cur:.4f} >= arm trigger ${tp_price:.4f} "
                                      f"({take_profit}%) -- should already be ARMED "
                                      f"(trailing) but trail_state shows not trailing")
                else:
                    flags.append(f"cur ${cur:.4f} >= TP trigger ${tp_price:.4f} ({take_profit}%) -- "
                                  f"should already have taken profit")
        max_hold_hours = real_position.get('max_hold_hours')
        if max_hold_hours is not None:
            try:
                import pandas as pd  # local: no other evening_status.py section needs pandas
                df_h = pd.read_csv(f"cache/research/{n['ticker']}_1h.csv", index_col=0, parse_dates=True)
                bars_since_entry = int((df_h.index > entry_time_str).sum())
                if bars_since_entry >= int(max_hold_hours):
                    flags.append(f"{bars_since_entry} hourly bar(s) since entry >= max_hold_hours="
                                 f"{max_hold_hours} -- should already have TIME-exited")
            except Exception as e:
                flags.append(f"could not check max_hold_hours ({e})")
        if flags:
            print(f"{label} entered {entry_date} (prior day), still open -- LIMIT BREACH:")
            for f in flags:
                print(f"    {f}")
        else:
            print(f"{label} entered {entry_date} (prior day), still open -- within SL/TP/max-hold limits")
    if not any_printed:
        print("no real open positions to sweep")


def _brokerage_tax_forecast_section():
    """End-of-year tax reserve forecast for `brokerage` (the one taxable account) --
    docs/deep_backlog.md's 2026-08-15 tax-forecast model. ESTIMATOR ONLY, not a
    filing-accuracy tool: expected error ~5-10%, "close enough to plan cash
    around," not precision-to-the-dollar -- the CPA will catch any real shortfall.
    Reuses k1_tax.py's RateConfig/blended-rate engine and rate-config persistence;
    this function is only the realized-loss-baseline netting + reserve piece."""
    db.ensure_tables()  # idempotent; guarantees tax_realized_loss_baseline exists + is seeded
    year = int(TODAY[:4])
    # Tax realization is a property of trade_log (a closed trade this year),
    # not current node state -- scoping to state='live' nodes only silently
    # dropped a ticker's already-realized gains from the forecast if it was
    # demoted to paper (or its node deleted) after realizing them earlier in
    # the year (found in review, 2026-08-15). Union with currently-live
    # tickers too, so a ticker with a live node but zero closed trades yet
    # still shows up in the "no closed trades yet" case below instead of
    # being silently invisible.
    con = sqlite3.connect(LIVE_DB)
    live_tickers = {r[0] for r in con.execute(
        "SELECT DISTINCT ticker FROM watch_list WHERE account='brokerage' AND state='live'"
    ).fetchall()}
    realized_tickers = {r[0] for r in con.execute(
        "SELECT DISTINCT ticker FROM trade_log WHERE account='brokerage' AND exit_time IS NOT NULL "
        "AND COALESCE(is_dry_run_sim, 0) = 0 AND strftime('%Y', exit_time) = ?", (str(year),)
    ).fetchall()}
    tickers = sorted(live_tickers | realized_tickers)
    if not tickers:
        print("=== Tax forecast (brokerage) ===")
        print("No real state='live' brokerage nodes or closed trades found -- nothing to forecast.\n")
        return

    realized = db.get_realized_pnl_by_ticker('brokerage', tickers, year)
    baseline = db.get_tax_realized_loss_baseline('brokerage', year)
    paid = sum(amt for _, amt in k1_tax.get_payments(year))
    forecast = k1_tax.brokerage_tax_forecast(year, realized, baseline, estimate_already_paid=paid)

    print(f"=== Tax forecast (brokerage, {year}) -- ESTIMATOR ONLY, not filing-accuracy "
          f"(~5-10% expected error; confirm with CPA/K-1) ===")
    print(f"Tickers scoped (live + realized-this-year): {', '.join(tickers)} "
          f"(Section 1256 60/40: {', '.join(sorted(forecast.section_1256_gain)) or 'none'}; "
          f"ordinary short-term: {', '.join(sorted(forecast.ordinary_st_gain)) or 'none'})")
    if not realized:
        print("No closed brokerage trades yet this year.")
    else:
        for t, g in sorted(realized.items()):
            print(f"  {t:6s} realized YTD: ${g:>12,.2f}")
    print(f"Long-term slice (60% of Sec.1256 gain): gross=${forecast.lt_gain_gross:,.2f}  "
          f"baseline_loss=${forecast.lt_baseline_loss:,.2f}  net_taxable=${forecast.lt_gain_net:,.2f}  "
          f"baseline_remaining=${forecast.lt_baseline_remaining:,.2f}")
    print(f"Short-term pool (ordinary-ST gains + 40% Sec.1256 slice): gross=${forecast.st_pool_gross:,.2f}  "
          f"baseline_loss=${forecast.st_baseline_loss:,.2f}  net_taxable=${forecast.st_pool_net:,.2f}  "
          f"baseline_remaining=${forecast.st_baseline_remaining:,.2f}")
    print(f"Liability=${forecast.liability:,.2f}  estimate_already_paid=${forecast.estimate_already_paid:,.2f}  "
          f"Reserve=${forecast.reserve:,.2f}")
    if forecast.recommend_full_sweep:
        print("SIGNAL: realized-loss baseline fully exhausted and profit remains -- "
              "recommend sweeping 100% of profit to brokerage's margin buffer.")
    elif not forecast.baseline_exhausted:
        print(f"Baseline not yet exhausted (${forecast.st_baseline_remaining + forecast.lt_baseline_remaining:,.2f} "
              f"remaining) -- no sweep recommendation yet.")

    open_brokerage = [p for p in db.get_open_positions() if p.get('account') == 'brokerage']
    if not open_brokerage:
        print("No open brokerage positions -- nothing unrealized to report.")
    else:
        prices = {}
        for p in open_brokerage:
            try:
                prices[p['ticker']] = schwab_client.get_current_price(p['ticker'])
            except Exception as e:
                print(f"  {p['ticker']:6s} unrealized: NOT CHECKED ({type(e).__name__})")
        unrealized = db.get_unrealized_pnl_by_ticker('brokerage', prices)
        u_forecast = k1_tax.unrealized_forecast(year, unrealized)
        print(f"Unrealized (mark-to-market, {len(open_brokerage)} open position(s)):")
        for t, g in sorted(unrealized.items()):
            tag = " [Sec.1256]" if t in k1_tax.SECTION_1256_TICKERS else ""
            print(f"  {t:6s} unrealized: ${g:>12,.2f}{tag}")
        if u_forecast.section_1256_unrealized:
            print(f"  Sec.1256 hypothetical MTM liability (INFORMATIONAL ONLY, NOT in Reserve above): "
                  f"${u_forecast.section_1256_hypothetical_liability:,.2f} -- {u_forecast.note}")
    print()


def part2():
    print(f"=== Part 2: real capital-at-stake nodes ({TODAY}) ===")
    _brokerage_tax_forecast_section()
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
    _part2_daily_sweep(nodes, node_state)

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
    per_node_results = []  # (notional, {window: pct or None}, since_incept, account, ticker, row_str)
    for n in nodes:
        trades = all_trades.get(n['id'], [])
        cells, results = [], {}
        for w, days in windows.items():
            r = _compounded(trades, days)
            results[w] = r
            cells.append(f"{r:+8.2f}%" if r is not None else f"{'-':>9s}")
        since_incept = _since_inception(trades, n.get('added_at'))
        incept_s = f"{since_incept:+.2f}%" if since_incept is not None else "-"
        row_str = f"{n['ticker']:6s} " + " ".join(cells) + f" {incept_s:>10s}"
        print(row_str)
        ns = node_state.get(n['id'])
        mv = ns[1]['shares'] * ns[2] if ns and ns[0] == 'holding' else None
        per_node_results.append((effective_notional(n, mv), results, since_incept, n.get('account'), row_str))

    # Notional-weighted average of each ticker's OWN compounded return per window -- not a
    # chained product of unrelated trades across 3 separate accounts (the prior version's
    # bug, flagged by review: that treats simultaneous positions as sequential capital use).
    def _weighted_row(rows):
        """rows: iterable of (notional, results_dict, since_incept). Returns
        (cells, incept_s) -- shared by the ALL row and each per-account row
        below so both use the identical weighting math."""
        cells = []
        for w in windows:
            weighted = [(notional, res[w]) for notional, res, _ in rows if res[w] is not None]
            if not weighted:
                cells.append(f"{'-':>9s}")
                continue
            total_notional = sum(notional for notional, _ in weighted)
            avg = sum(notional * pct for notional, pct in weighted) / total_notional if total_notional else 0
            cells.append(f"{avg:+8.2f}%")
        incept_weighted = [(notional, si) for notional, _, si in rows if si is not None]
        if incept_weighted:
            total_notional = sum(notional for notional, _ in incept_weighted)
            incept_avg = sum(notional * si for notional, si in incept_weighted) / total_notional if total_notional else 0
            incept_s = f"{incept_avg:+.2f}%"
        else:
            incept_s = "-"
        return cells, incept_s

    print("\nPortfolio (notional-weighted average across real capital-at-stake tickers):")
    all_rows = [(notional, res, si) for notional, res, si, _acct, _row in per_node_results]
    cells, incept_s = _weighted_row(all_rows)
    print(f"{'ALL':6s} " + " ".join(cells) + f" {incept_s:>10s}")

    # Per-account breakdown (2026-08-16 -- previously only the flat per-
    # ticker rows above + one blended ALL row existed; the real, scoped gap
    # left from the original portfolio-return-calc backlog item was distinct
    # capital pools (brokerage/ira/roth/soxl_ira) having no way to see their
    # OWN ticker rows + subtotal without hand-filtering the flat table above.
    # Re-prints each account's own ticker rows (same row_str already printed
    # once above, not recomputed) grouped under its own header, then the
    # same _weighted_row math as ALL, scoped to just that account's nodes.
    by_account = {}
    for notional, res, si, acct, row_str in per_node_results:
        by_account.setdefault(acct, []).append((notional, res, si, row_str))
    if len(by_account) > 1:
        print("\nBy account:")
        for acct in sorted(by_account, key=lambda a: (a is None, a)):
            rows = by_account[acct]
            label = acct or '(no account)'
            print(f"  {label}:")
            for _n, _r, _s, row_str in rows:
                print(f"    {row_str}")
            acct_cells, acct_incept_s = _weighted_row([(n, r, s) for n, r, s, _row in rows])
            print(f"    {'subtotal':6s} " + " ".join(acct_cells) + f" {acct_incept_s:>10s}")


# verify_live_parity.replay() drives compute_buy_signal once per bar and opens the position on
# that same bar. It has no equivalent of the real multi-bar resting trailing-buy "wait for the
# bounce above the running low" state machine, so these two strategies cannot be replayed by
# that harness at all -- see its module docstring. Not a config choice here, a structural gap.
PARITY_UNSUPPORTED = {'TrailingBuyZScoreBreakout', 'TrailingBothZScoreBreakout'}


def _deep_parity_worker(ticker, wl_id, strategy, window, z_score_threshold, take_profit,
                         sl, max_hold_hours, trail_pct):
    """ProcessPoolExecutor worker -- must stay a plain module-level function (picklable) and
    take only primitive args, not a `node` dict closed over from the caller. Computes and
    returns the exact print line _deep_live_parity used to build inline; the parallelized
    version below prints them back in original node order once all workers finish, since
    prints can't be safely interleaved across worker processes."""
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            kt = parity.kernel_trades(ticker, strategy, window, z_score_threshold, take_profit,
                                       sl, max_hold_hours, trail_pct=trail_pct)
            rt = parity.replay(ticker, strategy, window, z_score_threshold, take_profit,
                                sl, max_hold_hours, trail_pct=trail_pct)
    except Exception as e:
        return f"  {ticker:6s} wl_id={wl_id:4d}  NOT CHECKED -- replay failed ({e})"
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
        return f"  {ticker:6s} MATCH, {len(kt)} trades identical"
    elif first is None:
        return (f"  {ticker:6s} count differs (kernel {len(kt)} vs replay {len(rt)}), "
                f"first {min(len(kt), len(rt))} identical")
    else:
        i, k, _ = first
        return (f"  {ticker:6s} first mismatch #{i} entry {k['Entry Time']:%Y-%m-%d}, "
                f"kernel {len(kt)}/replay {len(rt)} trades")


TICK_TO_ACTION_FLAG_SECS = 30
TICK_TO_ACTION_MATCH_WINDOW_SECS = 15 * 60
# Every real outcome a tick can resolve to -- a BLOCK is just as much an "action" as a
# placed order for this purpose (see tick_to_trade_report.py's identical list).
TICK_TO_ACTION_SCENARIO_KEYS = (
    "automated_buy_execution",
    "buy_signal_window_block",
    "daily_order_cap_block",
    "global_burst_cap_block",
    "same_day_block",
    "hard_order_ceiling_block",
)


def _open_check_tick_to_action():
    """Measures the delay from every entry_timing='open_check' tick today (the pinned
    Open price fetch, open_price_quality_log) to whatever outcome followed it -- a
    placed order OR any check_order SafetyViolation block -- not just price drift on
    successful placements. Found 2026-09-17 diagnosing ETHU's missed 2026-09-15 14:30
    entry (blocked, 573s after its own tick) and DPST/OILU/ERY/DFEN/HIBL/NUGT's
    same-night near-misses (168-432s, still placed): the common thread across ALL of
    these isn't price drift specifically, it's DELAY -- every tick that should trigger
    an action deserves its delay measured, whether the action succeeded, drifted in
    price, or got blocked outright. This generalizes the original price-drift-only
    version of this check per the same-night discussion.

    Bounded to open_check nodes -- open_price_quality_log is the only place a tick
    timestamp is already logged without touching active_signals.py/signals_notify.py;
    a `close`-timing node's ambient entry has no equivalent logged tick today, so this
    can't yet cover every entry-triggering tick system-wide. Exit-side ticks (SL/ARM/
    TP/TIME) are explicitly out of scope too (2026-09-17 call) -- a different shape
    (no single pinned reference tick), not built here.

    Scoped to TODAY only, matching this Part's other same-day sub-checks; a longer
    lookback is scripts/tick_to_trade_report.py's job (same join, this is its daily
    standing-report sibling). Read-only, no judgment on whether any given trade was
    itself "wrong" -- just surfaces the delay so it's never invisible again."""
    since_utc = f"{TODAY} 00:00:00"
    quality_rows = db.get_open_price_quality_log(since=since_utc)
    if not quality_rows:
        return
    outcomes = _get_coverage_events_since(TICK_TO_ACTION_SCENARIO_KEYS, since_utc)
    by_ticker = {}
    for o in outcomes:
        by_ticker.setdefault(o["ticker"], []).append(o)

    print(f"\n--- 4b. Tick-to-action delay, every open_check entry tick today ---")
    # Reports EVERY live outcome in-window per tick, not a single "best match" --
    # a ticker with 2+ concurrently active live nodes can legitimately produce 2+
    # real, distinct outcomes off the same shared tick (open_price_quality_log
    # logs one tick per ticker, not per node); picking a nearest winner would
    # silently drop a real event. Same fix as tick_to_trade_report.py, 2026-09-17.
    unmatched = 0
    n_outcomes = 0
    flagged = []
    for q in quality_rows:
        tick_dt = datetime.strptime(q["ts"], "%Y-%m-%d %H:%M:%S")
        matches = []
        for o in by_ticker.get(q["ticker"], []):
            o_dt = datetime.strptime(o["ts"], "%Y-%m-%d %H:%M:%S")
            delta = (o_dt - tick_dt).total_seconds()
            if 0 <= delta <= TICK_TO_ACTION_MATCH_WINDOW_SECS:
                matches.append((delta, o))
        if not matches:
            unmatched += 1
            continue
        for delta, o in matches:
            n_outcomes += 1
            if delta >= TICK_TO_ACTION_FLAG_SECS:
                outcome_label = o["result"] if o["scenario_key"] == "automated_buy_execution" else f"BLOCKED:{o['scenario_key']}"
                flagged.append((q, delta, outcome_label, o["node_id"]))

    print(f"  {len(quality_rows)} pinned ticks today, {n_outcomes} real live outcome(s) matched, "
          f"{unmatched} tick(s) unmatched (no BUY signal ever fired at/after that tick)")
    if flagged:
        for q, delta, outcome_label, node_id in sorted(flagged, key=lambda r: -r[1]):
            print(f"  ⚠️  {q['ticker']:6s} target={q['target_h']:02d}:{q['target_m']:02d}  "
                  f"delay={delta:.0f}s  outcome={outcome_label}  node_id={node_id}")
        print(f"  {len(flagged)} of {n_outcomes} matched outcome(s) took >= {TICK_TO_ACTION_FLAG_SECS}s "
              f"from tick to action")
    else:
        print(f"  all {n_outcomes} matched outcome(s) resolved in under {TICK_TO_ACTION_FLAG_SECS}s")


NON_BLOCKING_RESULTS = ("skipped_margin_account",)


def _get_coverage_events_since(scenario_keys, since):
    """mode='live' only -- a ticker can carry several watch_list nodes at once
    (dry_run/research/paper/live, different accounts/strategies); coverage_events
    is per-node but open_price_quality_log's tick is logged per-TICKER only, so
    without this filter a ticker-only join can silently match a dry_run/research
    node's own unrelated activity instead of the real live outcome (found
    2026-09-17: GDXU's join grabbed a research-state dry_run node's $129.04
    'placed' instead of the actual live node's real outcome). Also excludes
    NON_BLOCKING_RESULTS -- same_day_block's 'skipped_margin_account' is an
    informational log, not a real block, and was matching ahead of the real
    outcome a few seconds later."""
    con = sqlite3.connect(LIVE_DB)
    con.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in scenario_keys)
    exclude_placeholders = ",".join("?" for _ in NON_BLOCKING_RESULTS)
    rows = con.execute(
        f"SELECT ts, ticker, scenario_key, result, detail, node_id FROM coverage_events "
        f"WHERE scenario_key IN ({placeholders}) AND ts >= ? AND mode='live' "
        f"AND result NOT IN ({exclude_placeholders}) ORDER BY ts",
        (*scenario_keys, since, *NON_BLOCKING_RESULTS),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


FILL_TO_SL_FLAG_SECS = 15
FILL_TO_ADDON_MATCH_WINDOW_SECS = 24 * 60 * 60  # addon can legitimately fire hours after fill (arm-triggered), not a delay signal itself


def _fill_to_sl_delay():
    """Measures the delay from a real buy fill (buy_fill_reconciled) to that
    position's stop-loss actually being placed at the broker (sl_placement) --
    the position is genuinely UNPROTECTED for this whole gap. Raised 2026-09-17
    alongside the tick-to-action work: 'when filled, need filled-to-SL measured'
    -- a fill->SL delay is arguably higher-stakes than an entry-timing delay,
    since it's real capital sitting with no resting protective order, not just
    a signal that fired late. Joined by (ticker, nearest sl_placement after the
    fill) rather than position_id -- buy_fill_reconciled logs node_id (wl_id),
    sl_placement logs position_id (open_positions.id), two different id spaces;
    a wl_id can only have one open position at a time so ticker+time-proximity
    is unambiguous in practice. Scoped to TODAY, same convention as this Part's
    other same-day sub-checks."""
    since_utc = f"{TODAY} 00:00:00"
    fills = _get_coverage_events_since(("buy_fill_reconciled",), since_utc)
    sl_events = _get_coverage_events_since(("sl_placement",), since_utc)
    by_ticker = {}
    for s in sl_events:
        by_ticker.setdefault(s["ticker"], []).append(s)

    print(f"\n--- 4c. Fill-to-SL-placed delay, every real buy fill today ---")
    if not fills:
        print("  no real buy fills today")
        return
    unmatched = 0
    flagged = []
    for f in fills:
        f_dt = datetime.strptime(f["ts"], "%Y-%m-%d %H:%M:%S")
        best = None
        for s in by_ticker.get(f["ticker"], []):
            s_dt = datetime.strptime(s["ts"], "%Y-%m-%d %H:%M:%S")
            delta = (s_dt - f_dt).total_seconds()
            if 0 <= delta <= TICK_TO_ACTION_MATCH_WINDOW_SECS and (best is None or delta < best[0]):
                best = (delta, s)
        if best is None:
            unmatched += 1
            continue
        delta, s = best
        if delta >= FILL_TO_SL_FLAG_SECS:
            flagged.append((f, delta))
    matched = len(fills) - unmatched
    print(f"  {len(fills)} real fill(s) today, {matched} matched to an SL placement, "
          f"{unmatched} unmatched (no sl_placement event found -- worth checking directly, "
          f"not just a slow-report artifact)")
    if flagged:
        for f, delta in sorted(flagged, key=lambda r: -r[1]):
            print(f"  ⚠️  {f['ticker']:6s} fill@{f['ts']}  UNPROTECTED for {delta:.0f}s before SL placed")
    elif matched:
        print(f"  all {matched} matched fill(s) got their SL placed within {FILL_TO_SL_FLAG_SECS}s")


def _fill_to_addon_delay():
    """Measures the delay from a real buy fill to that position's first addon
    leg placement (addon_entry_placement), for positions where an addon leg
    actually fired. NOT flagged on a delay threshold the way fill-to-SL is --
    an addon leg fires on its own arm/vol-gate condition, legitimately hours
    after fill (see docs/CLAUDE.md's overlay design), so a long gap here is
    normal, not a defect. Pure reporting, matching section 4's existing
    'magnitude, pure reporting, no threshold' convention for the same reason.
    Scoped to TODAY, same convention as this Part's other same-day sub-checks."""
    since_utc = f"{TODAY} 00:00:00"
    fills = _get_coverage_events_since(("buy_fill_reconciled",), since_utc)
    addon_events = _get_coverage_events_since(("addon_entry_placement",), since_utc)
    by_ticker = {}
    for a in addon_events:
        by_ticker.setdefault(a["ticker"], []).append(a)

    print(f"\n--- 4d. Fill-to-addon delay, informational only (no defect threshold) ---")
    if not fills or not addon_events:
        print("  no addon leg placed today" if fills else "  no real buy fills today")
        return
    shown = 0
    for f in fills:
        f_dt = datetime.strptime(f["ts"], "%Y-%m-%d %H:%M:%S")
        best = None
        for a in by_ticker.get(f["ticker"], []):
            a_dt = datetime.strptime(a["ts"], "%Y-%m-%d %H:%M:%S")
            delta = (a_dt - f_dt).total_seconds()
            if 0 <= delta <= FILL_TO_ADDON_MATCH_WINDOW_SECS and (best is None or delta < best[0]):
                best = (delta, a)
        if best is None:
            continue
        delta, a = best
        shown += 1
        print(f"  {f['ticker']:6s} fill@{f['ts']}  addon placed +{delta / 60:.1f}min later")
    if not shown:
        print("  no fill today had a matching addon leg placement")


def _deep_live_parity():
    """Plan Part 3 sub-part 3 -- live CODE vs kernel, which is a different question from the
    outcome-vs-kernel check above: it replays active_signals.py's own compute_buy_signal/
    check_sell_condition bar-by-bar against the numba kernel, so it catches silent drift
    between the two codebases even on days with no real trades at all.

    The per-node kernel_trades()/replay() pair is the dominant cost of the whole evening
    report (timed 2026-08-28: 187.98s of ~233s total 4-part run, vs 19.13s with
    --skip-deep-parity) -- each node's compute is fully independent (no shared state), so
    it's parallelized via ProcessPoolExecutor (matching this project's sweep-worker-pool
    convention, see run_optimization_sweep.py), and same-day results are cached to
    DEEP_PARITY_CACHE_PATH so a later same-day invocation just reads and prints them instead
    of recomputing. Only real_capital_nodes()'s cheap DB query and the unsupported-strategy
    listing stay uncached/recomputed fresh every call. Caching same-day is safe because the
    16:05 EOD cron run happens after the 16:00 close -- nothing about that day's trade/
    position state changes after that (confirmed with user, 2026-08-28)."""
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

    node_keys = sorted(f"{n['ticker']}:{n['id']}" for n in supported)
    cache = None
    if '--refresh-parity' not in sys.argv and DEEP_PARITY_CACHE_PATH.exists():
        try:
            cache = json.loads(DEEP_PARITY_CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            cache = None
        if cache and (cache.get('date') != TODAY or sorted(cache.get('node_keys', [])) != node_keys):
            cache = None  # different day, or the live node set changed since -- recompute

    if cache is not None:
        print(f"(cached from {cache['computed_at']})")
        for line in cache['lines']:
            print(line)
        return

    lines_by_ticker = {}
    max_workers = min(len(supported), os.cpu_count() or 4)
    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for n in supported:
            uses_fixed = strategies.uses_fixed_sl(n['strategy'])
            sl = (n.get('fixed_sl') if uses_fixed else n.get('stop_loss')) or 0
            fut = pool.submit(_deep_parity_worker, n['ticker'], n['id'], n['strategy'],
                               n['window'], n['z_score_threshold'], n['take_profit'], sl,
                               n['max_hold_hours'], n.get('trail_sell_pct'))
            futures[fut] = n['ticker']
        for fut in as_completed(futures):
            lines_by_ticker[futures[fut]] = fut.result()

    lines = [lines_by_ticker[n['ticker']] for n in supported]
    for line in lines:
        print(line)

    DEEP_PARITY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEEP_PARITY_CACHE_PATH.write_text(json.dumps({
        'date': TODAY,
        'computed_at': datetime.now().strftime('%H:%M:%S'),
        'node_keys': node_keys,
        'lines': lines,
    }))


def part3():
    print(f"=== Part 3: coverage trend, paper vs kernel, live vs kernel ({TODAY}) ===")
    # Got this wrong twice (2026-08-16) -- see the print below for the rule.
    print("--- staged-node staleness questions: use `audit_live_test_candidates.py --staged` "
          "(see docs/grid_ticker_coverage_promotion_process.md), not ad hoc coverage_events queries ---")
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
    event_days = event_days_by_scenario(con)
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
            fb = fake_broker_proof_for(row['scenario_key'])[0] == 'event-asserted'
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
        # Snooze check (2026-08-16, spec'd 2026-08-16 morning): unlike
        # coverage_check.py's scenario_expectations checks (already wired to
        # coverage_snoozes/is_snoozed), this comparison had no way to
        # acknowledge and silence a known/understood divergence -- LABD
        # (wl_id=152) was re-flagging every single run with no way to quiet
        # it once the user confirmed why it diverges. Freeform scenario_key
        # ('paper_vs_kernel_mismatch'), no new table/registration needed --
        # matches the existing snooze_coverage.py CLI pattern directly.
        if db.is_snoozed('paper_vs_kernel_mismatch', ticker=node['ticker'], node_id=node['id']):
            results.append((node, len(paper), len(bt), 'snoozed', None))
            continue
        # Occurrence/magnitude split (2026-08-26, folded in from the trace-tool dispatch):
        # occurrence (did a trade happen at all) is a HARD bidirectional flag, no tolerance
        # band -- replaces the old abs(len(paper)-len(bt))>2 count-diff heuristic, which
        # could silently pass a real divergence (e.g. paper=3/kernel=3 by count, but a
        # different 3 trades on each side) or flag a benign one-trade-later-than-expected
        # timing wobble. Magnitude (real vs kernel return% for a trade BOTH sides agree
        # happened) is pure reporting, never gated -- see verify.match_trades' own 4h
        # signal-time tolerance, which is a "is this the same event" matching tolerance,
        # not a magnitude tolerance. Paper trades have no separate signal_time column
        # (paper_trade_log), so entry_time doubles as signal_time here -- a paper fill is
        # simulated at/near the signal bar, unlike a real trailing-buy's bounce-fill delay.
        real_for_match = [{"signal_time": r["entry_time"], "pnl_pct": r["pnl_pct"]}
                           for r in paper.to_dict("records")]
        bt_for_match = [{"entry_time": str(t["entry_time"]), "ret": t["ret"]} for t in bt]
        pmatched, punmatched_real, punmatched_bt = verify.match_trades(real_for_match, bt_for_match, 4)
        occurrence_mismatch = bool(punmatched_real or punmatched_bt)
        results.append((node, len(paper), len(bt), 'MISMATCH' if occurrence_mismatch else 'ok',
                         (pmatched, punmatched_real, punmatched_bt)))

    quiet = sum(1 for r in results if r[3] == 'quiet')
    not_paper = sum(1 for r in results if r[3] == 'not-paper')
    errored = sum(1 for r in results if r[3] == 'error')
    flagged = sum(1 for r in results if r[3] == 'MISMATCH')
    matched = sum(1 for r in results if r[3] == 'ok')
    snoozed = sum(1 for r in results if r[3] == 'snoozed')
    print(f"{len(paper_nodes)} total: {quiet} no activity either side, {not_paper} not a paper node "
          f"(dry_run/canary/live -- not comparable here), {matched} matched, {flagged} issues"
          + (f", {snoozed} snoozed" if snoozed else "")
          + (f", {errored} errored" if errored else ""))
    for node, paper_n, kernel_n, tag, detail in results:
        if tag == 'MISMATCH':
            pmatched, punmatched_real, punmatched_bt = detail
            print(f"  {node['ticker']:6s} wl_id={node['id']:4d}  paper={paper_n} kernel={kernel_n}  MISMATCH"
                  f" ({len(punmatched_real)} paper trade(s) with no kernel counterpart, "
                  f"{len(punmatched_bt)} kernel trade(s) the paper track never took)")
        elif tag == 'error':
            print(f"  {node['ticker']:6s} wl_id={node['id']:4d}  NOT CHECKED ({detail})")
        elif tag == 'snoozed':
            print(f"  {node['ticker']:6s} wl_id={node['id']:4d}  paper={paper_n} kernel={kernel_n}  (snoozed)")
        elif tag == 'ok':
            print(f"  {node['ticker']:6s} wl_id={node['id']:4d}  paper={paper_n} kernel={kernel_n}  ok")
        # Magnitude, pure reporting -- every matched pair's real vs kernel return%, never
        # gated/flagged regardless of how far apart the two numbers are, and printed
        # independent of the node's occurrence tag above (LABD-shaped case: an occurrence
        # MISMATCH from 11 unmatched paper trades doesn't mean the OTHER 5 matched trades
        # have nothing worth reporting -- found live testing this exact node, 2026-08-26).
        # pnl_pct is NULL until a paper position closes -- a still-open one has no real
        # return yet to compare.
        if tag in ('MISMATCH', 'ok') and detail and detail[0]:
            pmatched = detail[0]
            # paper's pnl_pct came through a pandas DataFrame (.to_dict('records')) --
            # a still-open row's missing value is NaN there, not None, and `is not None`
            # silently passes NaN through (found live testing this exact code, 2026-08-26:
            # SOXL printed "real +nan% vs kernel ...").  != self is the NaN-only-value-that-
            # isn't-equal-to-itself check, no pandas import needed for one comparison.
            priced = [(r, t) for r, t, _dh in pmatched
                      if r.get('pnl_pct') is not None and r.get('pnl_pct') == r.get('pnl_pct')]
            if priced:
                mags = ", ".join(f"real {r['pnl_pct']:+.2f}% vs kernel {t['ret']*100:+.2f}%" for r, t in priced)
                print(f"      matched: {mags}")

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
              "ticker": r["ticker"], "exit_reason": r["exit_reason"], "pnl_pct": r["pnl_pct"],
              "signal_price": r["signal_price"]} for r in node_real],
            [{"entry_time": str(t["entry_time"]), "ret": t["ret"]} for t in bt], 4)
        genuine = [r for r in unmatched_real if not verify.is_staged_or_manual(r['ticker'], r['entry_time'], r['exit_reason'])]
        # Occurrence (PHANTOM/MISSED, bidirectional -- a kernel trade the real daemon
        # NEVER TOOK is exactly as much of a "trades must equal backtest" violation as a
        # real trade the kernel never predicted) stays a HARD flag, no tolerance band --
        # the existing 4h window above is match_trades' own "is this the same event"
        # signal-time tolerance, not a magnitude tolerance, so it's unchanged.
        flags = []
        if genuine:
            flags.append(f"{len(genuine)} PHANTOM real trade(s) the kernel never predicted")
        if unmatched_bt:
            flags.append(f"{len(unmatched_bt)} MISSED kernel trade(s) the daemon never took")
        flag = "; ".join(flags) if flags else f"matches kernel ({len(matched)} matched)"
        print(f"  {node['ticker']:6s} {node['account'] or '':10s} wl_id={wl_id:4d}  {flag}")
        for r in genuine:
            print(f"      PHANTOM: entry={r['entry_time']} exit_reason={r['exit_reason']}")
            # Always run, not gated on Part 3's own check having errored -- see
            # _cheap_signal_cross_check's own docstring for why (it can silently
            # succeed on stale data instead of raising).
            breached, detail = _cheap_signal_cross_check(
                node['ticker'], r.get('signal_time') or r['entry_time'],
                node['window'], node['z'], node['entry_timing'])
            if breached is None:
                print(f"        cross-check: UNABLE TO VERIFY ({detail})")
            elif breached:
                print(f"        cross-check: REAL signal confirmed independently ({detail}) "
                      f"-- likely a stale-kernel-data false PHANTOM, not a real divergence")
            else:
                pattern = _known_pattern_checks(node, r)
                if pattern:
                    plabel, pdetail = pattern
                    print(f"        cross-check: NO real breach found ({detail}) -- but matches known "
                          f"pattern '{plabel}': {pdetail} -- known non-issue, not investigation-needed")
                else:
                    print(f"        cross-check: NO real breach found ({detail}) "
                          f"-- this PHANTOM looks genuine, worth investigating")
        for t in unmatched_bt:
            print(f"      MISSED : kernel entry={t['entry_time']}")
        # Magnitude, pure reporting (2026-08-26, folded in from the trace-tool dispatch)
        # -- once BOTH sides agree a trade happened, just show real vs kernel return% side
        # by side. Deliberately no pass/fail line and no threshold here -- Part 3's
        # existing compounded-return divergence section (below) already owns that
        # aggregate, separately-calibrated alert; this is per-trade information only.
        # pnl_pct is NULL on trade_log until a position closes -- a still-open real
        # position matched against a kernel trade has no real return yet to compare.
        priced_matches = [(r, t) for r, t, _dh in matched if r.get('pnl_pct') is not None]
        if priced_matches:
            mags = ", ".join(f"entry={r['entry_time']} real {r['pnl_pct']:+.2f}% vs kernel {t['ret']*100:+.2f}%"
                              for r, t in priced_matches)
            print(f"      matched: {mags}")
    for wl_id, reason in sorted(unchecked.items()):
        print(f"  wl_id={wl_id:4d}  UNCHECKED -- {reason}")
    print(f"{active_count} node(s) had activity to compare, {quiet_count} confirmed quiet on BOTH "
          f"real and kernel sides (checked, not assumed)")

    _open_check_tick_to_action()
    _fill_to_sl_delay()
    _fill_to_addon_delay()

    _deep_live_parity()

    print(f"\n--- 5. Real vs kernel compounded-return divergence (trailing {DIVERGENCE_WINDOW_DAYS}d) ---")
    div_start = (datetime.now() - timedelta(days=DIVERGENCE_WINDOW_DAYS)).strftime('%Y-%m-%d')
    div_real_all = verify.get_real_trades(div_start, TODAY, accounts=None)
    div_wl_ids = sorted({r['wl_id'] for r in div_real_all if r['wl_id'] and r['wl_id'] > 0})
    div_nodes, _div_skipped = verify.resolve_nodes(div_wl_ids, min_notional=5000)
    # A retired node has no forward performance to judge -- comparing its stale real
    # trades against a backtest replay that keeps running its dead config past archival
    # is meaningless (found 2026-08-28, SOXL wl_id=92 rotated out at v6 promotion,
    # 8-day-old real trades still dragged into this report with no current relevance).
    # This intentionally means a just-promoted node has thin/no comparison data for a
    # while -- accepted as bootstrap noise, per user's call, rather than stitching
    # predecessor/successor history together.
    div_nodes = {wl_id: node for wl_id, node in div_nodes.items() if not node.get("archived_at")}
    div_flagged = 0
    for wl_id, node in sorted(div_nodes.items()):
        node_real = [r for r in div_real_all if r['wl_id'] == wl_id and r['pnl_pct'] is not None]
        try:
            bt = verify.get_backtest_trades_in_window(node, div_start, TODAY)
        except Exception:
            continue
        result = compute_divergence(
            [r['pnl_pct'] / 100.0 for r in node_real], [t['ret'] for t in bt])
        if result is None:
            continue
        real_comp, bt_comp, delta_pp = result
        flagged = delta_pp < -DIVERGENCE_THRESHOLD_PP
        # Persisted 2026-08-20 (docs/backlog_cache.md, specced 2026-08-20) -- this was
        # print-only before, so nothing survived past whatever terminal ran it. Logs every
        # node with a computed comparison (not just flagged ones), so a later session can
        # see the full picture, not just the alarms.
        db.log_divergence_check(wl_id, node['ticker'], node['account'], TODAY,
                                 DIVERGENCE_WINDOW_DAYS, real_comp, bt_comp, delta_pp, flagged)
        if flagged:
            div_flagged += 1
            print(f"  ⚠️  {node['ticker']:6s} {node['account'] or '':10s} wl_id={wl_id:4d}  "
                  f"NOT MATCHING BACKTEST: real {real_comp:+.1f}% vs backtest-implied {bt_comp:+.1f}% "
                  f"over the last {DIVERGENCE_WINDOW_DAYS}d ({-delta_pp:.1f}pp worse than backtest predicted)")
    if not div_flagged:
        print(f"  no node exceeds the {DIVERGENCE_THRESHOLD_PP:.0f}pp divergence threshold "
              f"(of {len(div_nodes)} node(s) with enough trades to compare)")
    # Run-level marker (2026-08-20, from paired Opus review of the EOD-wiring diff) -- the
    # per-node divergence_check_log rows above only exist when a node has enough trades to
    # compare, so on a thin-trading night zero rows is indistinguishable from "never ran" or
    # "crashed before reaching here." This coverage_event fires every run regardless, so a
    # later session can tell "ran, N node(s) checked, M flagged" apart from silence.
    db.log_coverage_event("divergence_check_run", "live", None,
                           result="completed", detail=f"checked={len(div_nodes)} flagged={div_flagged}")

    devs = [d for d in db.get_deviations(unexplained_only=True) if d.get('check_date') == TODAY]
    print(f"\n{len(devs)} unexplained coverage_deviation(s) today")
    for d in devs:
        print(f"  {d['scenario_key']:28s} {d['ticker'] or '':6s} {d['actual_summary']}")

    explained_devs = [d for d in db.get_deviations(check_date=TODAY) if d.get('reason') is not None]
    if explained_devs:
        print(f"\n{len(explained_devs)} explained coverage_deviation(s) today (audit trail, not actionable)")
        for d in explained_devs:
            print(f"  {d['scenario_key']:28s} {d['ticker'] or '':6s} reason: {d['reason']} "
                  f"(by {d['reason_by']} @ {d['reason_ts']})")


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

    print("\n--- 2. Real live nodes with no watch_list_candidate_link ---")
    # Informational only, not an alert -- was a manually-maintained static list
    # in a doc that went stale twice; queried fresh from the DB every run
    # instead (docs/backlog_cache.md, "watch_list_candidate_link"/"real live
    # watch_list nodes with no" item). db.get_live_nodes() already excludes
    # archived_at IS NOT NULL nodes, so a retired node never shows here either.
    live_nodes = db.get_live_nodes()
    linked_wl_ids = {link['wl_id'] for link in db.get_candidate_links()}
    unlinked = [n for n in live_nodes if n['id'] not in linked_wl_ids]
    unlinked.sort(key=lambda n: (ACCOUNT_ORDER.get(n.get('account'), 99), n['ticker']))
    if not unlinked:
        print("none -- every real live node has a recorded candidate link")
    else:
        for n in unlinked:
            print(f"  {n['ticker']:6s} {n['account'] or '':10s} wl_id={n['id']}")

    print("\n--- 2b. Real live nodes with an overlay enabled but no validation link ---")
    # Added 2026-08-19 (Task #7) -- the overlay-level sibling of the section
    # above. Calls signals_invariants.check_live_overlay_missing_validation_link
    # directly (not a re-derived query) so this print can't drift from the
    # real check's own logic -- that check also runs standalone via
    # `.venv/bin/python signals_invariants.py` (non-blocking there, see its
    # own TRACEABILITY_CHECKS comment for why it's kept out of the
    # loud/blocking run_all() path).
    overlay_gaps = signals_invariants.check_live_overlay_missing_validation_link()
    if not overlay_gaps:
        print("none -- every live node's enabled overlay has a recorded validation link")
    else:
        for g in overlay_gaps:
            print(f"  {g}")

    print("\n--- 2c. Stale unresolved pending buys (order_placed=False, signal predates today) ---")
    # Wires in scripts/check_stale_pending_buys.py's exact detection logic --
    # that script existed specifically to catch this class of gap (a pending
    # buy that never resolved and never got a real broker order) but was
    # never actually called from any nightly routine, so it only caught
    # anything if a human remembered to run it by hand. Found 2026-08-25:
    # CURE and TMF both sat stuck since 2026-08-17 (8+ days) with nothing
    # flagging it -- the standalone script would have caught it immediately,
    # it just never ran. Reuses db.get_pending_buys() directly, not a
    # re-derived query, so this can't drift from the standalone script's logic.
    today_date = datetime.now().date()
    stale_pending = []
    for pending in db.get_pending_buys():
        if pending['order_placed']:
            continue
        signal_dt = datetime.strptime(pending['signal_time'], '%Y-%m-%d %H:%M:%S')
        if signal_dt.date() >= today_date:
            continue
        stale_pending.append(pending)
    if not stale_pending:
        print("none -- no stale unresolved pending buys")
    else:
        for p in stale_pending:
            node = p['node']
            print(f"  {p['ticker']:6s} wl_id={p['wl_id']} account={node.get('account')} "
                  f"state={node.get('state')} signal_time={p['signal_time']} "
                  f"reminder_count={p['reminder_count']}")


PARTS = {'1': part1, '2': part2, '3': part3, '4': part4}

RUN_LOG_PATH = ROOT / "logs" / "evening_status_runs.log"


class _Tee:
    """Writes to both the real stream (so the terminal still sees normal output)
    and a buffer -- used to log a full copy of a run's output, not just the
    fact that it happened. Found 2026-08-19: nothing recorded whether/when this
    entirely-manual script (no crontab, no daemon wiring -- see backlog) was
    actually run on a given evening, so a real divergence could go unnoticed
    with zero way to later confirm whether anyone looked."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)

    def flush(self):
        for s in self.streams:
            s.flush()


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else 'all'
    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = _Tee(real_stdout, buf)
    try:
        if arg == 'all':
            for p in PARTS.values():
                p()
                print()
        elif arg in PARTS:
            PARTS[arg]()
        else:
            print(__doc__)
    finally:
        sys.stdout = real_stdout

    RUN_LOG_PATH.parent.mkdir(exist_ok=True)
    with open(RUN_LOG_PATH, "a") as f:
        f.write(f"\n{'=' * 70}\n=== {datetime.now().isoformat(timespec='seconds')} "
                f"run=evening_status.py {arg} ===\n{'=' * 70}\n")
        f.write(buf.getvalue())


if __name__ == '__main__':
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
