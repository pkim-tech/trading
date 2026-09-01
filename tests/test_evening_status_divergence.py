"""Pinned tests for scripts.evening_status.compute_divergence -- the real-vs-
kernel compounded-return divergence check (Part 3 sub-part 5, 2026-08-15).
Deliberately NOT a loss-streak circuit breaker (see the function's own
docstring); this only flags real compounded return meaningfully worse than
a kernel replay of the same period, not a raw consecutive-loss count."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import scripts.evening_status as es


def test_none_when_too_few_real_trades():
    assert es.compute_divergence([-0.01], [-0.01, -0.02]) is None


def test_none_when_too_few_backtest_trades():
    assert es.compute_divergence([-0.01, -0.02], [-0.01]) is None


def test_real_matching_backtest_not_flagged():
    """Real -23.0% vs backtest -23.3% (SOXL/ira, real 2026-08-15 data) --
    tiny divergence, well under threshold."""
    real_comp, bt_comp, delta_pp = es.compute_divergence([-0.15, -0.096], [-0.15, -0.096, 0.04])
    assert delta_pp == pytest.approx(real_comp - bt_comp)
    assert delta_pp > -es.DIVERGENCE_THRESHOLD_PP


def test_real_worse_than_backtest_beyond_threshold_is_flagged():
    real_rets = [-0.30, -0.30]  # compounds to -51%
    bt_rets = [-0.05, -0.05]    # compounds to -9.75%
    real_comp, bt_comp, delta_pp = es.compute_divergence(real_rets, bt_rets)
    assert real_comp < bt_comp
    assert delta_pp < -es.DIVERGENCE_THRESHOLD_PP


def test_real_better_than_backtest_never_flagged():
    """Real doing BETTER than backtest is not a divergence worth alerting on
    -- only real-worse-than-backtest triggers the flag (delta_pp negative)."""
    real_rets = [0.10, 0.10]
    bt_rets = [-0.05, -0.05]
    real_comp, bt_comp, delta_pp = es.compute_divergence(real_rets, bt_rets)
    assert delta_pp > 0
    assert not (delta_pp < -es.DIVERGENCE_THRESHOLD_PP)


def test_threshold_boundary_exact_not_flagged():
    """Exactly at threshold should not flag (strict inequality)."""
    # Construct rets whose compounded delta is exactly -DIVERGENCE_THRESHOLD_PP.
    bt_rets = [0.0, 0.0]  # bt_comp = 0%
    real_rets = [-es.DIVERGENCE_THRESHOLD_PP / 100.0, 0.0]  # real_comp = -threshold%
    real_comp, bt_comp, delta_pp = es.compute_divergence(real_rets, bt_rets)
    assert delta_pp == pytest.approx(-es.DIVERGENCE_THRESHOLD_PP, abs=0.01)
    assert not (delta_pp < -es.DIVERGENCE_THRESHOLD_PP)


def test_broker_order_check_sizes_via_the_shared_last_sale_recovery_basis(monkeypatch):
    """Real incident #15 round-2 review finding, 2026-08-31 (later corrected
    2026-09-01 -- core/drought share ONE capital pool, not per-leg ones, see
    signals_helpers._last_sale_recovery's own docstring): _broker_order_check's
    local_qty estimate used to call buy_order_sizing with no target_notional
    at all, so a wrong/hardcoded value there wouldn't be caught by any test.
    Proves the reconciliation check genuinely flows through the shared
    buy_order_sizing/_last_sale_recovery helper (not a separately reimplemented
    formula) for a drought-overlay pending, same as any other pending."""
    node = {'id': 1, 'ticker': 'TEST_ES_DIVERGENCE', 'account': 'soxl_ira', 'strategy': 'TrailingBothZScoreBreakout',
            'trail_buy_pct': 1.0, 'starting_notional': 2000}
    pending = {'order_id': 12345, 'signal_price': 50.0, 'position_source': 'drought_overlay'}

    monkeypatch.setattr(es.helpers, 'effectively_dry_run', lambda acct, node: False)
    monkeypatch.setattr(es.schwab_client, 'get_real_orders', lambda acct, ticker: [])
    monkeypatch.setattr(es.schwab_client, 'filter_resting_orders', lambda orders: [])
    monkeypatch.setattr(es.helpers, '_last_sale_recovery', lambda n: 2800.0)

    _ratio, _detail = es._broker_order_check(node, 'pending_entry', pending)

    # target_notional=2800 (from the shared _last_sale_recovery basis), price=50.0,
    # trail_buy_pct=1.0, pad_pct=1.0 (default) -> int(2800 // (50.0 * 1.02)) = 54
    expected_local_qty = int(2800 // (50.0 * 1.02))
    assert _ratio.startswith(f"{expected_local_qty:g}/"), (
        f"local_qty did not flow through the shared _last_sale_recovery basis -- got ratio={_ratio!r}, "
        f"expected local_qty={expected_local_qty}"
    )
