"""Records per-phase sweep/reporting elapsed time by appending one line per
phase completion to logs/phase_timing.log. Modeled directly on
script_usage.py's record_invocation() pattern: no DB table, no migration,
just a plain tab-separated append -- scripts/phase_timing_report.py reads
the log back to answer "where is sweep time actually going."

Convention: call record_phase_timing() from the same PROGRESS: print sites
that already compute per-phase elapsed time in bench_phase1_phase2_inmemory.py,
candidate_summary_report.py, and candidate_full_review_two_tab.py.
"""
from datetime import datetime, timezone
from pathlib import Path

_LOG_PATH = Path(__file__).resolve().parent / "logs" / "phase_timing.log"


def record_phase_timing(phase, ticker, strategy, fixed_sl, elapsed_s, version):
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _LOG_PATH.open("a") as f:
        f.write(f"{ts}\t{phase}\t{ticker}\t{strategy}\t{fixed_sl}\t{elapsed_s:.1f}\t{version}\n")
