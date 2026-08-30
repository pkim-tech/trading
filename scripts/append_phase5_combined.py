"""Rebuild the single running cross-campaign Phase5 file for one version -- new,
2026-08-30 (planner dispatch), follow-on to 7bb57bb's combined-per-ticker Phase5
output. Concatenates every per-ticker phase5_second_level_overlay_check_<ticker>_
<version>.csv file that exists for `version` into one
phase5_second_level_overlay_check_ALL_<version>.csv/.xlsx pair, so the user never
has to open per-ticker files or manually re-merge as a campaign progresses -- run
this once after each ticker's Phase5 step finishes (wired into
run_inmemory_sweep_queue.sh) and it just grows, one ticker at a time.

Always a full rebuild from whatever per-ticker files currently exist for
`version`, never an incremental append -- simpler, no dedup/ordering risk, and
trivial cost at this data scale (~450-600 rows/ticker, single digits of tickers
per campaign). Safe/idempotent to re-run any time.

Usage:
  .venv/bin/python scripts/append_phase5_combined.py --version <VERSION>
"""
import argparse
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import pandas as pd


def find_per_ticker_files(version):
    """Real per-ticker Phase5 combined-output files for `version` -- excludes the
    ALL_<version> output itself (ticker component "ALL" is uppercase, doesn't match
    the [a-z0-9]+ ticker.lower() shape) and any stale pre-7bb57bb _w<N>/_combined-
    suffixed file (those don't end the filename right after `_{version}`, so the
    trailing `\\.csv$` anchor excludes them)."""
    pattern = os.path.join(ROOT, "output", f"phase5_second_level_overlay_check_*_{version}.csv")
    name_re = re.compile(
        r"^phase5_second_level_overlay_check_([a-z0-9]+)_" + re.escape(version) + r"\.csv$")
    return sorted(p for p in glob.glob(pattern) if name_re.match(os.path.basename(p)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True,
                     help="campaign version string -- same one already threaded through "
                          "run_inmemory_sweep_queue.sh/phase5_second_level_overlay_check.py's "
                          "own --version.")
    args = ap.parse_args()

    files = find_per_ticker_files(args.version)
    if not files:
        print(f"No per-ticker Phase5 files found for version={args.version} -- nothing to combine.")
        return

    df_all = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)

    from phase5_second_level_overlay_check import _PHASE5_COLUMN_DEFS
    from candidate_summary_report import _write_xlsx

    base_name = f"phase5_second_level_overlay_check_ALL_{args.version}"
    out_path = os.path.join(ROOT, "output", f"{base_name}.csv")
    df_all.to_csv(out_path, index=False)
    xlsx_out_path = os.path.join(ROOT, "output", f"{base_name}.xlsx")
    _write_xlsx(xlsx_out_path, df_all.to_dict("records"),
                col_defs=_PHASE5_COLUMN_DEFS, to_record=lambda r: r)

    prefix, suffix = "phase5_second_level_overlay_check_", f"_{args.version}.csv"
    tickers = sorted(os.path.basename(f)[len(prefix):-len(suffix)] for f in files)
    print(f"PROGRESS: append_phase5_combined done version={args.version}: "
          f"{len(files)} ticker(s) ({', '.join(tickers)}), {len(df_all)} total rows -> {out_path}")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
