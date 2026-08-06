"""Skim-and-reserve overlay. Reuses sim_skim_redeploy.py's validated SKIM math
(automated, fires on new equity highs) as-is, but builds the FINALIZED manual-redeploy
design (docs/deep_backlog.md, decided-in-principle 2026-08-04) fresh: the existing
sim_skim_redeploy.py prototype tested a different, automated "redeploy more as price
keeps falling" ladder, which is NOT the real decided design (a human alert fires when
equity recovers back above 80% and 100% of its pre-decline peak -- redeploying on
confirmed recovery, not while still falling).

Manual redeploy is modeled with a real reaction-latency parameter (redeploy
`latency_days` calendar days after a threshold is actually crossed) to bound how much a
human's delayed reaction costs relative to the instant-redeploy upper bound -- this
directly backtests against the design's known, accepted "redeploy-fakeout" risk (a
failed bounce before the true bottom) instead of ignoring it.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
# sim_skim_redeploy.py has its own bare `from sim_bear_market_stress import ...` (no
# `scripts.` prefix) -- only resolves when scripts/ itself is on sys.path, which
# happens automatically when running `python scripts/foo.py` directly (Python puts the
# script's own dir at sys.path[0]) but NOT when this module is imported as
# scripts.stacked_model.skim_reserve from the repo root (e.g. `import
# scripts.stacked_model.skim_reserve`, or from v5_stacked_backtest.py). Caught
# 2026-08-08: that import path raised ModuleNotFoundError outside a direct script run.
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
from scripts.sim_skim_redeploy import skim_redeploy_overlay

SKIM_STEP = 0.10
SKIM_FRAC = 0.20
REDEPLOY_THRESHOLDS = (0.80, 1.00)  # fraction of the pre-decline peak
REDEPLOY_FRAC_EACH = 0.50  # each threshold crossing redeploys half the CURRENT reserve


def daily_equity_from_trades(trades, df_h):
    """Compounds canonical trades' realized `ret` onto a real calendar-day index built
    from df_h's own timestamps; flat between trades (cash), matching how capital
    actually sits idle between signal windows live. Mirrors
    sim_real_skim_reserve.daily_equity_from_trades but reads exit_i against df_h
    directly instead of a separate timestamps array."""
    days = pd.DatetimeIndex(pd.Series(df_h.index).dt.normalize().unique()).sort_values()
    equity = pd.Series(1.0, index=days)
    level = 1.0
    for t in sorted(trades, key=lambda x: x["exit_i"]):
        exit_day = pd.Timestamp(df_h.index[t["exit_i"]]).normalize()
        level *= (1.0 + t["ret"])
        equity.loc[equity.index >= exit_day] = level
    return equity


def skim_only(strategy_equity, spy_equity, skim_step=SKIM_STEP, skim_frac=SKIM_FRAC):
    """Skim math only (validated, reused as-is) -- redeploy disabled (an effectively
    unreachable redeploy_step), so the reserve monotonically accumulates. Serves as the
    upper bound the manual-redeploy variants below are compared against (max possible
    dry powder, never redeployed)."""
    return skim_redeploy_overlay(strategy_equity, spy_equity, skim_step, skim_frac,
                                  redeploy_step=1e9, redeploy_frac=0.0)


def manual_redeploy_overlay(strategy_equity, spy_equity, skim_step=SKIM_STEP,
                             skim_frac=SKIM_FRAC, latency_days=0,
                             thresholds=REDEPLOY_THRESHOLDS, redeploy_frac_each=REDEPLOY_FRAC_EACH):
    """Skim (reused, automated) + manual redeploy modeled per the real finalized
    design: each threshold in `thresholds` fires once per decline cycle when equity
    recovers back above (threshold * peak reached just before that decline), acted on
    `latency_days` days after the real crossing (0 = instant/idealized best case; >0
    bounds the cost of slower human reaction). Thresholds re-arm only once a fresh
    post-recovery high is set, so the same recovery can't fire twice."""
    n = len(strategy_equity)
    w_strategy, w_spy = 1.0, 0.0
    total = 1.0
    skim_ref = strategy_equity[0]
    peak_before_decline = strategy_equity[0]
    min_since_peak = strategy_equity[0]
    declining = False
    armed = set(thresholds)
    pending = []  # (trigger_day, threshold) waiting out latency_days

    total_curve = np.ones(n)
    reserve_frac_curve = np.zeros(n)

    for i in range(1, n):
        r_strat = strategy_equity[i] / strategy_equity[i - 1] - 1.0
        r_spy = spy_equity[i] / spy_equity[i - 1] - 1.0
        val_strategy = total * w_strategy * (1 + r_strat)
        val_spy = total * w_spy * (1 + r_spy)
        total = val_strategy + val_spy
        w_strategy, w_spy = val_strategy / total, val_spy / total

        if strategy_equity[i] >= skim_ref * (1 + skim_step):
            moved = w_strategy * skim_frac
            w_strategy -= moved
            w_spy += moved
            skim_ref = strategy_equity[i]

        # A threshold may only fire on RECOVERY past it -- it must first have been
        # crossed on the way DOWN (min_since_peak below peak*thr), or a 0.1% wiggle
        # right at the peak (still ~99.9% of peak, already >= peak*0.80) fires the
        # 0.80 threshold on noise, never having actually declined. The 1.00 threshold
        # specifically means "recovered past the old peak" -- checked here, at the
        # moment equity actually crosses back above peak_before_decline, since by the
        # time this same bar's decline-state would otherwise be re-evaluated,
        # `declining` has already flipped False and the 1.00 check could never see it.
        if strategy_equity[i] > peak_before_decline:
            if declining:
                if 1.00 in armed and min_since_peak < peak_before_decline and w_spy > 0:
                    pending.append((i + latency_days, 1.00))
                armed = set(thresholds)  # full round trip past the old peak -- re-arm
            declining = False
            peak_before_decline = strategy_equity[i]
            min_since_peak = strategy_equity[i]
        else:
            min_since_peak = min(min_since_peak, strategy_equity[i])
            if strategy_equity[i] < peak_before_decline * 0.999:
                declining = True

        if declining and w_spy > 0:
            for thr in list(armed):
                if thr == 1.00:
                    continue  # only ever fires at the full-recovery branch above
                if min_since_peak < peak_before_decline * thr <= strategy_equity[i]:
                    pending.append((i + latency_days, thr))
                    armed.discard(thr)

        still_pending = []
        for trigger_day, thr in pending:
            if i >= trigger_day and w_spy > 0:
                moved = w_spy * redeploy_frac_each
                w_spy -= moved
                w_strategy += moved
            elif i < trigger_day:
                still_pending.append((trigger_day, thr))
        pending = still_pending

        total_curve[i] = total
        reserve_frac_curve[i] = w_spy

    return total_curve, reserve_frac_curve
