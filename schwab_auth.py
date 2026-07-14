"""
Schwab OAuth via schwab-py's easy_client. First run opens a browser for the
3-legged OAuth consent flow and saves the token to TOKEN_PATH; every run
after that refreshes silently from the saved token -- until the 7-day
refresh token expires, at which point interactive login is required again.
This is a hard cap from Schwab's API (confirmed 2026-07-13 research), not a
sliding window -- there is no fully unattended path today, a human has to
redo the browser login roughly weekly.
"""
import os
from pathlib import Path

from dotenv import load_dotenv
import schwab.auth

load_dotenv()

TOKEN_PATH = Path(__file__).parent / "cache" / "live" / "schwab_token.json"


def get_client(interactive: bool = True):
    """interactive=False fails fast instead of opening a browser -- use for
    unattended contexts (e.g. the daemon) where a stale token should surface
    as an error, not block on a login prompt nothing can answer."""
    api_key = os.environ["SCHWAB_API_KEY"]
    app_secret = os.environ["SCHWAB_APP_SECRET"]
    callback_url = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")

    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    return schwab.auth.easy_client(
        api_key=api_key,
        app_secret=app_secret,
        callback_url=callback_url,
        token_path=str(TOKEN_PATH),
        interactive=interactive,
    )
