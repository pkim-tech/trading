"""Alert via Slack if active_signals.py's heartbeat is stale.

Runs independently of the daemon process itself -- meant to be invoked by a
Windows Task Scheduler job (host-level, survives WSL/VM suspend), not a WSL
cron job (which freezes along with the daemon during a host sleep/standby).

Usage: python scripts/check_heartbeat.py [max_age_seconds]
"""
import sys
import os
import requests
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

HEARTBEAT_PATH = Path(__file__).resolve().parent.parent / "cache" / "live" / "active_signals_heartbeat.txt"
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL   = os.environ.get("SLACK_CHANNEL", "")

DEFAULT_MAX_AGE = 900  # 15 min -- 3x the daemon's 300s poll interval


def alert(text):
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL:
        print(f"[no slack config] {text}")
        return
    try:
        requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            json={"channel": SLACK_CHANNEL, "text": text},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[alert failed: {e}] {text}")


def check():
    max_age = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MAX_AGE

    if not HEARTBEAT_PATH.exists():
        alert(f"⚠️ active_signals.py heartbeat file missing ({HEARTBEAT_PATH}) — daemon may not be running.")
        return

    last = datetime.strptime(HEARTBEAT_PATH.read_text().strip(), '%Y-%m-%d %H:%M:%S')
    age = (datetime.now() - last).total_seconds()

    if age > max_age:
        alert(
            f"⚠️ active_signals.py heartbeat stale — last update {last:%Y-%m-%d %H:%M:%S} "
            f"({age/60:.0f} min ago). Daemon likely frozen (host sleep/standby?) — check it."
        )
    else:
        print(f"heartbeat OK — {age:.0f}s old")


def main():
    try:
        check()
    except Exception as e:
        alert(f"⚠️ check_heartbeat.py itself crashed: {e!r} — heartbeat status unknown, check manually.")
        raise


if __name__ == '__main__':
    main()
