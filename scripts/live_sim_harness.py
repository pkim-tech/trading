#!/usr/bin/env python3
"""
Non-interactive coverage harness for active_signals.py / signals_*.py.

Extends scripts/live_sim.py's interactive REPL (manual, one bar at a time)
with a scriptable pass that calls the real orchestration functions directly
-- _scan_pinned_entry, _scan_pinned_exit_arm, check_sell_condition (TIME
exit), signals_notify._reconcile_fill (forced top-up shortfall),
signals_notify.check_gap_resize, and both real entry paths (trailing-buy via
the pinned path, market-buy via the ambient scan) -- against an isolated sim
DB, in seconds, so it's the standard verification step for a
active_signals.py/signals_*.py change (the same role backtest-change-rollout
plays for kernel changes; decided 2026-07-22, see docs/backlog_cache.md).

Real z-score math runs for real (writes a real synthetic CSV to
cache/research/, same convention as tests/conftest.py's make_synthetic_csv)
-- only the broker boundary (schwab_client's price/order/balance calls) is
stubbed, since every scenario here is about verifying *wiring* (dedup,
mode-gating, arm/re-arm state, top-up sizing, gap-resize amounts, TIME
trigger), not re-verifying signal math (that's backtest-change-rollout's job).
All accounts used here are the existing dry_run=True fixtures, so every order
placement takes schwab_client's dry_run branch (no real API call) -- but
price/balance/order-book *reads* hit the real API regardless of dry_run, so
those are explicitly stubbed below (found while building this: get_account_
balance is called on every BUY's cash check even in dry_run).

Usage:
    python scripts/live_sim_harness.py [--sim-db PATH] [--keep-csvs]

Exits 0 if every scenario passes, 1 otherwise.
"""
import argparse
import os
import shutil
import sys
import tempfile
import time as _time
import traceback
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

parser = argparse.ArgumentParser()
parser.add_argument("--sim-db", default="./cache/live/trading_sim_harness.db")
parser.add_argument("--keep-csvs", action="store_true", help="don't delete synthetic CSVs after the run (debugging)")
args, _ = parser.parse_known_args()

os.environ["TRADING_DB_PATH"] = args.sim_db
os.environ["SIM_MODE"] = "1"
# Isolates schwab_safety.py's real, hardcoded state files (order counts, kill
# switch, ticker-automation pause, ...) the same way TRADING_DB_PATH isolates
# the DB -- found the hard way (2026-07-23): before SCHWAB_STATE_DIR existed,
# an earlier version of this harness wrote real dry-run BUY attempts straight
# into the real schwab_order_counts.json across repeated runs, driving the
# real 'ira' account's daily_order_cap counter to its actual limit. A fresh,
# empty dir here means kill_switch_engaged() reads as False (no file = not
# engaged) with no need to separately stub it.
_state_dir = Path(tempfile.mkdtemp(prefix="live_sim_harness_state_"))
os.environ["SCHWAB_STATE_DIR"] = str(_state_dir)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import active_signals as A  # noqa: E402  (must import after env vars are set)
import signals_config as cfg  # noqa: E402
import signals_compute as compute  # noqa: E402
import signals_db as db  # noqa: E402
import signals_notify as notify  # noqa: E402
from signals_helpers import _pos_key  # noqa: E402
import schwab_client  # noqa: E402
import schwab_safety  # noqa: E402

RESEARCH_DIR = cfg.RESEARCH_DIR


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _synthetic_timestamps(days=90):
    """Same grid as tests/conftest.py's make_synthetic_csv, kept in sync
    deliberately -- both need window=20 + trend-filter history."""
    dates = pd.bdate_range("2025-01-01", periods=days)
    market_hours = [9, 10, 11, 12, 13, 14, 15]
    return [pd.Timestamp(f"{d.date()} {h:02d}:30:00") for d in dates for h in market_hours]


def write_synthetic_csv(ticker, last_close, days=90):
    """Real CSV at RESEARCH_DIR, same convention as tests/conftest.py's
    make_synthetic_csv -- lets the real z-score/indicator pipeline run
    unmodified rather than stubbing compute_buy_signal's math."""
    np.random.seed(0)
    timestamps = _synthetic_timestamps(days)
    prices = 100.0 + np.random.normal(0, 0.3, len(timestamps))
    prices[-1] = last_close
    df = pd.DataFrame({'Close': prices}, index=timestamps)
    df.index.name = 'Datetime'
    df.to_csv(RESEARCH_DIR / f"{ticker}_1h.csv")
    return timestamps


