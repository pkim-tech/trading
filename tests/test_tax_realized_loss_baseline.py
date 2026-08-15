"""Pinned tests for the brokerage end-of-year tax-forecast netting math
(docs/deep_backlog.md's 2026-08-15 model) -- k1_tax.brokerage_tax_forecast()
and signals_db's tax_realized_loss_baseline table + get_realized_pnl_by_ticker.
Finance math: pinned so it can't silently drift. Uses an isolated tmp DB with
synthetic trade_log rows (isolated_db fixture pattern from test_coverage_check.py),
not real trading data, so this is deterministic and doesn't depend on real
trading continuing to produce the right shape of trades."""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db
import k1_tax

TICKER_AGQ = 'AGQ'
TICKER_JNUG = 'JNUG'
TICKER_ETHU = 'ETHU'
YEAR = 2026

RATES = k1_tax.RateConfig()  # defaults: fed_ordinary=.37, fed_lt=.20, niit=.038, state=.109, city=.03876
LT_RATE = RATES.federal_lt_rate + RATES.niit_rate + RATES.state_rate + RATES.city_rate       # .38576
ST_RATE = RATES.federal_ordinary_rate + RATES.niit_rate + RATES.state_rate + RATES.city_rate  # .55576


@pytest.fixture
def isolated_db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    db.ensure_tables()
    yield db
    os.unlink(tmp_db.name)


def _insert_closed_trade(ticker, entry_price, exit_price, shares, account='brokerage',
                          exit_time=None, is_dry_run_sim=0):
    exit_time = exit_time or datetime(YEAR, 6, 1, 10, 30)
    with db._conn() as c:
        c.execute("""
            INSERT INTO trade_log (ticker, strategy, version, window, stop_loss, max_hold_hours,
                                    signal_price, signal_time, entry_price, entry_time, entry_drift_pct,
                                    exit_signal_price, exit_price, exit_time, exit_reason, account,
                                    shares, is_dry_run_sim)
            VALUES (?, 'TrailingBothZScoreBreakout', 'v5', 10, 1, 48,
                    ?, ?, ?, ?, 0, ?, ?, ?, 'WIN', ?, ?, ?)
        """, (ticker, entry_price, exit_time.isoformat(), entry_price, exit_time.isoformat(),
              exit_price, exit_price, exit_time.isoformat(), account, shares, is_dry_run_sim))
        c.commit()


# ---------------------------------------------------------------------------
# tax_realized_loss_baseline table + seed row
# ---------------------------------------------------------------------------

def test_ensure_tables_seeds_the_real_240k_baseline(isolated_db):
    baseline = db.get_tax_realized_loss_baseline('brokerage', 2026)
    assert baseline == {'short_term': 240000.0}


def test_ensure_tables_seed_is_idempotent(isolated_db):
    db.ensure_tables()
    db.ensure_tables()
    baseline = db.get_tax_realized_loss_baseline('brokerage', 2026)
    assert baseline == {'short_term': 240000.0}


def test_add_tax_realized_loss_baseline_sums_multiple_rows(isolated_db):
    db.add_tax_realized_loss_baseline('brokerage', 2026, 5000, 'short_term', note='correction')
    baseline = db.get_tax_realized_loss_baseline('brokerage', 2026)
    assert baseline['short_term'] == 245000.0


def test_get_tax_realized_loss_baseline_scoped_to_account_and_year(isolated_db):
    assert db.get_tax_realized_loss_baseline('ira', 2026) == {}
    assert db.get_tax_realized_loss_baseline('brokerage', 2025) == {}


# ---------------------------------------------------------------------------
# get_realized_pnl_by_ticker
# ---------------------------------------------------------------------------

def test_get_realized_pnl_by_ticker_computes_dollar_pnl(isolated_db):
    _insert_closed_trade(TICKER_AGQ, entry_price=100, exit_price=110, shares=1000)  # +$10,000
    _insert_closed_trade(TICKER_JNUG, entry_price=50, exit_price=45, shares=200)    # -$1,000
    realized = db.get_realized_pnl_by_ticker('brokerage', [TICKER_AGQ, TICKER_JNUG], YEAR)
    assert realized[TICKER_AGQ] == pytest.approx(10000.0)
    assert realized[TICKER_JNUG] == pytest.approx(-1000.0)


def test_get_realized_pnl_by_ticker_excludes_dry_run_sim(isolated_db):
    _insert_closed_trade(TICKER_AGQ, entry_price=100, exit_price=200, shares=100, is_dry_run_sim=1)
    realized = db.get_realized_pnl_by_ticker('brokerage', [TICKER_AGQ], YEAR)
    assert TICKER_AGQ not in realized


def test_get_realized_pnl_by_ticker_scopes_to_account_and_year(isolated_db):
    _insert_closed_trade(TICKER_AGQ, entry_price=100, exit_price=110, shares=100, account='ira')
    _insert_closed_trade(TICKER_AGQ, entry_price=100, exit_price=110, shares=100,
                          exit_time=datetime(YEAR - 1, 6, 1))
    realized = db.get_realized_pnl_by_ticker('brokerage', [TICKER_AGQ], YEAR)
    assert TICKER_AGQ not in realized


def test_get_realized_pnl_by_ticker_absent_when_no_closed_trades(isolated_db):
    realized = db.get_realized_pnl_by_ticker('brokerage', [TICKER_AGQ], YEAR)
    assert realized == {}


# ---------------------------------------------------------------------------
# k1_tax.brokerage_tax_forecast -- the netting math itself, pinned by hand
# ---------------------------------------------------------------------------

