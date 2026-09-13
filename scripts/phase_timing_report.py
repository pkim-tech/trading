"""Reads logs/phase_timing.log (written by phase_timing.record_phase_timing(),
called from the PROGRESS: print sites in bench_phase1_phase2_inmemory.py's
Phase1/Phase2/Phase2.5, candidate_summary_report.py's Phase4, and
candidate_full_review_two_tab.py's Phase9/Phase10 tab builds) and summarizes
where sweep/reporting wall-clock time is actually going, phase by phase --
same "read the log, summarize" shape as scripts/list_scripts.py.

Usage:
  .venv/bin/python scripts/phase_timing_report.py                  # totals/avg per phase
  .venv/bin/python scripts/phase_timing_report.py --ticker SOXL    # filter by ticker
  .venv/bin/python scripts/phase_timing_report.py --version v6.5.2-...  # filter by version
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "phase_timing.log"


def _read_rows():
    rows = []
    if not _LOG_PATH.exists():
        return rows
    for line in _LOG_PATH.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        ts, phase, ticker, strategy, fixed_sl, elapsed_s, version = parts
        try:
            elapsed_s = float(elapsed_s)
        except ValueError:
            continue
        rows.append({
            "ts": ts, "phase": phase, "ticker": ticker, "strategy": strategy,
            "fixed_sl": fixed_sl, "elapsed_s": elapsed_s, "version": version,
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--version", default=None)
    args = ap.parse_args()

    rows = _read_rows()
    if args.ticker:
        rows = [r for r in rows if r["ticker"] == args.ticker]
    if args.version:
        rows = [r for r in rows if r["version"] == args.version]

    if not rows:
        print(f"No phase timing rows found in {_LOG_PATH} "
              f"(after filters: ticker={args.ticker!r}, version={args.version!r}).")
        return

    by_phase = defaultdict(list)
    for r in rows:
        by_phase[r["phase"]].append(r["elapsed_s"])

    print(f"{len(rows)} phase-timing row(s) from {_LOG_PATH}\n")
    print(f"{'phase':<14} {'count':>6} {'total_s':>12} {'avg_s':>10} {'min_s':>10} {'max_s':>10}")
    for phase in sorted(by_phase, key=lambda p: -sum(by_phase[p])):
        vals = by_phase[phase]
        print(f"{phase:<14} {len(vals):>6} {sum(vals):>12.1f} {sum(vals)/len(vals):>10.1f} "
              f"{min(vals):>10.1f} {max(vals):>10.1f}")


if __name__ == "__main__":
    import script_usage
    script_usage.record_invocation()
    main()
