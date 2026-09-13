"""Computes the version string a real bench_phase1_phase2_inmemory.py per-job
invocation would produce, given the SAME flags run_inmemory_sweep_queue.sh would
pass it right now -- used by that script's ATTACH_CAMPAIGN_ID path to detect a
caller/attached-campaign version mismatch before any job runs (found 2026-09-12:
launching campaign_22 in attach mode without WINDOW_START/WINDOW_END silently
computed campaign_18's version instead, false-"already done"-skipping all 16
real fixed_sl runs against campaign_18's unrelated finished rows -- caught only
by noticing the skip happened implausibly fast). Mirrors run_inmemory_sweep_
queue.sh's own per-job dispatch exactly: --z-thresholds/--n-islands are ALWAYS
passed explicitly (so always affect the version); --campaign-label/--entry-
timing/window flags are only passed when the corresponding env var is non-empty
(so a blank one means "use bench's own module default", not "no override").

Usage:
  .venv/bin/python scripts/compute_expected_bench_version.py \
      --z-thresholds "0.5 1.0 1.5 2.0" --n-islands 3 \
      [--campaign-label v6.5.2] [--window-start 2021-08-23] [--window-end 2026-05-23] \
      [--entry-timing close] [--seed-watch-list-id 65]
Prints the computed version string to stdout, nothing else.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import bench_phase1_phase2_inmemory as bench
from scripts import campaign_registry


def compute_expected_version(z_thresholds, n_islands, campaign_label=None,
                              window_start=None, window_end=None,
                              entry_timing=None, seed_watch_list_id=None):
    """Pure function (no I/O, no env reads) -- takes already-resolved values so
    it's directly unit-testable. `campaign_label`/`window_start`/`window_end`
    of None means "not overridden", matching run_inmemory_sweep_queue.sh's own
    conditional-arg-omission convention -- falls back to bench's real module
    constants, not a hardcoded guess at what those defaults are."""
    return campaign_registry.build_version_string(
        label=campaign_label if campaign_label else bench.CAMPAIGN_LABEL,
        promotion_algo_version=bench.PROMOTION_ALGO_VERSION,
        data_source=bench.DATA_SOURCE,
        window_start=window_start if window_start else bench.START,
        window_end=window_end if window_end else bench.END,
        z_thresholds=tuple(float(z) for z in z_thresholds.split()),
        n_islands=n_islands,
        seed_watch_list_id=seed_watch_list_id,
        entry_timing=entry_timing,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--z-thresholds", required=True)
    ap.add_argument("--n-islands", type=int, required=True)
    ap.add_argument("--campaign-label", default=None)
    ap.add_argument("--window-start", default=None)
    ap.add_argument("--window-end", default=None)
    ap.add_argument("--entry-timing", default=None)
    ap.add_argument("--seed-watch-list-id", type=int, default=None)
    args = ap.parse_args()

    print(compute_expected_version(
        z_thresholds=args.z_thresholds, n_islands=args.n_islands,
        campaign_label=args.campaign_label, window_start=args.window_start,
        window_end=args.window_end, entry_timing=args.entry_timing,
        seed_watch_list_id=args.seed_watch_list_id,
    ))


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    sys.exit(main())
