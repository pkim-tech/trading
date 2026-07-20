"""Shared config for backtest-change-rollout queue runs (see
.claude/skills/backtest-change-rollout). Single source of truth for each
strategy's hyperparameter grid and which entry_timings it supports, so the
queue script can't drift from run_optimization_sweep.py's own axis mapping.

Usage:
    python scripts/campaign_config.py patch STRATEGY FIXED_SL
"""
import json
import sys

COMBINED = [1, 2, 3, 4, 5, 6, 9, 12, 15, 18, 21, 24, 27, 30]
TRAIL_PCTS = [1, 2, 3, 4, 5, 6, 7]

# Per-strategy grid + supported entry_timings. TrailingExitZScoreBreakout's
# kernel (run_backtest_v18) ignores entry_timing -- close only (see
# run_v5_full18_test.sh's original comment).
STRATEGIES = {
    "TrailingBothZScoreBreakout": {
        "take_profits": COMBINED,
        "stop_losses": COMBINED,       # -> trail_buy_pct axis (sl_axis)
        "trail_pcts": TRAIL_PCTS,      # -> trail_sell_pct axis (fourth_axis)
        # open_check only, 2026-07-20: matches the real live watch_list config
        # for all 18 tickers -- close was just the earlier experimental
        # comparison, not something to resweep by default.
        "entry_timings": ["open_check"],
    },
    "TrailingExitZScoreBreakout": {
        "take_profits": COMBINED,
        "stop_losses": TRAIL_PCTS,     # -> trail_pct axis (sl_axis), no fourth axis
        "trail_pcts": TRAIL_PCTS,      # unused (no fourth_axis) but harmless to set
        # open_check now supported (backtester.py, 2026-07-20) -- defaulted to
        # match TrailingBothZScoreBreakout's scope for a fair immediate-entry
        # vs trailing-buy comparison under the real live entry_timing.
        "entry_timings": ["open_check"],
    },
}


def patch_config(strategy, fixed_sl):
    grid = STRATEGIES[strategy]
    with open("config.json") as f:
        c = json.load(f)
    c["active_strategies"] = [strategy]
    c["hyperparameters"]["take_profits"] = grid["take_profits"]
    c["hyperparameters"]["stop_losses"] = grid["stop_losses"]
    c["hyperparameters"]["trail_pcts"] = grid["trail_pcts"]
    c["execution"]["max_generations"] = 3
    c["execution"]["fixed_stop_loss"] = float(fixed_sl)
    with open("config.json", "w") as f:
        json.dump(c, f, indent=4)
    print(f"Patched config for {strategy} (fixed_sl={fixed_sl}%)")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "patch":
        patch_config(sys.argv[2], sys.argv[3])
    elif cmd == "entry_timings":
        print(" ".join(STRATEGIES[sys.argv[2]]["entry_timings"]))
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
