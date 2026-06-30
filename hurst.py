import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

ROLLING_WINDOW = 200


def _hurst_vectorized(log_p, window):
    lags = [l for l in [2, 4, 8, 16, 32] if l < window // 2]
    if len(lags) < 2 or len(log_p) <= window:
        return np.full(len(log_p), np.nan)

    windows = sliding_window_view(log_p, window)[:-1]
    variances = np.array([
        np.mean((windows[:, lag:] - windows[:, :-lag]) ** 2, axis=1)
        for lag in lags
    ])

    valid = np.all(variances > 0, axis=0)
    hurst_vals = np.full(len(windows), np.nan)
    if valid.any():
        log_lags = np.log(lags)
        X = np.vstack([log_lags, np.ones(len(lags))]).T
        coeffs = np.linalg.lstsq(X, np.log(variances[:, valid]), rcond=None)[0]
        hurst_vals[valid] = coeffs[0] / 2.0

    result = np.full(len(log_p), np.nan)
    result[window:] = hurst_vals
    return result
