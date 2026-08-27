"""Records that a scripts/*.py file was actually executed as __main__, by
appending one line per invocation to logs/script_usage.log. This exists
because git mtime only tells you when a script's CODE last changed, not
when it was last actually RUN -- a script can be untouched in git for a
year while still firing nightly via cron, or edited yesterday and never
run since. list_scripts.py reads this log to show real recency instead.

Convention: every scripts/*.py file with an `if __name__ == '__main__':`
block calls record_invocation() as the first line of that block. Enforced
by scripts/check_script_usage_convention.py (run it after adding a new
script, or as part of the pre-commit checklist).
"""
from datetime import datetime, timezone
from pathlib import Path
import sys

_LOG_PATH = Path(__file__).resolve().parent / "logs" / "script_usage.log"


def record_invocation():
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    name = Path(sys.argv[0]).name
    args = " ".join(sys.argv[1:])
    with _LOG_PATH.open("a") as f:
        f.write(f"{ts}\t{name}\t{args}\n")
