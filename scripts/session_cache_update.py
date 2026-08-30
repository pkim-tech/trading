#!/usr/bin/env python3
"""
Mechanically prepend a session entry to docs/session_cache.md (cap 5, drop oldest)
and append the same entry to docs/conversation_summary.md (uncapped).

Usage:
    python scripts/session_cache_update.py < entry.md
    python scripts/session_cache_update.py entry.md

Entry text must start with "## <date> — <title>" and not include surrounding "---".

Also touches ~/.claude/hooks/.session_wrap_active.<session_id> on success --
the marker the global enforce_ready_to_clear.py Stop hook checks for, instead
of detecting a git commit (see docs/backlog_cache.md's "ready to clear" Stop
hook item and docs/deep_backlog.md's matching entry for why: commit-detection
false-positived on any ad hoc commit outside a real wrap, and false-negatived
on a wrap that ended with nothing to commit). This script is the one
mechanical step both `session wrap` and `session close` always run, so tying
the marker here -- not to the assistant remembering to touch a marker "at the
start" -- keeps the fix mechanical rather than memory-dependent, which is the
whole reason this hook exists in the first place (manual compliance already
failed 4 times).

2026-08-30: the marker is scoped to CLAUDE_CODE_SESSION_ID (read from the
env, set for any invocation running inside a real Claude Code session) so a
concurrent unrelated session's Stop hook can't see this run's marker and
wrongly conclude it wrapped too. If the env var is unset (e.g. a manual
out-of-Claude-Code invocation), falls back to the original global filename --
that invocation isn't Stop-hook-relevant anyway, so this just preserves prior
behavior rather than needing new handling.
"""
import os
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
SESSION_CACHE = DOCS / "session_cache.md"
CONVERSATION_SUMMARY = DOCS / "conversation_summary.md"
HOOKS_DIR = Path.home() / ".claude" / "hooks"
_SESSION_ID = os.environ.get("CLAUDE_CODE_SESSION_ID")
WRAP_MARKER = (
    HOOKS_DIR / f".session_wrap_active.{_SESSION_ID}"
    if _SESSION_ID
    else HOOKS_DIR / ".session_wrap_active"
)
MAX_ENTRIES = 5
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
    WRAP_MARKER.parent.mkdir(parents=True, exist_ok=True)
    WRAP_MARKER.touch()
    print("session_cache.md and conversation_summary.md updated")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