def cleanup_csv(ticker):
    if not args.keep_csvs:
        (RESEARCH_DIR / f"{ticker}_1h.csv").unlink(missing_ok=True)


def make_node(ticker, strategy, **overrides):
    kwargs = dict(
        window=20, take_profit=10, stop_loss=5, max_hold_hours=56,
        state='live', trail_buy_pct=1.0, trail_pct=1.0, entry_timing='close',
        starting_notional=50000,
    )
    kwargs.update(overrides)
    db.add_node(ticker, strategy, 'harness', **kwargs)
    with db._conn() as c:
        c.execute("UPDATE watch_list SET account='roth' WHERE ticker=?", (ticker,))
        c.commit()
    return [n for n in db.get_watchlist() if n['ticker'] == ticker][0]


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_pinned_entry_trailing_buy(posted):
    """Real entry path #1: trailing-buy via the pinned open-check scan
    (_scan_pinned_entry) -- the live path for all 10 watchlist-65 v5 nodes."""
    ticker = 'ZHARN1'
    last_close = 100.0 - 6.0  # ~20 std devs below the mean -> reliable BUY z-score
    write_synthetic_csv(ticker, last_close)
    node = make_node(ticker, 'TrailingBothZScoreBreakout', entry_timing='open_check')
    with patch.object(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {ticker}), \
         patch.object(schwab_client, 'get_session_open_price', return_value=(last_close, True)):
        A._scan_pinned_entry(9, 30, [node], set(), A._position_keys_by_book([], []))
    pending = [p for p in db.get_pending_buys() if p['ticker'] == ticker]
    assert pending, "expected a pending_buys row after a trailing-buy BUY signal"
    assert pending[0]['order_placed'], "trailing-buy order should auto-place (dry_run) and mark placed"
    # The harness's 'roth' account is dry_run (trading_enabled=False) -- as of
    # 2026-08-08, dry_run nodes get zero real-time Slack for the routine BUY
    # SIGNAL post (has_capital_at_stake is always False for a dry_run node),
    # even though the underlying entry mechanics (pending_buys row, order
    # marked placed) are deliberately unconditional and still fire, asserted
    # above. Confirms the mute, not just its absence.
    assert not any('BUY SIGNAL' in m for m in posted), \
        "BUY SIGNAL should be muted for this dry_run node, not posted"
    assert any('TRAILING BUY' in m for m in posted), "expected the dry-run trailing-buy order message"


def scenario_pinned_exit_arm(posted):
    """Real exit-arm path: _scan_pinned_exit_arm -> check_sell_condition arms
    trailing, notify_trailing_activated persists it -- regression coverage
    for the 2026-07-22 stale-clobber bug (arming must survive the merge, not
    silently revert to unarmed on the next bar). take_profit doubles as
    TrailingBothZScoreBreakout's arm-sell threshold (signals_db._tp_or_arm_pct)
    -- passed small (1%) so a modest bar move reliably arms it. Uses a
    hand-built OHLC df (matches tests/test_part4_entry_trigger.py's pattern)
    instead of the Close-only synthetic CSV, since this scenario needs real
    Open/Low/High, not z-score history."""
    ticker = 'ZHARN2'
    node = make_node(ticker, 'TrailingBothZScoreBreakout', take_profit=1, trail_pct=1)
    entry_price = 100.0
    signal_time = datetime(2026, 1, 2, 9, 30)
    db.open_position(node, signal_price=entry_price, signal_time=signal_time,
                      entry_price=entry_price, entry_time=signal_time, shares=10)
    df = pd.DataFrame(
        {'Open': [100.0], 'Close': [104.0], 'Low': [99.5], 'High': [105.0]},
        index=pd.to_datetime(['2026-01-02 10:30:00']),
    )
    # Seed last_seen_bar to the entry bar first -- a real poll immediately
    # after open_position would already mark that bar as seen (2026-07-27
    # fix: a position's first-ever check must never be graded as 'a bar just
    # closed' against a bar that could contain pre-entry price history, so
    # it's deferred instead of firing on a fresh {}). This arm check then
    # correctly reads as a genuinely new bar closing.
    pos_now = db.get_open_position(ticker)
    last_seen_bar = {_pos_key(pos_now): pd.Timestamp('2026-01-02 09:30:00')}
    with patch.object(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {ticker}), \
         patch.object(A, '_load_cache', lambda t, _df=df: (_df, None) if t == ticker else (None, None)):
        A._scan_pinned_exit_arm(db.get_open_positions(), set(), last_seen_bar)
    pos = db.get_open_position(ticker)
    # Both fields, not just 'trailing' -- the 2026-07-22 clobber bug dropped
    # 'peak' alongside 'trailing' (a merge onto a stale pre-arm copy), so
    # checking 'trailing' alone would pass even if a future partial-clobber
    # regression preserved 'trailing' but still dropped 'peak'.
    assert pos['trail_state'].get('trailing'), "expected trail_state.trailing=True to persist after arming"
    assert pos['trail_state'].get('peak') is not None, "expected trail_state.peak to persist after arming"


