"""Promotes a staged market-data pull (from fetch_massive_minute_data.py or
fetch_massive_second_data.py's pulls/ subfolder) into the canonical
cache/research/{minute_data,second_data}/{ticker}_{1m,1s}.csv location.

This is the ONLY thing that should ever write to the canonical path -- the
fetch scripts always stage to pulls/ and never touch canonical directly
(real incident, 2026-08-26: a no-args fetch_massive_minute_data.py refresh
silently overwrote SOXL/DPST/DFEN's canonical 5yr archive down to 2yr,
undetected for hours).

Safety checks, in order:
1. If canonical already exists, back it up first -- unless a backup with
   the exact same (ticker, kind, start, end) signature already exists, in
   which case this canonical content is already preserved and no duplicate
   backup is written.
2. Refuses to promote a staged pull whose start date is LATER than the
   current canonical's start date (i.e. narrower/shrinking history) unless
   --force is passed.
3. Copies the staged file over canonical (never a move -- the staged pull
   stays in pulls/ too).

Usage:
    .venv/bin/python scripts/promote_market_data_pull.py --ticker SOXL --kind minute
    .venv/bin/python scripts/promote_market_data_pull.py --ticker SOXL --kind minute --pull cache/research/minute_data/pulls/SOXL_1m_2021-08-27_2026-08-26.csv
    .venv/bin/python scripts/promote_market_data_pull.py --ticker SOXL --kind minute --force
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

KIND_CONFIG = {
    "minute": {"dir_name": "minute_data", "suffix": "1m"},
    "second": {"dir_name": "second_data", "suffix": "1s"},
}


def _first_last_dates(path: Path):
    with path.open() as f:
        next(f)  # header
        first_line = f.readline()
    first_date = first_line.split(",", 1)[0][:10]
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        block = min(size, 4096)
        f.seek(size - block)
        tail = f.read().decode(errors="replace")
    last_line = [l for l in tail.splitlines() if l.strip()][-1]
    last_date = last_line.split(",", 1)[0][:10]
    return first_date, last_date


def _backup_dir(pulls_dir: Path) -> Path:
    return pulls_dir.parent / "backups"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--kind", required=True, choices=sorted(KIND_CONFIG))
    ap.add_argument("--pull", type=str, default=None,
                     help="explicit staged pull file; default: most recently modified staged pull for this ticker")
    ap.add_argument("--force", action="store_true", help="allow promoting a narrower (shrinking) pull")
    args = ap.parse_args()

    cfg = KIND_CONFIG[args.kind]
    data_dir = ROOT / "cache" / "research" / cfg["dir_name"]
    pulls_dir = data_dir / "pulls"
    canonical_path = data_dir / f"{args.ticker}_{cfg['suffix']}.csv"

    if args.pull:
        staged_path = Path(args.pull)
    else:
        candidates = sorted(pulls_dir.glob(f"{args.ticker}_{cfg['suffix']}_*.csv"),
                             key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            print(f"No staged pulls found for {args.ticker} in {pulls_dir}", file=sys.stderr)
            return 1
        staged_path = candidates[0]

    if not staged_path.exists():
        print(f"Staged pull not found: {staged_path}", file=sys.stderr)
        return 1

    staged_start, staged_end = _first_last_dates(staged_path)
    print(f"Staged pull: {staged_path.name} ({staged_start} -> {staged_end})")

    if canonical_path.exists():
        canon_start, canon_end = _first_last_dates(canonical_path)
        print(f"Current canonical: {canonical_path.name} ({canon_start} -> {canon_end})")

        if staged_start > canon_start and not args.force:
            print(f"REFUSED: staged pull starts {staged_start}, later (narrower) than canonical's "
                  f"{canon_start} -- this would shrink the archive. Use --force to override.",
                  file=sys.stderr)
            return 1

        backup_dir = _backup_dir(pulls_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_signature = f"{args.ticker}_{cfg['suffix']}_{canon_start}_{canon_end}_demoted"
        existing_backup = list(backup_dir.glob(f"{backup_signature}*.csv"))
        if existing_backup:
            print(f"Canonical content ({canon_start} -> {canon_end}) already backed up at "
                  f"{existing_backup[0].name} -- skipping duplicate backup.")
        else:
            import time
            backup_path = backup_dir / f"{backup_signature}_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            shutil.copy2(canonical_path, backup_path)
            print(f"Backed up current canonical to {backup_path}")
    else:
        print("No existing canonical file -- first promotion for this ticker/kind.")

    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(staged_path, canonical_path)
    print(f"Promoted {staged_path.name} -> {canonical_path}")
    return 0


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    sys.exit(main())