def test_baseline_fully_absorbs_small_short_term_pool_leaves_only_lt_taxed():
    """AGQ $100k gain (60/40 split), JNUG $50k gain, ETHU -$10k loss.
    By hand: lt_gain_gross = 100000*0.6 = 60000
             st_pool_gross = 100000*0.4 + 50000 - 10000 = 80000
             baseline (240000, all ST) fully absorbs the 80000 ST pool -> st_pool_net = 0
             lt_gain_net = 60000 (untouched -- baseline is 100% short-term character)
             liability = 60000 * LT_RATE + 0 = 60000 * 0.38576 = 23145.6
    """
    realized = {'AGQ': 100000.0, 'JNUG': 50000.0, 'ETHU': -10000.0}
    baseline = {'short_term': 240000.0}
    f = k1_tax.brokerage_tax_forecast(YEAR, realized, baseline, rates=RATES)

    assert f.lt_gain_gross == pytest.approx(60000.0)
    assert f.st_pool_gross == pytest.approx(80000.0)
    assert f.lt_gain_net == pytest.approx(60000.0)
    assert f.st_pool_net == pytest.approx(0.0)
    assert f.st_baseline_remaining == pytest.approx(160000.0)
    assert f.lt_baseline_remaining == pytest.approx(0.0)
    assert f.liability == pytest.approx(60000.0 * LT_RATE)
    assert f.reserve == pytest.approx(f.liability)
    assert f.baseline_exhausted is False
    assert f.recommend_full_sweep is False


def test_baseline_exhausted_taxes_remaining_short_term_pool_and_signals_sweep():
    """AGQ $200k gain, JNUG $100k gain, ETHU $50k gain -- ST pool exceeds the baseline.
    By hand: lt_gain_gross = 200000*0.6 = 120000
             st_pool_gross = 200000*0.4 + 100000 + 50000 = 230000
             st_pool_net = 230000 - 240000 = -10000 -> floored to 0... wait check exceed case below instead
    Use bigger numbers so ST pool clears the $240k baseline outright.
    AGQ $200k, JNUG $150k, ETHU $100k:
             st_pool_gross = 80000 + 150000 + 100000 = 330000
             st_pool_net = 330000 - 240000 = 90000
             lt_gain_gross = 120000, lt_gain_net = 120000 (untouched)
             liability = 120000*LT_RATE + 90000*ST_RATE
    """
    realized = {'AGQ': 200000.0, 'JNUG': 150000.0, 'ETHU': 100000.0}
    baseline = {'short_term': 240000.0}
    f = k1_tax.brokerage_tax_forecast(YEAR, realized, baseline, rates=RATES)

    assert f.lt_gain_gross == pytest.approx(120000.0)
    assert f.st_pool_gross == pytest.approx(330000.0)
    assert f.st_pool_net == pytest.approx(90000.0)
    assert f.lt_gain_net == pytest.approx(120000.0)
    assert f.st_baseline_remaining == pytest.approx(0.0)
    assert f.lt_baseline_remaining == pytest.approx(0.0)
    expected_liability = 120000.0 * LT_RATE + 90000.0 * ST_RATE
    assert f.liability == pytest.approx(expected_liability)
    assert f.baseline_exhausted is True
    assert f.recommend_full_sweep is True


def test_reserve_subtracts_estimate_already_paid():
    realized = {'AGQ': 200000.0, 'JNUG': 150000.0, 'ETHU': 100000.0}
    baseline = {'short_term': 240000.0}
    f = k1_tax.brokerage_tax_forecast(YEAR, realized, baseline, rates=RATES, estimate_already_paid=10000.0)
    assert f.reserve == pytest.approx(f.liability - 10000.0)


def test_reserve_never_goes_negative_when_overpaid():
    realized = {'JNUG': 1000.0}
    baseline = {'short_term': 240000.0}
    f = k1_tax.brokerage_tax_forecast(YEAR, realized, baseline, rates=RATES, estimate_already_paid=999999.0)
    assert f.reserve == 0.0


def test_no_realized_gains_yields_zero_liability_and_baseline_untouched():
    f = k1_tax.brokerage_tax_forecast(YEAR, {}, {'short_term': 240000.0}, rates=RATES)
    assert f.liability == 0.0
    assert f.reserve == 0.0
    assert f.baseline_exhausted is False
    assert f.recommend_full_sweep is False
    assert f.st_baseline_remaining == pytest.approx(240000.0)


def test_long_term_baseline_only_offsets_long_term_slice_not_short_term_pool():
    """A long_term-character baseline (not the real seed, but the table supports it)
    must reduce ONLY the 60% LT slice, never the ST pool -- distinct axis."""
    realized = {'AGQ': 100000.0}  # lt=60000, st=40000
    baseline = {'long_term': 60000.0}
    f = k1_tax.brokerage_tax_forecast(YEAR, realized, baseline, rates=RATES)
    assert f.lt_gain_net == pytest.approx(0.0)   # fully offset
    assert f.st_pool_net == pytest.approx(40000.0)  # untouched by the LT baseline
    assert f.liability == pytest.approx(40000.0 * ST_RATE)


def test_no_section_1256_ticker_activity_classifies_correctly():
    realized = {'JNUG': 30000.0, 'ETHU': -5000.0}
    f = k1_tax.brokerage_tax_forecast(YEAR, realized, {}, rates=RATES)
    assert f.section_1256_gain == {}
    assert f.ordinary_st_gain == {'JNUG': 30000.0, 'ETHU': -5000.0}
    assert f.lt_gain_gross == 0.0
    assert f.st_pool_gross == pytest.approx(25000.0)
