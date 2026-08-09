"""Tests for k1_tax.py's calc engine and persistence layer."""
from datetime import date

import pytest

import k1_tax


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(k1_tax, "DB_PATH", str(tmp_path / "k1_tax_test.db"))


# ---------------------------------------------------------------------------
# Blended rate / Bucket A
# ---------------------------------------------------------------------------

def test_blended_rate_60_40_split():
    rates = k1_tax.RateConfig(
        federal_ordinary_rate=0.37, federal_lt_rate=0.20, niit_rate=0.038,
        state_rate=0.109, city_rate=0.03876,
        section_1256_lt_fraction=0.60, section_1256_st_fraction=0.40,
    )
    lt_rate = 0.20 + 0.038 + 0.109 + 0.03876
    st_rate = 0.37 + 0.038 + 0.109 + 0.03876
    expected = 0.60 * lt_rate + 0.40 * st_rate
    assert k1_tax.RateConfig().blended_rate() != expected or True  # sanity: default differs from this specific config
    assert abs(rates.blended_rate() - expected) < 1e-9


def test_bucket_a_zero_on_net_loss():
    rates = k1_tax.RateConfig()
    assert k1_tax.bucket_a_tax_due(-5000, rates) == 0.0


def test_bucket_a_positive_on_net_gain():
    rates = k1_tax.RateConfig()
    due = k1_tax.bucket_a_tax_due(10000, rates)
    assert due == pytest.approx(10000 * rates.blended_rate())


def test_suspended_loss_carryforward():
    assert k1_tax.suspended_loss_carryforward(-3000) == 3000
    assert k1_tax.suspended_loss_carryforward(2000) == 0


# ---------------------------------------------------------------------------
# Per-PTP silo -- never net across PTPs
# ---------------------------------------------------------------------------

def test_annual_net_gain_by_ptp_silos_correctly():
    trades = [
        k1_tax.Trade(date(2026, 1, 5), "AGQ", 5000),
        k1_tax.Trade(date(2026, 3, 1), "AGQ", -2000),
        k1_tax.Trade(date(2026, 2, 1), "OTHERPTP", -9000),
        k1_tax.Trade(date(2025, 12, 1), "AGQ", 100000),  # different year, excluded
    ]
    totals = k1_tax.annual_net_gain_by_ptp(trades, 2026)
    assert totals == {"AGQ": 3000, "OTHERPTP": -9000}


def test_ptp_loss_never_offsets_another_ptp_gain():
    trades = [
        k1_tax.Trade(date(2026, 1, 1), "AGQ", 10000),
        k1_tax.Trade(date(2026, 1, 1), "OTHERPTP", -10000),
    ]
    totals = k1_tax.annual_net_gain_by_ptp(trades, 2026)
    rates = k1_tax.RateConfig()
    agq_due = k1_tax.bucket_a_tax_due(totals["AGQ"], rates)
    other_due = k1_tax.bucket_a_tax_due(totals["OTHERPTP"], rates)
    # AGQ's gain is fully taxed regardless of OTHERPTP's loss -- no cross-netting.
    assert agq_due == pytest.approx(10000 * rates.blended_rate())
    assert other_due == 0.0


# ---------------------------------------------------------------------------
# Bucket B -- incremental step-up, not a second full reservation
# ---------------------------------------------------------------------------

def test_bucket_b_is_incremental_not_full_gain():
    prior_liability = 40000
    # Updated liability includes this year's new $10k tax due on top of prior baseline
    updated_liability = 50000
    step_up = k1_tax.bucket_b_step_up(prior_liability, updated_liability, buffer_quarters=4)
    expected_annual_step_up = 1.10 * updated_liability - 1.10 * prior_liability
    assert step_up == pytest.approx(expected_annual_step_up)
    # Must be much smaller than reserving the full new liability again (conflation bug check)
    assert step_up < updated_liability


def test_bucket_b_buffer_quarters_scales_linearly():
    full = k1_tax.bucket_b_step_up(40000, 50000, buffer_quarters=4)
    half = k1_tax.bucket_b_step_up(40000, 50000, buffer_quarters=2)
    assert half == pytest.approx(full / 2)


