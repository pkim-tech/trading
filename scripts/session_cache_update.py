#!/usr/bin/env python3
"""
Mechanically prepend a session entry to docs/session_cache.md (cap 10, drop oldest)
and append the same entry to docs/conversation_summary.md (uncapped).

Usage:
    python scripts/session_cache_update.py < entry.md
    python scripts/session_cache_update.py entry.md

Entry text must start with "## <date> — <title>" and not include surrounding "---".
"""
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
SESSION_CACHE = DOCS / "session_cache.md"
CONVERSATION_SUMMARY = DOCS / "conversation_summary.md"
MAX_ENTRIES = 10
SEP = "\n---\n\n"


def read_entry() -> str:
    if len(sys.argv) > 1:
        text = Path(sys.argv[1]).read_text()
    else:
        text = sys.stdin.read()
    return text.strip("\n") + "\n"


def update_session_cache(entry: str) -> None:
    raw = SESSION_CACHE.read_text()
    header, _, rest = raw.partition(SEP)
    entries = rest.split(SEP) if rest else []
    entries = [entry] + entries
    entries = entries[:MAX_ENTRIES]
    entries[-1] = entries[-1].rstrip("\n") + "\n"
    new_content = header + SEP + SEP.join(entries)
    SESSION_CACHE.write_text(new_content)


def update_conversation_summary(entry: str) -> None:
    raw = CONVERSATION_SUMMARY.read_text()
    raw = raw.rstrip("\n") + "\n"
    new_content = raw + "\n---\n\n" + entry
    CONVERSATION_SUMMARY.write_text(new_content)


def main() -> None:
    entry = read_entry()
    if not entry.startswith("## "):
        sys.exit("entry must start with '## <date> — <title>'")
    update_session_cache(entry)
    update_conversation_summary(entry)
    print("session_cache.md and conversation_summary.md updated")


if __name__ == "__main__":
    main()
