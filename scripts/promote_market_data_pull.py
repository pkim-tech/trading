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


def _canonical_dates_from_meta(meta_path: Path):
    """For a compressed canonical (scripts/compress_raw_second_csvs.py's
    .csv.gz + sidecar .meta.json), reads start/end dates straight from the
    sidecar's first_row/last_row -- avoids decompressing the whole .gz just to
    find the tail (gzip has no random seek to a byte offset near the end the
    way _first_last_dates's plain-file tail-read does)."""
    import json
    meta = json.loads(meta_path.read_text())
    first_date = meta["first_row"].split(",", 1)[0][:10]
    last_date = meta["last_row"].split(",", 1)[0][:10]
    return first_date, last_date


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
    canonical_gz_path = data_dir / f"{args.ticker}_{cfg['suffix']}.csv.gz"
    canonical_meta_path = data_dir / f"{args.ticker}_{cfg['suffix']}.meta.json"
    # A compressed canonical (scripts/compress_raw_second_csvs.py) must count as
    # "canonical already exists" here -- real gap found 2026-09-14: this script
    # used to only check for the plain .csv, so a fresh pull against a
    # compressed-only canonical would silently skip the backup AND the
    # refuse-to-narrow guard entirely (the exact incident this script exists to
    # prevent), then leave the old .csv.gz/.meta.json behind as stale, orphaned
    # leftovers next to the new plain .csv.
    canonical_is_compressed = not canonical_path.exists() and canonical_gz_path.exists()

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

    if canonical_path.exists() or canonical_is_compressed:
        if canonical_is_compressed:
            canon_start, canon_end = _canonical_dates_from_meta(canonical_meta_path)
            print(f"Current canonical (compressed): {canonical_gz_path.name} ({canon_start} -> {canon_end})")
        else:
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
        existing_backup = list(backup_dir.glob(f"{backup_signature}*"))
        if existing_backup:
            print(f"Canonical content ({canon_start} -> {canon_end}) already backed up at "
                  f"{existing_backup[0].name} -- skipping duplicate backup.")
        elif canonical_is_compressed:
            import time
            stamp = time.strftime('%Y%m%d_%H%M%S')
            shutil.copy2(canonical_gz_path, backup_dir / f"{backup_signature}_{stamp}.csv.gz")
            shutil.copy2(canonical_meta_path, backup_dir / f"{backup_signature}_{stamp}.meta.json")
            print(f"Backed up current compressed canonical to {backup_dir}/{backup_signature}_{stamp}.csv.gz")
        else:
            import time
            backup_path = backup_dir / f"{backup_signature}_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            shutil.copy2(canonical_path, backup_path)
            print(f"Backed up current canonical to {backup_path}")

        if canonical_is_compressed:
            # The new staged pull always lands as a plain .csv -- remove the now-
            # superseded compressed copy + sidecar rather than leaving it as a stale,
            # orphaned leftover next to the fresh canonical (it's already preserved
            # in backups/ above if anyone needs the old content).
            canonical_gz_path.unlink()
            canonical_meta_path.unlink()
            print(f"Removed superseded {canonical_gz_path.name}/{canonical_meta_path.name} "
                  f"(preserved in backups/)")
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
