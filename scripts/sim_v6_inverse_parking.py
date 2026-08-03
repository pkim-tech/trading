"""
v6 idea, inverse-pair variant (2026-08-03): rather than parking idle capital in
SPY (tested 2026-07-22, no robust edge -- docs/research_log.md) or scanning the
whole cached universe for a vehicle (sim_v6_parking_vehicle_sweep.py), park it
in the ticker's own real leveraged inverse pair while its primary position is
closed. Rationale (user's hypothesis): a buy signal on one side implies price
is far from the inverse's own buy trigger, so the two shouldn't fire
simultaneously -- and if the primary is genuinely down enough to trigger,
the inverse should be up over that same stretch.

Only 3 pairs currently have both legs backtested on the corrected v5 kernel
and a real watchlist_id=65 node to source real gap windows from:
GDXU/GDXD, NUGT/DUST, AGQ/ZSL. Reuses sim_v6_parking_vehicle_sweep's
extract_gap_windows/candidate_window_returns (identical windowing/pricing
logic, just scored against one named inverse candidate instead of scanning
the full universe) plus SPY and a 0%-return cash baseline for comparison.

This is a per-window compounded-return screen only (same "as if capital
always free" simplification the vehicle sweep flags) -- not yet the overlap-
timing claim itself (whether the inverse is ever concurrently near its own
trigger during the primary's idle window); that's a separate, not-yet-run
check.

Usage:
  .venv/bin/python scripts/sim_v6_inverse_parking.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from scripts.sim_v6_parking_vehicle_sweep import (
    extract_gap_windows, candidate_window_returns, score_candidate,
)

PAIRS = [
    ("GDXU", "GDXD"),
    ("NUGT", "DUST"),
    ("AGQ", "ZSL"),
]
BASELINES = ["SPY"]


def main():
    all_rows = []
    for source, inverse in PAIRS:
        print(f"\n=== {source} idle windows -> park in {inverse}? ===")
        windows = extract_gap_windows(source)
        windows_df = pd.DataFrame(windows)
        if windows_df.empty:
            print(f"  no gap windows found for {source}, skipping")
            continue
        print(f"  {len(windows_df)} real gap windows")

        for candidate in [inverse] + BASELINES:
            r = score_candidate(candidate, windows_df)
            if r is None:
                print(f"  {candidate}: insufficient data")
                continue
            r["source"] = source
            all_rows.append(r)

        # cash baseline (0% every window) computed directly, no price data needed
        all_rows.append({
            "source": source, "candidate": "CASH", "windows_covered": len(windows_df),
            "compounded_return_pct": 0.0, "mean_window_return_pct": 0.0, "win_rate_pct": 0.0,
        })

    out = pd.DataFrame(all_rows)[
        ["source", "candidate", "windows_covered", "compounded_return_pct",
         "mean_window_return_pct", "win_rate_pct"]
    ]
    print("\n=== Summary ===")
    print(out.to_string(index=False))
    out_path = Path("output") / "v6_inverse_parking_results.csv"
    out.to_csv(out_path, index=False)
    print(f"\n-> {out_path}")


if __name__ == "__main__":
    main()
