import numpy as np
import pytest
from hurst import _hurst_vectorized

RNG = np.random.default_rng(42)
N = 2000
WINDOW = 100


def _last_valid(h):
    s = h[~np.isnan(h)]
    assert len(s) > 0, "no valid Hurst values"
    return np.median(s[-200:])


def test_hurst_random_walk():
    prices = np.cumsum(RNG.standard_normal(N))
    h = _hurst_vectorized(prices, WINDOW)
    val = _last_valid(h)
    assert 0.35 < val < 0.65, f"random walk H={val:.3f}, expected ~0.5"


def test_hurst_trending():
    # positively autocorrelated increments via MA filter → H > 0.5
    noise = RNG.standard_normal(N + 10)
    increments = np.convolve(noise, np.ones(10) / 10, mode='valid')[:N]
    prices = np.cumsum(increments)
    h = _hurst_vectorized(prices, WINDOW)
    val = _last_valid(h)
    assert val > 0.55, f"trending series H={val:.3f}, expected >0.55"


def test_hurst_mean_reverting():
    # negatively autocorrelated increments (alternating signs) → H < 0.5
    noise = RNG.standard_normal(N)
    signs = np.array([(-1) ** i for i in range(N)])
    increments = np.abs(noise) * signs
    prices = np.cumsum(increments)
    h = _hurst_vectorized(prices, WINDOW)
    val = _last_valid(h)
    assert val < 0.45, f"mean-reverting series H={val:.3f}, expected <0.45"


def test_hurst_output_length():
    prices = np.cumsum(RNG.standard_normal(N))
    h = _hurst_vectorized(prices, WINDOW)
    assert len(h) == N
    assert np.all(np.isnan(h[:WINDOW]))
    assert np.any(~np.isnan(h[WINDOW:]))
