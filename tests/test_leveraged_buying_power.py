"""Unit tests for schwab_client.get_account_margin_requirement /
get_leveraged_buying_power, added 2026-08-12 after confirming Schwab's raw
'buyingPower' account field is a blanket 50%-margin-requirement number that
overstates real capacity for a 3x leveraged fund (e.g. SOXL/HIBL -- AGQ/ETHU/
JNUG are all actually 2x) by roughly 1/3 -- real house requirement is 50% for
a 2x fund, 75% for a 3x fund,
confirmed against a real `brokerage` account response ($20,000 cash / 0.50 =
$40,000, exact match to AGQ's real reported buyingPower)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pytest

import schwab_client

from fake_broker import fake_broker  # noqa: F401


def test_margin_requirement_2x_fund_is_50_percent(fake_broker):
    fake_broker.set_leverage_factor('AGQ', 200.0)
    assert schwab_client.get_account_margin_requirement('AGQ') == 0.50


def test_margin_requirement_3x_fund_is_75_percent(fake_broker):
    fake_broker.set_leverage_factor('JNUG', 300.0)
    assert schwab_client.get_account_margin_requirement('JNUG') == 0.75


def test_margin_requirement_missing_leverage_factor_fails_closed(fake_broker):
    fake_broker.quotes['NOFUND'] = {'lastPrice': 10.0, 'bidPrice': 10.0, 'askPrice': 10.0}

    def _no_fundamental(ticker):
        from fake_broker import FakeResponse
        return FakeResponse({ticker: {'quote': dict(fake_broker.quotes[ticker])}})
    fake_broker.get_quote = _no_fundamental
    with pytest.raises(ValueError, match="fundLeverageFactor"):
        schwab_client.get_account_margin_requirement('NOFUND')


def test_leveraged_buying_power_2x_fund_matches_real_confirmed_math(fake_broker):
    # Real confirmed 2026-08-12: $20,000 equity / 0.50 = $40,000 for AGQ (2x).
    fake_broker.set_equity('brokerage', 20_000.0)
    fake_broker.set_leverage_factor('AGQ', 200.0)
    assert schwab_client.get_leveraged_buying_power('brokerage', 'AGQ') == pytest.approx(40_000.0)


def test_leveraged_buying_power_3x_fund_is_lower_than_raw_2x_default(fake_broker):
    # $20,000 equity / 0.75 = $26,666.67 for JNUG (3x) -- roughly 1/3 less
    # than the blanket-50% raw buyingPower figure ($40,000) would suggest.
    fake_broker.set_equity('brokerage', 20_000.0)
    fake_broker.set_leverage_factor('JNUG', 300.0)
    result = schwab_client.get_leveraged_buying_power('brokerage', 'JNUG')
    assert result == pytest.approx(26_666.666, abs=0.01)
    assert result < 40_000.0


def test_leveraged_buying_power_defaults_equity_to_cash_when_flat(fake_broker):
    fake_broker.set_cash_balance('brokerage', 20_000.0)  # equity not set explicitly
    fake_broker.set_leverage_factor('AGQ', 200.0)
    assert schwab_client.get_leveraged_buying_power('brokerage', 'AGQ') == pytest.approx(40_000.0)


def test_raw_get_account_buying_power_unaffected_by_leverage_factor(fake_broker):
    """The old function stays untouched -- still reads the raw field
    directly, used only by the drift monitor now."""
    fake_broker.set_cash_balance('brokerage', 20_000.0)
    fake_broker.set_buying_power('brokerage', 40_000.0)
    fake_broker.set_leverage_factor('JNUG', 300.0)  # must have zero effect here
    assert schwab_client.get_account_buying_power('brokerage') == pytest.approx(40_000.0)
