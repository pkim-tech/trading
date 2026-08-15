"""Tests for signals_notify._throttled -- the shared cooldown gate that four
live-alert paths (_RECONCILE_ALERTED, _RECONCILE_FETCH_FAIL_ALERTED,
_STALE_PRICE_ALERTED, _ENTRY_ABANDON_ALERTED) each used to hand-roll
separately.

A bug here is bidirectionally dangerous in live trading: too permissive means
Slack alert spam that buries a real trade alert, too aggressive means a real
UNPROTECTED-position/broker-outage alert is silently swallowed. So the window
boundary is driven with a fake clock rather than real sleeps -- exact, and no
wall-clock flake.

No DB access is involved (the gate is pure in-memory), but DB_PATH is still
pointed at a temp file so an accidental future import-time write can't reach
the real cache/live/trading_live.db.
"""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_notify


@pytest.fixture
def clock(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")

    fake = {'t': 1_000_000.0}
    monkeypatch.setattr(signals_notify.time, 'time', lambda: fake['t'])
    return fake


def test_fires_once_then_suppresses_then_fires_again_after_window(clock):
    store: dict[str, float] = {}

    assert signals_notify._throttled(store, 'k', 900) is True, "first call must fire"
    assert signals_notify._throttled(store, 'k', 900) is False, "immediate repeat must be suppressed"

    clock['t'] += 899.0
    assert signals_notify._throttled(store, 'k', 900) is False, "still inside the window"

    clock['t'] += 1.0  # exactly 900s since the firing
    assert signals_notify._throttled(store, 'k', 900) is True, "window elapsed, must fire again"
    assert signals_notify._throttled(store, 'k', 900) is False, "and the new firing restarts the window"


def test_distinct_keys_have_independent_cooldowns(clock):
    """Guards the real reason each call site uses a compound key: a genuinely
    different condition on the same position/node must alert immediately
    rather than be silenced by an unrelated condition's cooldown."""
    store: dict[str, float] = {}

    assert signals_notify._throttled(store, '42:shares', 900) is True
    assert signals_notify._throttled(store, '42:missing_trailing_sell', 900) is True
    assert signals_notify._throttled(store, '42:shares', 900) is False
    assert signals_notify._throttled(store, '42:missing_trailing_sell', 900) is False


def test_separate_stores_do_not_share_state(clock):
    """The four alert domains keep separate dicts on purpose -- an account
    name and a position id live in different key spaces and must never be
    able to suppress each other."""
    a: dict[str, float] = {}
    b: dict[str, float] = {}

    assert signals_notify._throttled(a, 'ira', 900) is True
    assert signals_notify._throttled(b, 'ira', 900) is True, "other store must be unaffected"
    assert signals_notify._throttled(a, 'ira', 900) is False


def test_firing_is_recorded_before_the_caller_posts(clock):
    """_throttled records the firing itself, so a caller whose _post_message
    raises still consumes its cooldown instead of retrying unbounded every
    poll cycle."""
    store: dict[str, float] = {}

    assert signals_notify._throttled(store, 'k', 900) is True
    assert store['k'] == clock['t'], "timestamp recorded at the moment of the firing"
    assert signals_notify._throttled(store, 'k', 900) is False


def test_respects_per_domain_cooldown_length(clock):
    """The stale-price and entry-abandon domains pass their own constants --
    the window must come from the argument, not a module-wide default."""
    store: dict[str, float] = {}

    assert signals_notify._throttled(store, 'k', 60) is True
    clock['t'] += 61.0
    assert signals_notify._throttled(store, 'k', 60) is True, "short window elapsed"
    clock['t'] += 61.0
    assert signals_notify._throttled(store, 'k', 900) is False, "long window has not"
