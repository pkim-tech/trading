"""A mid-bar poll (at_bar_close=False) must not roll the persisted trail
`peak` forward -- only a genuine bar close may, since the next bar-close's
gap-through-trigger check (2026-07-20 fix) depends on `prior_peak`
reflecting only what was confirmed through the PRIOR closed bar. Before this
fix (2026-07-31, found via execution-path walkthrough), every mid-bar poll
in the trailing branch unconditionally wrote `state['peak'] = max(prior_peak,
high)`, so a still-forming bar's own price action leaked into the peak used
to grade that same bar's own Open once it finally closed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from strategies import TrailingBothZScoreBreakout, TrailingExitZScoreBreakout, LimitOrderTrailingExit

STRATS = [TrailingBothZScoreBreakout, TrailingExitZScoreBreakout, LimitOrderTrailingExit]


def _ctx(**over):
    base = dict(
        entry_price=100.0, current_price=105.0, open=105.0, low=105.0, high=105.0,
        stop_loss=0.5, take_profit=0.05, hours_held=1, max_hours_to_hold=100,
        at_bar_close=True, state={'trailing': True, 'peak': 105.0},
    )
    base.update(over)
    return base


def test_mid_bar_poll_does_not_persist_a_higher_peak():
    for strat_cls in STRATS:
        strat = strat_cls(window=10, trail_pct=0.05)
        ctx = _ctx(at_bar_close=False, current_price=109.0, open=109.0, low=109.0, high=109.0)
        reason, price, new_state = strat.check_exit(ctx)
        assert new_state['peak'] == 105.0, (
            f"{strat_cls.__name__}: mid-bar poll must not roll peak forward, got {new_state['peak']}"
        )


def test_mid_bar_poll_still_detects_a_genuine_intrabar_breach():
    """The running peak must still be used LOCALLY within the same call to
    catch a real trailing-stop breach mid-bar (a real broker's continuous
    TRAILING_STOP order does track price this way) -- only the PERSISTED
    state must stay unpolluted, not the breach detection itself."""
    for strat_cls in STRATS:
        strat = strat_cls(window=10, trail_pct=0.05)
        # peak=105 -> trail_stop=99.75 if unpolluted; but this bar's own
        # high=120 -> running trail_stop=114. A low of 113 only breaches
        # against the RUNNING peak, not the stale persisted one.
        ctx = _ctx(at_bar_close=False, current_price=113.0, open=120.0, low=113.0, high=120.0)
        reason, price, new_state = strat.check_exit(ctx)
        assert reason in ('WIN', 'LOSS'), f"{strat_cls.__name__}: expected an intrabar breach, got {reason!r}"


def test_bar_close_rolls_peak_forward_and_next_gap_check_uses_prior_value():
    """The actual regression scenario: a mid-bar poll sees a spike (high=120)
    that does NOT breach, so peak must stay 105 when that same bar finally
    closes -- the bar-close gap check for THIS bar must grade its own Open
    against the peak confirmed through the PRIOR bar (105), not a peak this
    bar's own intrabar action would have produced."""
    for strat_cls in STRATS:
        strat = strat_cls(window=10, trail_pct=0.05)

        # Mid-bar poll: spike to 120, no breach (low stays high). Old code
        # would persist peak=120 here (trail_stop_gap would become 114).
        mid_ctx = _ctx(at_bar_close=False, current_price=120.0, open=120.0, low=119.0, high=120.0)
        reason, price, state_after_midbar = strat.check_exit(mid_ctx)
        assert reason is None, f"{strat_cls.__name__}: mid-bar spike should not itself trigger an exit"
        assert state_after_midbar['peak'] == 105.0

        # Same bar closes with Open=105 -- deliberately chosen BETWEEN the two
        # possible trail_stop_gap values (99.75 from the correct, unpolluted
        # peak=105; 114 from the old code's polluted peak=120), so this
        # assertion genuinely discriminates: correct code must NOT gap-exit
        # here (105 > 99.75), while the old bug WOULD have (105 <= 114,
        # firing a false WIN at a price that never should have exited this
        # bar at all). High/low kept well clear of the real (peak=106)
        # trail_stop of 100.7 so no genuine intrabar breach masks the check.
        close_ctx = _ctx(at_bar_close=True, current_price=104.5, open=105.0, low=104.0, high=106.0,
                          state=state_after_midbar)
        reason, price, final_state = strat.check_exit(close_ctx)
        assert reason is None, (
            f"{strat_cls.__name__}: expected no exit (Open=105 > correct trail_stop_gap=99.75), "
            f"got {reason!r} @ {price} -- indicates peak contamination regressed"
        )
        assert final_state['peak'] == 106.0, f"{strat_cls.__name__}: peak should now roll to this bar's real high"
