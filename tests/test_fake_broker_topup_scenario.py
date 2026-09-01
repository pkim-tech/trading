"""Fourth fake_broker scenario: post_fill_topup (real scenario_key 'top_up',
_reconcile_fill). Both real historical attempts (RETL 2026-07-29, LABU
2026-07-24) were legitimately blocked by real guards (signal-window gate,
daily-order-cap) -- not malfunctions, just never yet observed succeeding.
This pins down whether the GOOD path (real fill under target notional, top-up
buy placed and recorded) actually works when nothing blocks it -- something
no real event has ever proven."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import signals_config
import signals_db
import signals_notify
import schwab_safety

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_TOPUP_SCENARIO'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    monkeypatch.setattr(schwab_safety, 'STATE_PATH', tmp_path / "schwab_order_counts.json")
    monkeypatch.setattr(schwab_safety, 'KILL_SWITCH_PATH', tmp_path / "schwab_kill_switch.json")
    monkeypatch.setattr(schwab_safety, 'TICKER_AUTOMATION_PATH', tmp_path / "schwab_ticker_automation.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTOMATION_PATH', tmp_path / "schwab_node_automation.json")
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_node_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    # 10:30 ET -- inside the real (10,25,10,40) signal window, so the top-up
    # buy's own signal-window guard passes on its own merits, not via
    # is_gap_correction bypassing it -- this is the ordinary in-window case,
    # matching most real fills.
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 7, 29, 10, 30))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0)
    with signals_db._conn() as c:
        # $800 matches soxl_ira's real notional_cap (schwab_safety.ACCOUNTS) --
        # sizing the scenario to actually fit within the real guard, not an
        # arbitrary bigger number that would itself get correctly blocked.
        c.execute("UPDATE watch_list SET account='soxl_ira', starting_notional=800 WHERE ticker=?",
                   (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def test_topup_places_real_order_and_updates_position_when_unblocked(env, fake_broker, monkeypatch):
    node = _node()
    signal_price = 10.15
    fill_price = 10.00
    initial_shares = 40.0  # 40 * $10 = $400, well under the $800 target notional

    sig = {'current_price': signal_price, 'last_bar': datetime(2026, 7, 29, 10, 25)}
    signals_db.add_pending_buy(node, sig, channel='C0TEST', ts='1234.5', order_id=8888888888)
    signals_db.mark_pending_buy_placed_by_wl_id(node['id'])

    fake_broker.set_quote(TICKER, last=fill_price, bid=fill_price, ask=fill_price + 0.01)

    # --- act: real fill reconciliation, which internally calls _reconcile_fill ---
    signals_notify._reconcile_buy_fill(TICKER, fill_price=fill_price, filled_shares=initial_shares,
                                        wl_id=node['id'])

    # --- post-state: full check ---
    pos = signals_db.get_open_position(TICKER)
    assert pos is not None

    target_notional = 800.0
    delta = target_notional - (fill_price * initial_shares)
    expected_topup_shares = int(delta // fill_price)
    assert expected_topup_shares > 0, "test setup should genuinely need a top-up"

    expected_total_shares = initial_shares + expected_topup_shares
    assert pos['shares'] == expected_total_shares, (
        f"expected position to reflect the top-up: {initial_shares} initial + "
        f"{expected_topup_shares} top-up = {expected_total_shares}, got {pos['shares']}"
    )

    ticker_orders = [o for o in fake_broker.orders.values()
                      if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER]
    topup_orders = [o for o in ticker_orders if o['orderType'] == 'MARKET'
                     and o['orderLegCollection'][0]['instruction'] == 'BUY'
                     and o['status'] == 'FILLED']
    assert len(topup_orders) == 1, (
        f"expected exactly one real top-up MARKET BUY placed at the broker, found: "
        f"{[(o['orderId'], o['orderType'], o['status']) for o in ticker_orders]}"
    )
    assert topup_orders[0]['orderLegCollection'][0]['quantity'] == expected_topup_shares

    topup_events = signals_db.get_coverage_events(scenario_key='top_up')
    assert any(e['ticker'] == TICKER and e['result'] == 'placed' for e in topup_events), (
        "expected a top_up coverage_event with result='placed' -- the first-ever "
        "proof (real or fake-venue) that this path succeeds when unblocked, "
        "distinct from both real historical attempts which were legitimately "
        "blocked (signal-window gate, daily-order-cap)"
    )

    # --- close the position and confirm trade_log.shares reflects the TOPPED-UP
    # total, not the original pre-top-up fill (2026-08-15 fix: log_trade_exit now
    # takes the position's current shares at close time, since close_position()
    # previously never re-synced trade_log.shares after a same-day top_up -- found
    # live on RETL, trade_log id=97 recorded 41 vs. the real 49 shares actually
    # traded, trading_incidents id=8). This is the gap that let 6 real top-ups
    # ship unnoticed: this test previously only checked pos['shares'] above, never
    # carried the position through to close.
    closed = signals_db.close_position(pos['id'], exit_signal_price=fill_price, exit_price=fill_price,
                                        exit_time=datetime(2026, 7, 29, 11, 0), exit_reason='TIME')
    assert closed
    with signals_db._conn() as c:
        row = c.execute("SELECT shares FROM trade_log WHERE id = ?", (pos['trade_log_id'],)).fetchone()
    assert row is not None
    assert row[0] == expected_total_shares, (
        f"trade_log.shares should reflect the topped-up total ({expected_total_shares}), "
        f"got {row[0]} -- the exact staleness bug this test now guards against"
    )


def test_drought_overlay_topup_compounds_off_the_shared_pools_most_recent_exit(env, fake_broker, monkeypatch):
    """Real incident #15 CRITICAL finding (round 2, 2026-08-31) + follow-up
    design correction (2026-09-01, user's explicit decision): _reconcile_fill's
    top-up used to size a drought-overlay fill off the flat starting_notional
    column directly, defeating the entry-order override fix at fill time.
    Fixed by letting _reconcile_fill's own target_notional default
    (_last_sale_recovery(node)) apply for a drought fill same as core,
    instead of passing an explicit flat override.

    Design correction: _last_sale_recovery does NOT scope by leg -- core and
    drought share ONE capital pool per node ("if I just sold SPY for $5000
    then that's available capital for drought or core," user's exact words).
    So this test seeds a CORE trade_log row (smaller proceeds, older) and a
    DROUGHT_OVERLAY row (larger proceeds, exiting LATER) on the same node,
    and proves the top-up's target_notional flows through the real
    _last_sale_recovery(node) default (drought's $2800, the most recent
    exit) rather than the old flat-column special case. NOTE this specific
    setup does NOT by itself distinguish "shared pool, most-recent-wins"
    from "still scoped to position_source='drought_overlay'" -- both would
    return $2800 here, since drought is both the newer AND the
    would-be-scoped leg. That distinction (shared pool vs. per-leg scoping)
    is covered separately by tests/test_starting_notional_override.py's
    test_core_fill_compounds_off_a_more_recent_drought_exit_shared_pool. This
    test's own job is narrower and still real: proving the top-up no longer
    bypasses _last_sale_recovery entirely (round 2's CRITICAL finding). A
    regression back to the old flat-column read would see the fill
    ($2,020) as already exceeding a much smaller target and skip the top-up
    (or worse, fire a false overspend alert) instead of correctly topping up
    toward $2,800."""
    node = _node()
    with signals_db._conn() as c:
        # core proceeds = 300, exits FIRST (older) -- deliberately SMALLER
        # than the fill notional below, so a regression to reading this
        # stale/wrong row would see the fill as already an OVERSPEND, not a
        # valid top-up candidate -- a much stronger regression signal than a
        # merely-smaller topup.
        c.execute("""
            INSERT INTO trade_log
                (ticker, strategy, version, window, stop_loss, max_hold_hours, account,
                 signal_price, signal_time, entry_price, entry_time, entry_drift_pct,
                 exit_price, exit_time, exit_reason, shares, is_dry_run_sim, position_source)
            VALUES (?, 'TrailingBothZScoreBreakout', 'test', 10, 1, 105, 'soxl_ira',
                    9.8, ?, 9.8, ?, 0.0, 10.0, ?, 'SL', 30, 0, 'core')
        """, (TICKER, datetime(2026, 7, 27, 10, 30).isoformat(), datetime(2026, 7, 27, 10, 30).isoformat(),
              datetime(2026, 7, 27, 15, 30).isoformat()))
        # drought_overlay proceeds = 2800, exits SECOND (more recent) -- the
        # real shared-pool target this top-up must use.
        c.execute("""
            INSERT INTO trade_log
                (ticker, strategy, version, window, stop_loss, max_hold_hours, account,
                 signal_price, signal_time, entry_price, entry_time, entry_drift_pct,
                 exit_price, exit_time, exit_reason, shares, is_dry_run_sim, position_source)
            VALUES (?, 'TrailingBothZScoreBreakout', 'test', 10, 1, 105, 'soxl_ira',
                    27.8, ?, 27.8, ?, 0.0, 28.0, ?, 'SL', 100, 0, 'drought_overlay')
        """, (TICKER, datetime(2026, 7, 28, 10, 30).isoformat(), datetime(2026, 7, 28, 10, 30).isoformat(),
              datetime(2026, 7, 28, 15, 30).isoformat()))
        c.commit()

    fill_price = 50.5
    initial_shares = 40.0  # 40 * $50.5 = $2,020 -- exceeds core's $300 target, under the shared pool's $2,800

    sig = {'current_price': 50.0, 'last_bar': datetime(2026, 7, 29, 10, 25)}
    signals_db.add_pending_buy(node, sig, channel='C0TEST', ts='1234.5', order_id=7777777777,
                                position_source='drought_overlay', drought_confirm_days=3,
                                drought_vol_gate=None, drought_gap_start='2026-07-20 09:30:00',
                                drought_vol_pctile=None)
    signals_db.mark_pending_buy_placed_by_wl_id(node['id'])

    fake_broker.set_quote(TICKER, last=fill_price, bid=fill_price, ask=fill_price + 0.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    signals_notify._reconcile_buy_fill(TICKER, fill_price=fill_price, filled_shares=initial_shares,
                                        wl_id=node['id'])

    pos = signals_db.get_open_position(TICKER)
    assert pos is not None
    assert pos['position_source'] == 'drought_overlay'

    shared_pool_target_notional = 2800.0
    delta = shared_pool_target_notional - (fill_price * initial_shares)
    expected_topup_shares = int(delta // fill_price)
    assert expected_topup_shares > 0, "test setup should genuinely need a top-up against the shared-pool target"
    expected_total_shares = initial_shares + expected_topup_shares

    assert pos['shares'] == expected_total_shares, (
        f"drought top-up did not compound off the shared pool's most recent exit "
        f"(target={shared_pool_target_notional}) -- got {pos['shares']} shares, expected "
        f"{expected_total_shares}. A regression back to the flat starting_notional column, or to "
        f"the stale/older core row, would produce a different (likely smaller, or "
        f"zero/overspend-flagged) result here."
    )
