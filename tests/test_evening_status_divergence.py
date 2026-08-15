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