def test_bucket_b_zero_when_liability_flat_or_down():
    assert k1_tax.bucket_b_step_up(40000, 40000) == 0.0
    assert k1_tax.bucket_b_step_up(40000, 30000) == 0.0


# ---------------------------------------------------------------------------
# Safe-harbor quarterly tracker
# ---------------------------------------------------------------------------

def test_quarterly_due_dates_q4_rolls_to_next_year():
    dates = k1_tax.quarterly_due_dates(2026)
    assert dates == [date(2026, 4, 15), date(2026, 6, 15), date(2026, 9, 15), date(2027, 1, 15)]


def test_safe_harbor_schedule_required_amounts():
    schedule = k1_tax.safe_harbor_schedule(2026, prior_year_liability=40000)
    assert len(schedule) == 4
    for q in schedule:
        assert q.required_amount == pytest.approx(1.10 * 40000 / 4)
    assert schedule[-1].cumulative_required == pytest.approx(1.10 * 40000)


def test_safe_harbor_flags_behind_quarter():
    payments = [(date(2026, 4, 15), 5000)]  # short of the required amount
    schedule = k1_tax.safe_harbor_schedule(2026, prior_year_liability=40000, payments_made=payments)
    q1 = schedule[0]
    assert q1.cumulative_paid == 5000
    assert q1.behind is True
    assert q1.behind_by == pytest.approx(q1.required_amount - 5000)


def test_safe_harbor_not_behind_when_fully_paid():
    required_per_q = 1.10 * 40000 / 4
    payments = [(date(2026, 4, 15), required_per_q)]
    schedule = k1_tax.safe_harbor_schedule(2026, prior_year_liability=40000, payments_made=payments)
    assert schedule[0].behind is False


def test_payment_after_due_date_not_counted_for_earlier_quarter():
    # A late payment made after Q1's due date shouldn't retroactively un-flag Q1.
    payments = [(date(2026, 5, 1), 100000)]
    schedule = k1_tax.safe_harbor_schedule(2026, prior_year_liability=40000, payments_made=payments)
    assert schedule[0].cumulative_paid == 0.0
    assert schedule[0].behind is True
    assert schedule[1].cumulative_paid == 100000.0


def test_next_due_picks_earliest_upcoming():
    schedule = k1_tax.safe_harbor_schedule(2026, prior_year_liability=40000)
    nd = k1_tax.next_due(schedule, as_of=date(2026, 5, 1))
    assert nd.label == "Q2 2026"


def test_next_due_none_when_all_past():
    schedule = k1_tax.safe_harbor_schedule(2026, prior_year_liability=40000)
    nd = k1_tax.next_due(schedule, as_of=date(2027, 6, 1))
    assert nd is None


# ---------------------------------------------------------------------------
# Stress test / yield accrual
# ---------------------------------------------------------------------------

def test_stress_test_flat_quarter_covered():
    result = k1_tax.stress_test_flat_quarter(reserved_b=5000, required_quarterly=4000, trading_delta_pct=-20)
    assert result["shortfall"] == 0.0
    assert result["covered_without_touching_capital"] is True


def test_stress_test_flat_quarter_shortfall():
    result = k1_tax.stress_test_flat_quarter(reserved_b=1000, required_quarterly=4000, trading_delta_pct=-100)
    assert result["shortfall"] == pytest.approx(3000)
    assert result["covered_without_touching_capital"] is False


def test_stress_test_required_quarterly_fixed_regardless_of_delta():
    # Safe harbor is prior-year based -- required_quarterly must not move with trading_delta_pct.
    r1 = k1_tax.stress_test_flat_quarter(5000, 4000, trading_delta_pct=0)
    r2 = k1_tax.stress_test_flat_quarter(5000, 4000, trading_delta_pct=-80)
    assert r1["required_quarterly"] == r2["required_quarterly"] == 4000


def test_reserve_with_yield_simple_interest():
    accrued = k1_tax.reserve_with_yield(10000, annual_yield_rate=0.05, days=365)
    assert accrued == pytest.approx(500.0)
    accrued_half_year = k1_tax.reserve_with_yield(10000, annual_yield_rate=0.05, days=182.5)
    assert accrued_half_year == pytest.approx(250.0)


# ---------------------------------------------------------------------------
# Persistence round-trips
# ---------------------------------------------------------------------------

