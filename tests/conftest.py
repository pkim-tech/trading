"""
Shared test utilities for strategy signal tests.
"""

import numpy as np
import pandas as pd
import pytest
from pathlib import Path
from datetime import datetime, timedelta

CACHE_DIR = Path("./cache/research")


@pytest.fixture(autouse=True)
def _no_real_slack_posts(monkeypatch):
    """SOCKET_MODE is True whenever real Slack credentials are configured in
    .env, regardless of pytest -- and SIM_MODE only prefixes message text, it
    doesn't stop the real API call. Without this, any test exercising a code
    path that calls _post_message without its own explicit mock leaks a real
    message to the real Slack channel. Every module below did `from
    signals_blocks import _post_message` (a direct name import), so each
    holds its own reference -- patching signals_blocks._post_message alone
    does not affect any of them; each must be patched individually.

    Also forces cfg.INTERACTIVE = True (2026-08-01, found live: SIM_MODE's
    default flipped to fail-safe-ON the same session, which made
    INTERACTIVE = SOCKET_MODE and not SIM_MODE compute False in the test
    environment -- several tests exercise code paths gated on INTERACTIVE
    that fall back to a console typed-input prompt when it's False, which
    tries to read real stdin under pytest and crashes. Tests should exercise
    the interactive/button-driven branch regardless of what SIM_MODE
    happens to default to in this environment -- that's a real-daemon
    launch-time concern, not a test concern)."""
    noop = lambda text, blocks=None, thread_ts=None, reply_broadcast=False, node_id=None: (None, None)
    import signals_blocks
    import signals_config as cfg
    monkeypatch.setattr(signals_blocks, '_post_message', noop)
    monkeypatch.setattr(cfg, 'INTERACTIVE', True)
    for modname in (
        'paper_trading', 'schwab_client', 'schwab_stream',
        'signals_notify', 'signals_compute', 'signals_handlers',
        'active_signals',
    ):
        mod = __import__(modname)
        if hasattr(mod, '_post_message'):
            monkeypatch.setattr(mod, '_post_message', noop)
        if hasattr(mod, 'cfg') and hasattr(mod.cfg, 'INTERACTIVE'):
            monkeypatch.setattr(mod.cfg, 'INTERACTIVE', True)


@pytest.fixture(autouse=True)
def _isolate_schwab_safety_state_files(monkeypatch, tmp_path):
    """schwab_safety.py's 8 state files (order counts, kill switch, ticker/node
    automation scope, node circuit-breaker streaks, auto-fill-detection flags)
    are plain module-level Path constants under cache/live/, written directly
    by schwab_client.py's real order-placement functions (e.g. every call
    records a record_node_streak() hit/miss) -- not just by code a test author
    would recognize as "touching schwab_safety". Found 2026-08-15: dozens of
    tests/test_fake_broker_*_scenario.py files exercise real order-placement
    functions via the fake_broker fixture (which fakes the broker connection
    but not these state paths) without ever mentioning these constants by
    name, so a plain grep for the symbol names undercounted the exposure --
    the real suite was mutating cache/live/schwab_node_breaker_state.json
    (a real live node's order_failures_streak) and schwab_order_counts.json
    on every run. Patched here, once, for every test, rather than requiring
    each test file to opt in individually (the individual-patch pattern in
    tests/test_node_circuit_breaker.py's `env` fixture still works fine
    alongside this -- it just re-patches to its own tmp_path, redundant but
    harmless)."""
    import schwab_safety
    for name in (
        'STATE_PATH', 'KILL_SWITCH_PATH', 'TICKER_AUTOMATION_PATH',
        'AUTO_FILL_DETECTION_PATH', 'AUTOMATION_SCOPE_STATE_PATH',
        'NODE_AUTOMATION_PATH', 'NODE_BREAKER_PATH',
        'NODE_AUTO_FILL_DETECTION_PATH',
    ):
        monkeypatch.setattr(schwab_safety, name, tmp_path / f"{name.lower()}.json")


def _synthetic_timestamps(days=90):
    """Same hourly-bar timestamp grid make_synthetic_csv() writes to disk --
    shared so fake_position() can place signal_time exactly N bars back from
    the last bar (bars-ago, not wall-clock-hours-ago: the fixture data lives
    on a fixed 2025 date range, and _bars_held() counts rows, not elapsed
    calendar time, so wall-clock deltas don't land in the right place)."""
    dates = pd.bdate_range("2025-01-01", periods=days)
    market_hours = [9, 10, 11, 12, 13, 14, 15]
    return [
        pd.Timestamp(f"{d.date()} {h:02d}:30:00")
        for d in dates for h in market_hours
    ]


def make_synthetic_csv(ticker, last_close, days=90):
    """
    Writes a synthetic hourly CSV to cache/research/{ticker}_1h.csv.
    Prices are ~100 with low variance; last bar is set to last_close.
    days=90 ensures enough history for window=20 + 50-day trend filter.
    Call cleanup_csv() after the test.
    """
    np.random.seed(0)
    timestamps = _synthetic_timestamps(days)
    prices = 100.0 + np.random.normal(0, 0.3, len(timestamps))
    prices[-1] = last_close

    df = pd.DataFrame({'Close': prices}, index=timestamps)
    df.index.name = 'Datetime'
    df.to_csv(CACHE_DIR / f"{ticker}_1h.csv")


def cleanup_csv(ticker):
    path = CACHE_DIR / f"{ticker}_1h.csv"
    path.unlink(missing_ok=True)


def fake_node(ticker, strategy, window=20, tp=10, sl=5, hold=56):
    return {
        'ticker':         ticker,
        'strategy':       strategy,
        'version':        'test',
        'window':         window,
        'take_profit':    tp,
        'stop_loss':      sl,
        'max_hold_hours': hold,
    }


def fake_position(ticker, strategy, entry_price, hours_ago=10, tp=10, sl=5, hold=56, window=20, days=90):
    """hours_ago is really bars-ago -- see _synthetic_timestamps()."""
    timestamps = _synthetic_timestamps(days)
    entry_time = timestamps[-1 - hours_ago] if hours_ago < len(timestamps) else timestamps[0]
    return {
        'id':             999,
        'ticker':         ticker,
        'strategy':       strategy,
        'version':        'test',
        'window':         window,
        'take_profit':    tp,
        'stop_loss':      sl,
        'max_hold_hours': hold,
        'signal_price':   entry_price,
        'signal_time':    entry_time.strftime('%Y-%m-%d %H:%M:%S'),
        'entry_price':    entry_price,
        'entry_time':     entry_time.strftime('%Y-%m-%d %H:%M:%S'),
    }
