"""Out-of-sample drought confirm_days validation for SOXS specifically -- built for
docs/backlog_cache.md's 2026-08-19 "SOXS drought_confirm_days=1 looks uncalibrated"
item (wl_id=206, real $10k node). Mirrors drought_out_of_sample_check.py's real
5-axis fit/test methodology (the process that produced SOXL's documented
confirm_days=3/vol_gate=0.4 REAL_SELECTION verdict) exactly -- same grids, same
cliff-safety selection, same functions imported directly, not reimplemented.

Not a generic tool because load_nodes(watchlist_id, tickers) requires a real
state='paper', paper_role IS NULL row on watchlist_id=65 to source core risk
params from -- SOXL/AGQ/KORU all have one (their original research-mode node,
pre-promotion), but SOXS never did (went straight from add_node to state='live'
plus a state='paper'/paper_role='daily_sync' clone only). Node built here directly
from watch_list id=206 (the real live node) instead.

Usage: .venv/bin/python scripts/soxs_drought_confirm_days_check.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.drought_overlay_test import get_trades_and_bars, simulate_overlay
from scripts.drought_overlay_sweep import get_ivol_series
from scripts.drought_out_of_sample_check import grid_search_fit_only, _gate_windows, _compounded

NODE = {
    "id": 206, "ticker": "SOXS", "strategy": "TrailingBothZScoreBreakout",
    "window": 20, "z": 1.0, "arm_sell_pct": 9.0, "take_profit": None,
    "fixed_sl": 1.0, "trail_buy_pct": 8.0, "trail_sell_pct": 1.0,
    "max_hold_hours": 42, "entry_timing": "open_check",
}
NODE["arm_pct"] = NODE["arm_sell_pct"]  # TrailingBothZScoreBreakout convention, matches load_nodes()


def main():
    ticker = NODE["ticker"]
    trades, df_h = get_trades_and_bars(NODE)
    ivol_series = get_ivol_series(ticker)
    midpoint = df_h.index[0] + (df_h.index[-1] - df_h.index[0]) / 2

    winner, windows_by_cd = grid_search_fit_only(trades, df_h, ivol_series, midpoint)
    if winner is None:
        print(f"{ticker}: no fit-half drought windows at all -- cannot validate")
        return

    cd, vg, sl, arm, trail = winner["key"]
    _, tuned_test_w_all = windows_by_cd[cd]
    tuned_test_w = _gate_windows(tuned_test_w_all, df_h, ivol_series, vg)
    tuned_test_rets = [simulate_overlay(df_h, ei, ge, sl, arm, trail)["ret"] for ei, ge in tuned_test_w]
    tuned_test_comp = _compounded(tuned_test_rets)

    default_cd = 10
    default_sl, default_arm, default_trail = NODE["fixed_sl"], NODE["arm_pct"], NODE["trail_sell_pct"]
    default_fit_w, default_test_w = windows_by_cd[default_cd]
    default_fit_comp = _compounded(
        [simulate_overlay(df_h, ei, ge, default_sl, default_arm, default_trail)["ret"]
         for ei, ge in default_fit_w])
    default_test_comp = _compounded(
        [simulate_overlay(df_h, ei, ge, default_sl, default_arm, default_trail)["ret"]
         for ei, ge in default_test_w])

    # current live config: confirm_days=1, no vol gate, node's own core sl/arm/trail
    live_cd = 1
    live_fit_w, live_test_w = windows_by_cd[live_cd]
    live_fit_comp = _compounded(
        [simulate_overlay(df_h, ei, ge, default_sl, default_arm, default_trail)["ret"]
         for ei, ge in live_fit_w])
    live_test_comp = _compounded(
        [simulate_overlay(df_h, ei, ge, default_sl, default_arm, default_trail)["ret"]
         for ei, ge in live_test_w])

    print(f"Ticker: {ticker} (wl_id=206)")
    print(f"Midpoint split: {midpoint.date()}")
    print(f"\nTuned (fit-half-selected) config: confirm_days={cd} vol_gate={vg} sl={sl} arm={arm} trail={trail}")
    print(f"  fit:  n={winner['fit_n']} compounded={winner['fit_compounded']*100:.1f}% "
          f"worst_neighbor={winner['fit_worst_neighbor']*100:.1f}% cliff_safe={winner['fit_safe']}")
    print(f"  test: n={len(tuned_test_w)} compounded="
          f"{'n/a' if tuned_test_comp is None else f'{tuned_test_comp*100:.1f}%'}")

    print(f"\nPlain default (confirm_days=10, node's own core sl/arm/trail, no gate):")
    print(f"  fit:  n={len(default_fit_w)} compounded="
          f"{'n/a' if default_fit_comp is None else f'{default_fit_comp*100:.1f}%'}")
    print(f"  test: n={len(default_test_w)} compounded="
          f"{'n/a' if default_test_comp is None else f'{default_test_comp*100:.1f}%'}")

    print(f"\nCurrent LIVE config (confirm_days=1, no gate, node's own core sl/arm/trail):")
    print(f"  fit:  n={len(live_fit_w)} compounded="
          f"{'n/a' if live_fit_comp is None else f'{live_fit_comp*100:.1f}%'}")
    print(f"  test: n={len(live_test_w)} compounded="
          f"{'n/a' if live_test_comp is None else f'{live_test_comp*100:.1f}%'}")

    beats_default = (tuned_test_comp is not None and default_test_comp is not None
                      and tuned_test_comp > default_test_comp)
    print(f"\nTuned beats plain default OOS: {beats_default}")

    print("\nPer-confirm_days windows found (all history, before fit/test split or gating):")
    for cd_val in sorted(windows_by_cd):
        fit_w, test_w = windows_by_cd[cd_val]
        print(f"  confirm_days={cd_val:>2}: fit_windows={len(fit_w)} test_windows={len(test_w)}")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