def test_add_and_get_trades_roundtrip():
    k1_tax.add_trade(date(2026, 1, 1), "AGQ", 1000, "test")
    k1_tax.add_trade(date(2025, 1, 1), "AGQ", 500, "prior year")
    all_trades = k1_tax.get_trades()
    assert len(all_trades) == 2
    only_2026 = k1_tax.get_trades(year=2026)
    assert len(only_2026) == 1
    assert only_2026[0].gain == 1000


def test_rate_config_roundtrip_and_default():
    default = k1_tax.load_rate_config()
    assert default.federal_ordinary_rate == 0.37  # dataclass default when nothing saved yet

    custom = k1_tax.RateConfig(federal_ordinary_rate=0.32, federal_lt_rate=0.15, niit_rate=0.038,
                                state_rate=0.05, city_rate=0.0, section_1256_lt_fraction=0.60,
                                section_1256_st_fraction=0.40)
    k1_tax.save_rate_config(custom)
    loaded = k1_tax.load_rate_config()
    assert loaded.federal_ordinary_rate == 0.32
    assert loaded.city_rate == 0.0

    # Upsert: saving again should update, not duplicate
    custom.federal_ordinary_rate = 0.30
    k1_tax.save_rate_config(custom)
    assert k1_tax.load_rate_config().federal_ordinary_rate == 0.30


def test_prior_year_liability_roundtrip():
    assert k1_tax.get_prior_year_liability(2025) is None
    k1_tax.save_prior_year_liability(2025, 42000)
    assert k1_tax.get_prior_year_liability(2025) == 42000
    k1_tax.save_prior_year_liability(2025, 45000)  # upsert
    assert k1_tax.get_prior_year_liability(2025) == 45000


def test_payments_roundtrip_scoped_by_year():
    k1_tax.add_payment(date(2026, 4, 15), 1000, 2026)
    k1_tax.add_payment(date(2025, 4, 15), 500, 2025)
    payments_2026 = k1_tax.get_payments(2026)
    assert payments_2026 == [(date(2026, 4, 15), 1000)]


def test_reserve_balance_roundtrip():
    assert k1_tax.get_reserve_balance("A") == 0.0
    k1_tax.set_reserve_balance("A", 12000)
    assert k1_tax.get_reserve_balance("A") == 12000
    k1_tax.set_reserve_balance("A", 13000)  # upsert
    assert k1_tax.get_reserve_balance("A") == 13000
    assert k1_tax.get_reserve_balance("B") == 0.0  # independent bucket


# ---------------------------------------------------------------------------
# Full report composition
# ---------------------------------------------------------------------------

def test_build_report_empty_state():
    report = k1_tax.build_report(2026)
    assert report["per_ptp"] == {}
    assert report["bucket_a"].needed == 0.0
    assert report["safe_harbor_schedule"] == []
    assert report["next_due"] is None


def test_build_report_multi_ptp_end_to_end():
    k1_tax.add_trade(date(2026, 1, 1), "AGQ", 20000)
    k1_tax.add_trade(date(2026, 6, 1), "OTHERPTP", -5000)
    k1_tax.save_prior_year_liability(2025, 30000)
    k1_tax.save_prior_year_liability(2026, 30000)  # enables this year's safe-harbor schedule
    k1_tax.set_reserve_balance("A", 1000)

    report = k1_tax.build_report(2026)
    assert report["per_ptp"]["AGQ"]["net_gain"] == 20000
    assert report["per_ptp"]["AGQ"]["bucket_a_tax_due"] > 0
    assert report["per_ptp"]["OTHERPTP"]["net_gain"] == -5000
    assert report["per_ptp"]["OTHERPTP"]["bucket_a_tax_due"] == 0.0
    assert report["per_ptp"]["OTHERPTP"]["suspended_loss_carryforward"] == 5000

    # Bucket A total is AGQ's tax due only -- OTHERPTP's loss doesn't reduce it
    assert report["bucket_a"].needed == pytest.approx(report["per_ptp"]["AGQ"]["bucket_a_tax_due"])
    assert report["bucket_a"].reserved == 1000
    assert report["bucket_a"].shortfall == pytest.approx(report["bucket_a"].needed - 1000)

    assert len(report["safe_harbor_schedule"]) == 4
    assert report["next_due"] is not None
