"""Flags CLAUDE.md lines making a K-1 tax-status claim about a ticker that
contradicts tickers.k1_status in the real DB -- built 2026-08-19 after a stale
2026-08-04 CLAUDE.md note ("unlike the standard '40-Act/1099 structure of
SOXL/AGQ/etc.") turned out to be flatly wrong: AGQ/ZSL are confirmed K-1 too.
The note predated a later policy change (K-1 generalized 2026-08-12 from
"disqualifying" to "restricts to brokerage") and nobody reconciled the older
claim against it.

Heuristic, not authoritative -- a line matched here needs a human read, not
an auto-fix. Only checks the one claim class that's actually bitten us
(K-1/tax-structure claims); this is not a general "is any sentence in
CLAUDE.md still true" checker, which isn't a boundable problem. Only
tickers present in the tickers table are matched (avoids false-positives on
random all-caps words like "SL"/"TP"/"IRA").

Usage: .venv/bin/python scripts/claude_md_staleness_check.py [--file CLAUDE.md]
"""
import argparse
import re
import sqlite3
from pathlib import Path

DB = "cache/research/trading_universe.db"

# Words that, appearing near "K-1"/"K1" close to a ticker mention, flip the
# claim from POSITIVE (this ticker IS K-1) to NEGATIVE (this ticker is NOT K-1).
NEGATION_WORDS = re.compile(r"\b(not|non-|unlike|isn't|never|clean|no)\b", re.I)
K1_RE = re.compile(r"\bK-?1\b")


def load_k1_status():
    conn = sqlite3.connect(DB)
    rows = conn.execute("SELECT symbol, k1_status FROM tickers WHERE symbol IS NOT NULL").fetchall()
    conn.close()
    status = {}
    for sym, k1 in rows:
        if k1 is None:
            continue
        status[sym] = "K1" if k1.upper().startswith("CONFIRMED K-1") else "NOT_K1"
    return status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(Path(__file__).resolve().parent.parent / "CLAUDE.md"))
    args = ap.parse_args()

    k1_status = load_k1_status()
    tickers_re = re.compile(r"\b(" + "|".join(re.escape(t) for t in sorted(k1_status, key=len, reverse=True)) + r")\b")

    text = Path(args.file).read_text()
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not K1_RE.search(line):
            continue
        for m in tickers_re.finditer(line):
            ticker = m.group(1)
            # look at a window around the ticker mention and the nearest K-1 mention
            k1_match = K1_RE.search(line)
            window_start = min(m.start(), k1_match.start())
            window_end = max(m.end(), k1_match.end())
            window = line[max(0, window_start - 40):window_end + 10]
            claimed = "NOT_K1" if NEGATION_WORDS.search(window) else "K1"
            real = k1_status.get(ticker)
            if real is not None and claimed != real:
                findings.append((lineno, ticker, claimed, real, line.strip()))

    if not findings:
        print("No K-1 claim mismatches found (heuristic check -- doesn't mean the doc is fully current).")
        return

    print(f"{len(findings)} possible stale K-1 claim(s) -- verify by hand, this is a heuristic match:\n")
    seen = set()
    for lineno, ticker, claimed, real, line in findings:
        key = (lineno, ticker)
        if key in seen:
            continue
        seen.add(key)
        print(f"Line {lineno}: {ticker} -- CLAUDE.md implies {claimed}, tickers.k1_status says {real}")
        print(f"  {line}\n")


if __name__ == "__main__":
    main()
