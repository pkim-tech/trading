"""
Shared config/state for the active_signals split: paths, Slack tokens, the
Slack Bolt app singleton, and the SIM_MODE/INTERACTIVE flags.

Other signals_* modules read these via `import signals_config as cfg; cfg.X`
(attribute access), never `from signals_config import X` for anything mutable
(DB_PATH, SLACK_CHANNEL_ID) -- the latter would copy the value at import time
and silently stop tracking monkeypatches/runtime mutation (e.g.
_resolve_channel_id() setting SLACK_CHANNEL_ID after Socket Mode connects).
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DB_PATH          = Path(os.environ.get("TRADING_DB_PATH", "./cache/live/trading_live.db"))
RESEARCH_DB_PATH = Path("./cache/research/trading_universe.db")
CACHE_DIR        = Path("./cache")
LIVE_DIR         = CACHE_DIR / "live"
RESEARCH_DIR     = CACHE_DIR / "research"
CONFIG_PATH      = Path("./config.json")
POLL_SECS        = int(os.environ.get("SIGNAL_POLL_SECS", 300))
SLACK_HOOK       = os.environ.get("SLACK_WEBHOOK_URL", "")

LOG_DIR = Path("./logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
HUMAN_LOG_PATH   = LOG_DIR / "active_signals.log"
VERBOSE_LOG_PATH = LOG_DIR / "active_signals_verbose.log"
HEARTBEAT_PATH   = LIVE_DIR / "active_signals_heartbeat.txt"


class _Tee:
    """Mirrors writes to multiple streams — used to log to a file without losing
    the live console output when running `active_signals.py run` interactively."""
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            s.write(data)

    def flush(self):
        for s in self._streams:
            s.flush()


SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
SLACK_CHANNEL   = os.environ.get("SLACK_CHANNEL", "")
SOCKET_MODE     = bool(SLACK_BOT_TOKEN and SLACK_APP_TOKEN and SLACK_CHANNEL)

SLACK_CHANNEL_ID = ""
SIM_MODE         = os.environ.get("SIM_MODE") == "1"
SIM_SCENARIO     = os.environ.get("SIM_SCENARIO", "")
# Interactive buttons/reminders require the process's own Socket Mode connection to be
# the one Slack delivers the click to. The sim never starts a SocketModeHandler (only
# run_loop() does), so if it rendered real buttons, a click would be delivered to
# whichever *other* process (the live daemon) happens to be connected — using sim data
# against the live DB. SIM_MODE forces the plain-text/typed-input fallback instead.
INTERACTIVE      = SOCKET_MODE and not SIM_MODE

if SOCKET_MODE:
    from slack_bolt import App
    bolt_app = App(token=SLACK_BOT_TOKEN)
else:
    bolt_app = None


def _resolve_channel_id():
    global SLACK_CHANNEL_ID
    if SLACK_CHANNEL_ID or not SOCKET_MODE:
        return
    try:
        r = bolt_app.client.chat_postMessage(channel=SLACK_CHANNEL, text="Signal monitor online.")
        SLACK_CHANNEL_ID = r['channel']
    except Exception as e:
        print(f"  [slack] could not resolve channel ID: {e}")
