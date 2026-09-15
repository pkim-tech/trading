"""Compresses raw 1s CSVs (cache/research/second_data/{ticker}_1s.csv) to .gz,
freeing real disk space on files that are only ever read again by
build_massive_second_derived.py's rebuild path, not a hot read.

Confirmed 2026-09-14: gzip round-trip is byte-identical (pd.read_csv on .csv vs
.csv.gz produced identical DataFrames/hashes for a 29.1M-row file), and the
derive rebuild itself is cheap (SOXL, the largest ticker at 22.2M second bars,
took 365s; every other ticker is smaller).

Real correctness risk this script exists to close: build_massive_second_
derived.py's build_ticker() derives raw_pulled_at from the raw file's mtime
(os.path.getmtime), which feeds the residual post-pull-dividend-adjustment
logic. Gzipping naively resets mtime to compression time, silently corrupting
that provenance. Fix: capture the ORIGINAL file's real mtime into a sidecar
JSON (alongside row count, start/end timestamp, and an md5 of the decompressed
content) before compressing -- build_massive_second_derived.py reads
raw_pulled_at from this sidecar when the .gz path is used, never from the
.gz file's own mtime.

Usage:
    .venv/bin/python scripts/compress_raw_second_csvs.py [--tickers T ...] [--all]
        [--delete-originals]   # only after verification passes for every ticker
"""
import argparse
import gzip
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SECOND_DIR = ROOT / "cache" / "research" / "second_data"


def _md5_of_file(path, chunk_size=8 * 1024 * 1024):
    h = hashlib.md5()
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def compress_ticker(ticker, delete_original=False):
    csv_path = SECOND_DIR / f"{ticker}_1s.csv"
    gz_path = SECOND_DIR / f"{ticker}_1s.csv.gz"
    meta_path = SECOND_DIR / f"{ticker}_1s.meta.json"

    if not csv_path.exists():
        print(f"{ticker}: no raw CSV on disk, skipping")
        return False

    orig_mtime = os.path.getmtime(csv_path)
    raw_pulled_at = pd.Timestamp(orig_mtime, unit="s").strftime("%Y-%m-%d %H:%M:%S")

    # First/last row read cheaply (no full pandas parse) for the date-range
    # metadata -- must happen on the UNCOMPRESSED file, since gzip has no
    # random access to seek to the end without decompressing the whole stream.
    with open(csv_path, "r") as f:
        header = f.readline()
        first_row = f.readline()
    with open(csv_path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        seek_back = min(size, 65536)
        f.seek(size - seek_back)
        tail = f.read().decode(errors="ignore")
    last_row = [l for l in tail.strip().split("\n") if l][-1]
    row_count = sum(1 for _ in open(csv_path, "r")) - 1  # minus header

    orig_md5 = _md5_of_file(csv_path)

    print(f"{ticker}: compressing {csv_path.stat().st_size/1e9:.2f}GB "
          f"({row_count:,} rows, raw_pulled_at={raw_pulled_at})...")
    with open(csv_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    # Preserve original mtime on the .gz as a secondary signal (belt-and-
    # suspenders) -- the sidecar JSON is the authoritative source, this is
    # not relied on by build_massive_second_derived.py.
    os.utime(gz_path, (orig_mtime, orig_mtime))

    gz_md5 = _md5_of_file(gz_path)
    if gz_md5 != orig_md5:
        print(f"{ticker}: MISMATCH after compression (orig={orig_md5} gz={gz_md5}) -- "
              f"removing bad .gz, NOT deleting original", file=sys.stderr)
        gz_path.unlink(missing_ok=True)
        return False

    meta = {
        "ticker": ticker,
        "raw_pulled_at": raw_pulled_at,
        "row_count": row_count,
        "header": header.strip(),
        "first_row": first_row.strip(),
        "last_row": last_row,
        "md5_decompressed": orig_md5,
        "orig_csv_bytes": csv_path.stat().st_size,
        "gz_bytes": gz_path.stat().st_size,
    }
    meta_path.write_text(json.dumps(meta, indent=2))

    orig_size = csv_path.stat().st_size
    gz_size = gz_path.stat().st_size
    print(f"{ticker}: verified OK -- {orig_size/1e9:.2f}GB -> {gz_size/1e9:.2f}GB "
          f"({100*(1-gz_size/orig_size):.0f}% reduction)")

    if delete_original:
        csv_path.unlink()
        print(f"{ticker}: removed original {csv_path.name}")

    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--delete-originals", action="store_true",
                     help="delete the uncompressed .csv after verified compression -- "
                          "only takes effect per-ticker if that ticker's own verification passed")
    args = ap.parse_args()

    if args.all:
        tickers = sorted(p.stem.replace("_1s", "") for p in SECOND_DIR.glob("*_1s.csv"))
    elif args.tickers:
        tickers = args.tickers
    else:
        print("Specify --tickers or --all", file=sys.stderr)
        sys.exit(1)

    ok, failed = 0, []
    for t in tickers:
        if compress_ticker(t, delete_original=args.delete_originals):
            ok += 1
        else:
            failed.append(t)

    print(f"\nDone: {ok} compressed, {len(failed)} failed/skipped: {failed}")


if __name__ == "__main__":
    main()