def scenario_reconcile_fill_topup(posted):
    """Real Part 3 branch C: a fill that under-spent target_notional should
    trigger a real (dry_run) top-up market buy sized off the shortfall."""
    ticker = 'ZHARN3'
    node = make_node(ticker, 'TrailingBothZScoreBreakout', starting_notional=50000)
    entry_time = datetime.now()
    db.open_position(node, signal_price=100.0, signal_time=entry_time,
                      entry_price=100.0, entry_time=entry_time, shares=100)  # only $10k of a $50k target
    with patch.object(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {ticker}):
        notify._reconcile_fill(node, fill_price=100.0, filled_shares=100)
    pos = db.get_open_position(ticker)
    assert pos['shares'] > 100, f"expected top-up to increase shares beyond 100, got {pos['shares']}"
    assert any('top-up' in m.lower() for m in posted), "expected the top-up Slack message"


def scenario_gap_resize(posted):
    """Real Part 3 branch B: an overnight gap that already cleared the
    trailing-buy trigger should cancel-and-replace with a MARKET order."""
    ticker = 'ZHARN4'
    node = make_node(ticker, 'TrailingBothZScoreBreakout', trail_buy_pct=1.0, starting_notional=50000)
    sig = {'current_price': 100.0, 'last_bar': pd.Timestamp.now()}
    db.add_pending_buy(node, sig, channel=None, ts=None, order_id=None)
    db.mark_pending_buy_placed(ticker)
    current_price = 105.0
    # check_gap_resize() iterates every pending_buys row in the DB, not just
    # this scenario's -- a ticker-agnostic constant return_value here would
    # also spuriously clear any other scenario's still-pending row sharing
    # this same sim DB (e.g. scenario 1's ZHARN1), so gate it to this ticker.
    with patch.object(schwab_client, 'get_current_price',
                       side_effect=lambda t: current_price if t == ticker else 0.0), \
         patch.object(schwab_client, 'get_account_balance', return_value=1_000_000.0), \
         patch.object(schwab_safety, '_open_orders', return_value=[]), \
         patch.object(schwab_safety, '_all_orders', return_value=[]), \
         patch.object(schwab_client, 'place_equity_buy', wraps=schwab_client.place_equity_buy) as spy:
        notify.check_gap_resize()
    assert any('overnight gap cleared' in m for m in posted), "expected the gap-cleared Slack message"
    # Assert the actual replacement order, not just that some Slack text fired --
    # a regression that mis-types or mis-sizes the replacement could still post
    # a correct-looking message while placing the wrong order underneath.
    spy.assert_called_once()
    call_account, call_ticker, call_shares, call_price = spy.call_args.args
    assert call_account == 'roth'
    assert call_ticker == ticker
    assert spy.call_args.kwargs.get('is_gap_correction') is True
    padded_price = current_price * (1 + notify._GAP_RESIZE_PAD_PCT / 100)
    expected_shares = int(50000 // padded_price)
    assert call_shares == expected_shares, f"expected {expected_shares} shares, got {call_shares}"
    assert call_price == current_price


def scenario_time_exit(posted):
    """Real TIME-exit trigger: a position held past max_hold_hours (in bars,
    not wall-clock hours) with price sitting neutrally inside SL/TP should
    exit with reason='TIME', not SL/TP."""
    ticker = 'ZHARN5'
    timestamps = write_synthetic_csv(ticker, 100.0)
    node = make_node(ticker, 'TrailingBothZScoreBreakout', max_hold_hours=5, fixed_sl_override=50)
    signal_time = timestamps[-20]  # far more than 5 bars ago
    db.open_position(node, signal_price=100.0, signal_time=signal_time,
                      entry_price=100.0, entry_time=signal_time, shares=10)
    pos = db.get_open_position(ticker)
    df, _ = A._load_cache(ticker)
    bar = df.iloc[-1]
    reason, target, just_activated = A.check_sell_condition(
        pos, float(bar['Close']), datetime.now(), at_bar_close=True,
        low=float(bar['Close']), high=float(bar['Close']), open_price=float(bar['Close']), df_hourly=df,
    )
    assert reason == 'TIME', f"expected reason='TIME', got {reason!r}"


def scenario_ambient_market_buy_entry(posted):
    """Real entry path #2: an automated market buy (non-trailing-buy
    strategy, automation-enabled ticker) via the ambient _scan_buy_signals ->
    notify_buy_signal -> _attempt_automated_market_buy -> synchronous
    fast-confirm -> _reconcile_buy_fill -> stop-loss placement chain."""
    ticker = 'ZHARN6'
    last_close = 100.0 - 6.0
    write_synthetic_csv(ticker, last_close)
    node = make_node(ticker, 'TrailingExitZScoreBreakout', fixed_sl_override=5, state='live')
    with patch.object(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {ticker}), \
         patch.object(schwab_client, 'get_account_balance', return_value=1_000_000.0), \
         patch.object(schwab_safety, '_open_orders', return_value=[]), \
         patch.object(schwab_safety, '_all_orders', return_value=[]), \
         patch.object(schwab_client, 'get_filled_order', return_value={'price': last_close, 'quantity': 100}):
        A._scan_buy_signals([node], set(), A._position_keys_by_book([], []))
    pos = db.get_open_position(ticker)
    assert pos is not None, "expected an open position after the automated market-buy fill-confirm chain"
    assert any('auto-detected fill' in m.lower() for m in posted), "expected the fill-detected Slack message"


def scenario_dry_run_sim_cycle(posted):
    """Real dry_run fill-synthesis path (added 2026-07-26): a mode='live' node
    in a dry_run=True account never gets a real broker fill event
    (schwab_client short-circuits before the API call), so
    update_dry_run_buys/_fill_dry_run_buy synthesize the bounce-fill BUY and
    check_dry_run_sim_sells synthesizes the close, against real price data,
    into the real open_positions/trade_log tables (tagged is_dry_run_sim=1) --
    the fix for the canary/dry_run "no closed trade found" false-positive
    coverage_deviations. Exercises the full bounce-fill BUY followed by an
    SL-triggered close, since neither scripts/live_sim.py's manual REPL nor
    tests/test_dry_run_sim.py's unit tests drive this path end-to-end through
    the real active_signals.py/signals_notify.py wiring."""
    ticker = 'ZHARN7'
    node = make_node(ticker, 'TrailingBothZScoreBreakout', trail_buy_pct=1.0, fixed_sl_override=5)
    sig = {'current_price': 100.0, 'last_bar': datetime.now()}
    db.add_pending_buy(node, sig, channel=None, ts=None)

    fill_price = 102.0
    with patch.object(compute, '_current_price', return_value=(fill_price, None)):
        notify.update_dry_run_buys()

    pos = db.get_open_position(ticker)
    assert pos is not None, "expected a synthesized open position after update_dry_run_buys"
    assert pos.get('is_dry_run_sim'), "expected the synthesized position to be tagged is_dry_run_sim"
    assert not db.get_pending_buys(), "expected the pending_buys row to clear after the synthetic fill"
    assert any('would have filled' in m.lower() for m in posted), "expected the dry-run fill Slack message"

    # SL breach well below entry_price(102) * (1 - 5%) -> reason='SL'. Hand-built
    # OHLC df (same pattern as scenario_pinned_exit_arm) since check_dry_run_sim_sells's
    # at_bar_close path needs real Open/Low/High, not the Close-only synthetic CSV.
    df = pd.DataFrame(
        {'Open': [95.0], 'Close': [80.0], 'Low': [78.0], 'High': [96.0]},
        index=pd.to_datetime(['2026-01-02 10:30:00']),
    )
    # Seed last_seen_bar to the entry bar first -- a real poll immediately after
    # update_dry_run_buys opens the position would already mark that bar as seen
    # (2026-07-27 fix: a position's first-ever check must never be graded as
    # 'a bar just closed' against a bar that could contain pre-entry price
    # history, so it's deferred instead of firing on last_seen_bar={}). This
    # SL-breach check then correctly reads as a genuinely new bar closing.
    last_seen_bar = {_pos_key(pos): pd.Timestamp('2026-01-02 09:30:00')}
    with patch.object(A, '_load_cache', lambda t, _df=df: (_df, None) if t == ticker else (None, None)):
        notify.check_dry_run_sim_sells(last_seen_bar, set(), A._load_cache)

    assert db.get_open_position(ticker) is None, "expected the synthesized position to close on SL breach"
    today = datetime.now().strftime('%Y-%m-%d')
    closed = db.get_closed_trades_for_ticker_on_date(ticker, today)
    assert closed, "expected a trade_log row for the synthesized close"
    assert closed[0]['is_dry_run_sim'], "expected the closed trade to be tagged is_dry_run_sim"
    assert closed[0]['exit_reason'] == 'SL', f"expected exit_reason='SL', got {closed[0]['exit_reason']!r}"
    assert any('would have closed' in m.lower() for m in posted), "expected the dry-run close Slack message"


SCENARIOS = [
    scenario_pinned_entry_trailing_buy,
    scenario_pinned_exit_arm,
    scenario_reconcile_fill_topup,
    scenario_gap_resize,
    scenario_time_exit,
    scenario_ambient_market_buy_entry,
    scenario_dry_run_sim_cycle,
]


def main():
    try:
        Path(args.sim_db).unlink()
    except FileNotFoundError:
        pass
    db.ensure_tables()

    tickers = ['ZHARN1', 'ZHARN2', 'ZHARN3', 'ZHARN4', 'ZHARN5', 'ZHARN6', 'ZHARN7']
    failures = []
    t0 = _time.time()

    for scenario in SCENARIOS:
        name = scenario.__name__
        posted = []
        noop_post = lambda text, *args, _posted=posted, **kwargs: (_posted.append(text), (None, None))[1]
        with ExitStack() as stack:
            for mod in (A, notify, schwab_client):
                if hasattr(mod, '_post_message'):
                    stack.enter_context(patch.object(mod, '_post_message', noop_post))
            # Fixed in-window time (matches _SIGNAL_WINDOWS/_OPEN_CHECK_WINDOWS) --
            # schwab_safety.check_order's BUY window gate otherwise blocks every
            # order placed by this harness outside real market hours.
            stack.enter_context(patch.object(schwab_safety, '_now', lambda: datetime(2026, 7, 24, 10, 30)))
            # Universal broker-network safety net -- found the hard way while
            # building this: scenario 1 initially forgot to stub get_account_balance
            # (called on every real BUY's cash check regardless of dry_run) and it
            # fell through to a real, live Schwab OAuth login prompt. Every
            # schwab_client function that hits the network gets a safe default here
            # so a scenario that forgets to override one fails loudly (a clear
            # RuntimeError) instead of silently reaching the real API. Order
            # *placement* itself (place_equity_buy, place_trailing_buy/_sell,
            # place_stop_loss) is not stubbed -- those already take the dry_run
            # branch inside schwab_client and never touch the network, as long as
            # every account used here stays in schwab_safety.ACCOUNTS with dry_run=True.
            def _blocked_network_call(name):
                def _fn(*a, **kw):
                    raise RuntimeError(
                        f"scenario reached real schwab_client.{name}() unmocked -- "
                        f"add an explicit patch.object for this scenario"
                    )
                return _fn
            stack.enter_context(patch.object(schwab_client, 'get_account_balance', lambda account: 1_000_000.0))
            stack.enter_context(patch.object(schwab_safety, '_open_orders', lambda account: []))
            stack.enter_context(patch.object(schwab_safety, '_all_orders', lambda account: []))
            for fn_name in ('get_session_open_price', 'get_current_price', 'get_real_position',
                            'get_filled_order', 'cancel_order'):
                stack.enter_context(patch.object(schwab_client, fn_name, _blocked_network_call(fn_name)))
            try:
                scenario(posted)
                print(f"  PASS  {name}")
            except Exception as e:
                failures.append((name, e, traceback.format_exc()))
                print(f"  FAIL  {name}: {e}")

    for ticker in tickers:
        cleanup_csv(ticker)
    try:
        Path(args.sim_db).unlink()
    except FileNotFoundError:
        pass
    shutil.rmtree(_state_dir, ignore_errors=True)

    elapsed = _time.time() - t0
    print(f"\n{len(SCENARIOS) - len(failures)}/{len(SCENARIOS)} scenarios passed in {elapsed:.1f}s")
    if failures:
        print("\nFailures:")
        for name, e, tb in failures:
            print(f"--- {name} ---\n{tb}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
