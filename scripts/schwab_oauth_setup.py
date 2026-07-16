"""
Run this directly in your own terminal: python scripts/schwab_oauth_setup.py
Do not paste its output (auth URL, redirect URL, or account numbers) into a
chat session -- the whole point is that the auth code and account data never
leave this terminal. Completes the OAuth flow and saves the token to
cache/live/schwab_token.json (schwab_auth.TOKEN_PATH).
"""
import os
import json
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import schwab.auth as auth

TOKEN_PATH = Path(__file__).parent.parent / "cache" / "live" / "schwab_token.json"


def main():
    api_key = os.environ["SCHWAB_API_KEY"]
    app_secret = os.environ["SCHWAB_APP_SECRET"]
    callback_url = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")

    ctx = auth.get_auth_context(api_key, callback_url)
    print("Open this URL in your browser and log in:\n")
    print(ctx.authorization_url)
    print("\nAfter approving, the browser will redirect to a failed-to-load")
    print(f"{callback_url}/... page. Paste that full URL below.\n")

    received_url = input("Redirect URL: ").strip()

    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)

    def write_func(token):
        with open(TOKEN_PATH, "w") as f:
            json.dump(token, f)

    client = auth.client_from_received_url(
        api_key, app_secret, ctx, received_url, write_func
    )
    print(f"\nOAuth success. Token saved to {TOKEN_PATH}")

    r = client.get_account_numbers()
    r.raise_for_status()
    accounts = r.json()
    print(f"\nLinked {len(accounts)} account(s). Set SCHWAB_ACCOUNT_IRA in .env")
    print("to the account number shown in your Schwab app/site for the linked")
    print("Rollover IRA -- this script deliberately does not print the raw")
    print("account number or hash.")


if __name__ == "__main__":
    main()
