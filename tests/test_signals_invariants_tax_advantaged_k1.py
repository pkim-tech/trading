"""Tests for signals_invariants.check_tax_advantaged_excluded_tickers' 2026-08-15
generalization: the exclusion set is no longer just the hardcoded
db.TAX_ADVANTAGED_EXCLUDED_TICKERS (USO/AGQ) -- it also unions in any ticker
the research DB's tickers.k1_status confirms as K-1, so a future K-1 ticker
doesn't need its own hardcoded addition."""
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db
import schwab_safety
import signals_invariants


@pytest.fixture
def env(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    db.ensure_tables()
    schwab_safety.reload_accounts()

    tmp_research_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_research_db.close()
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', Path(tmp_research_db.name))

    yield
    Path(tmp_db.name).unlink()
    Path(tmp_research_db.name).unlink()


def _seed_research_k1(path, ticker, k1_status):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS tickers (symbol TEXT PRIMARY KEY, k1_status TEXT)")
        conn.execute("INSERT OR REPLACE INTO tickers (symbol, k1_status) VALUES (?, ?)", (ticker, k1_status))
        conn.commit()
    finally:
        conn.close()


def test_hardcoded_agq_still_flagged_in_ira(env):
    # add_node() itself refuses to create AGQ directly in a tax-advantaged
    # account (its own guard) -- simulate the real bypass this invariant
    # check exists to catch (a raw UPDATE reassigning account after the
    # fact, the dominant real path per the check's own docstring).
    db.add_node(ticker='AGQ', strategy='TrailingBothZScoreBreakout', version='v5', window=10, take_profit=30,
                stop_loss=2, max_hold_hours=48, state='live', account='brokerage')
    with db._conn() as c:
        c.execute("UPDATE watch_list SET account='ira' WHERE ticker='AGQ'")
        c.commit()
    violations = signals_invariants.check_tax_advantaged_excluded_tickers()
    assert len(violations) == 1
    assert 'AGQ' in violations[0]


def test_agq_clean_in_brokerage_not_flagged(env):
    """Real 2026-08-12 policy: K-1 alone doesn't disqualify a ticker, only
    restricts it to the taxable brokerage account -- this must stay clean."""
    db.add_node(ticker='AGQ', strategy='TrailingBothZScoreBreakout', version='v5', window=10, take_profit=30,
                stop_loss=2, max_hold_hours=48, state='live', account='brokerage')
    assert signals_invariants.check_tax_advantaged_excluded_tickers() == []


def test_research_confirmed_k1_ticker_not_in_hardcoded_set_is_still_flagged(env):
    """UCO is confirmed K-1 in the real research DB (scripts/candidate_full_
    review.py's K1_STATUS) but was never in the hardcoded
    TAX_ADVANTAGED_EXCLUDED_TICKERS set -- must be caught via the union, not
    require its own hardcoded addition."""
    assert 'UCO' not in db.TAX_ADVANTAGED_EXCLUDED_TICKERS
    _seed_research_k1(signals_config.RESEARCH_DB_PATH, 'UCO', 'CONFIRMED K-1 (uscfinvestments.com)')
    db.add_node(ticker='UCO', strategy='TrailingBothZScoreBreakout', version='v5', window=10, take_profit=30,
                stop_loss=2, max_hold_hours=48, state='live', account='roth')
    violations = signals_invariants.check_tax_advantaged_excluded_tickers()
    assert len(violations) == 1
    assert 'UCO' in violations[0]


def test_research_clean_confirmed_ticker_not_flagged(env):
    _seed_research_k1(signals_config.RESEARCH_DB_PATH, 'SOXL', 'confirmed clean, standard 1099')
    db.add_node(ticker='SOXL', strategy='TrailingBothZScoreBreakout', version='v5', window=10, take_profit=30,
                stop_loss=2, max_hold_hours=48, state='live', account='roth')
    assert signals_invariants.check_tax_advantaged_excluded_tickers() == []


def test_missing_research_db_falls_back_to_hardcoded_set_without_crashing(env, monkeypatch):
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', Path('/nonexistent/path/trading_universe.db'))
    db.add_node(ticker='AGQ', strategy='TrailingBothZScoreBreakout', version='v5', window=10, take_profit=30,
                stop_loss=2, max_hold_hours=48, state='live', account='brokerage')
    with db._conn() as c:
        c.execute("UPDATE watch_list SET account='ira' WHERE ticker='AGQ'")
        c.commit()
    violations = signals_invariants.check_tax_advantaged_excluded_tickers()
    assert len(violations) == 1
    assert 'AGQ' in violations[0]


def test_paper_mode_node_never_flagged(env):
    db.add_node(ticker='AGQ', strategy='TrailingBothZScoreBreakout', version='v5', window=10, take_profit=30,
                stop_loss=2, max_hold_hours=48, state='paper', account='ira')
    assert signals_invariants.check_tax_advantaged_excluded_tickers() == []


def test_scans_across_watchlists_not_just_active_one(env):
    """get_live_nodes() (all watchlists) not get_watchlist() (active only) --
    real live nodes span more than one watchlist."""
    other_wl = db.create_watchlist('other_wl')
    db.add_node(ticker='AGQ', strategy='TrailingBothZScoreBreakout', version='v5', window=10, take_profit=30,
                stop_loss=2, max_hold_hours=48, state='live', account='brokerage', watchlist_id=other_wl)
    with db._conn() as c:
        c.execute("UPDATE watch_list SET account='ira' WHERE ticker='AGQ'")
        c.commit()
    active_id = db.get_active_watchlist_id()
    assert other_wl != active_id
    violations = signals_invariants.check_tax_advantaged_excluded_tickers()
    assert len(violations) == 1
    assert 'AGQ' in violations[0]
