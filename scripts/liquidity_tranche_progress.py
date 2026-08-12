"""Fine-grained progress inside the currently-running liquidity tranche.

scripts/run_liquidity_tranches.sh --status only reports tranche-level
done/pending (a tranche is marked done only after sweep+prune+overlay all
finish) -- while a tranche's sweep is running, there's no visibility into
which ticker/combo it's on or how far through the combo list it is. This
parses the latest logs/liquidity_tranches_*.log to fill that gap.

Usage: .venv/bin/python scripts/liquidity_tranche_progress.py
"""
import glob
import re
import sqlite3
import subprocess
from pathlib import Path

import campaign_comparison_table as cct

COMBO_START_RE = re.compile(
    r"^=== (\S+) \| (\S+) \| fixed_sl=(\d+) \| (\S+) — (.+) ===", re.M
)
TRANCHES_FILE = Path(__file__).resolve().parent / "liquidity_tranches.txt"


def load_campaign():
    """Reads campaign params + tranche membership from
    scripts/liquidity_tranches.txt -- the single source of truth also read
    by run_liquidity_tranches.sh. Don't hardcode any of this a second time."""
    meta = {}
    tranches = {}
    for line in TRANCHES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line and line.split("=", 1)[0].isupper():
            key, val = line.split("=", 1)
            meta[key] = val
        else:
            n, tickers = line.split(None, 1)
            tranches[int(n)] = tickers
    return meta, tranches


def latest_log():
    logs = sorted(glob.glob("logs/liquidity_tranches_*.log"))
    return Path(logs[-1]) if logs else None


def process_running():
    """pgrep -f 'run_liquidity_tranches.sh' self-matches any shell wrapper
    whose own command-line text happens to mention the script name (e.g.
    this very check running inside one) -- anchor to the actual bash
    invocation's exact argv instead of a bare substring."""
    out = subprocess.run(
        ["pgrep", "-af", r"bash \./scripts/run_liquidity_tranches\.sh$"],
        capture_output=True, text=True,
    )
    return bool(out.stdout.strip())


def main():
    meta, tranches = load_campaign()
    version = meta["VERSION"]
    entry_timing = meta["ENTRY_TIMING"]
    strategies = meta["STRATEGIES"].split()
    fixed_sls = [int(x) for x in meta["FIXED_SLS"].split()]

    log = latest_log()
    if log is None:
        print("No logs/liquidity_tranches_*.log found.")
        return

    text = log.read_text(encoding="utf-8", errors="ignore")
    starts = list(COMBO_START_RE.finditer(text))
    if not starts:
        print(f"{log}: no combo-start lines found yet.")
        return

    last_ticker = starts[-1].group(1)
    tranche_n = next((n for n, t in tranches.items() if last_ticker in t.split()), None)

    if tranche_n is None:
        print(f"{log}: last ticker seen ({last_ticker}) not in any known tranche.")
        return

    tranche_tickers = tranches[tranche_n].split()
    expected_combos = [
        (tkr, strat, sl)
        for tkr in tranche_tickers
        for strat in strategies
        for sl in fixed_sls
    ]

    seen_combos = [
        (m.group(1), m.group(2), int(m.group(3)))
        for m in starts
        if m.group(1) in tranche_tickers
    ]
    # Combo is "complete" once a later combo (or SWEEP COMPLETE) started after it.
    complete_markers = text.count("THREE-PHASE SWEEP COMPLETE")

    running = process_running()
    current = starts[-1]

    print(f"log: {log}")
    print(f"process running: {running}")
    print(f"tranche {tranche_n}: {' '.join(tranche_tickers)}")
    print(f"combos seen this tranche: {len(seen_combos)} / {len(expected_combos)} expected")
    print(f"current combo: {current.group(1)} | {current.group(2)} | fixed_sl={current.group(3)} | {current.group(4)}  (started {current.group(5)})")

    remaining = [c for c in expected_combos if c not in seen_combos]
    if remaining:
        print(f"not yet started this tranche ({len(remaining)}):")
        for tkr, strat, sl in remaining:
            print(f"  {tkr} | {strat} | fixed_sl={sl}")

    remaining_tickers = {tkr for tkr, _, _ in remaining}
    done_tickers = [t for t in tranche_tickers if t not in remaining_tickers]
    if done_tickers:
        print(f"\nwinners so far (all combos done for: {', '.join(done_tickers)}):")
        con = sqlite3.connect(cct.DB_PATH)
        for t in done_tickers:
            for strat in strategies:
                nodes = cct.collect(con, version, t, strat, fixed_sls, entry_timing)
                pick = cct.safe_best(nodes) or cct.best_any(nodes)
                if pick is None:
                    print(f"  {t} | {cct.short_label(strat)}: no data")
                    continue
                tag = "SAFE" if pick["safe"] else "CLIFF (best-any, no safe node)"
                print(
                    f"  {t} | {cct.short_label(strat)}: best={pick['best']:.1f}% "
                    f"worst_neighbor={pick['worst']:.1f}% [{tag}] {cct.fmt_node(pick)} "
                    f"trades={pick['trades']} wr={pick['winrate']:.1f}%"
                )
        con.close()
    else:
        print("\nno ticker in this tranche has all 6 combos done yet -- no winners to show.")


if __name__ == "__main__":
    main()
