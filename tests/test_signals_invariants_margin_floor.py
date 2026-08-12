"""Tests for signals_invariants.check_margin_floor_zero_for_trading_enabled_accounts,
added 2026-08-12 as a narrow replacement for the removed
check_brokerage_not_live_with_unresolved_leverage_gap -- margin_floor is a
plain DB float no other check guards, and a nonzero value on a
trading_enabled account silently reopens the core-entry leverage-inclusive-
cash gap that check used to bound."""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import schwab_safety
import signals_invariants


@pytest.fixture
def env(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    signals_db.ensure_tables()
    schwab_safety.reload_accounts()
    yield
    Path(tmp_db.name).unlink()


def _set_margin_floor(alias, value, trading_enabled=None):
    conn = signals_db._conn()
    try:
        conn.execute("UPDATE accounts SET margin_floor=? WHERE alias=?", (value, alias))
        if trading_enabled is not None:
            conn.execute("UPDATE accounts SET trading_enabled=? WHERE alias=?",
                         (int(trading_enabled), alias))
        conn.commit()
    finally:
        conn.close()
    schwab_safety.reload_accounts()


def test_zero_margin_floor_on_trading_enabled_account_is_clean(env):
    _set_margin_floor('brokerage', 0.0, trading_enabled=True)
    assert signals_invariants.check_margin_floor_zero_for_trading_enabled_accounts() == []


def test_nonzero_margin_floor_on_trading_enabled_account_flagged(env):
    _set_margin_floor('brokerage', -5000.0, trading_enabled=True)
    violations = signals_invariants.check_margin_floor_zero_for_trading_enabled_accounts()
    assert len(violations) == 1
    assert 'brokerage' in violations[0]
    assert '-5000' in violations[0]


def test_nonzero_margin_floor_on_dry_run_account_not_flagged(env):
    """A nonzero margin_floor has no live effect while trading_enabled=False
    -- matches the removed check's own scoping (only fired for a real,
    trading_enabled account), not a blanket ban on the field ever being set."""
    _set_margin_floor('brokerage', -5000.0, trading_enabled=False)
    assert signals_invariants.check_margin_floor_zero_for_trading_enabled_accounts() == []
