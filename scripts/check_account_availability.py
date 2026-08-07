"""Checks which account nicknames in schwab_safety.ACCOUNTS actually resolve
to a real, usable Schwab account hash right now -- config listing an account
doesn't mean it's actually linked/funded/available in the current session.

Usage: .venv/bin/python scripts/check_account_availability.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import schwab_client
import schwab_safety

for nickname, limits in schwab_safety.ACCOUNTS.items():
    try:
        hashes = schwab_client._resolve_account_hashes()
        if nickname in hashes:
            print(f"  {nickname:12s} trading_enabled={limits.trading_enabled!s:5s} -- RESOLVES (real account hash found)")
        else:
            print(f"  {nickname:12s} trading_enabled={limits.trading_enabled!s:5s} -- NOT LINKED (no SCHWAB_ACCOUNT_{nickname.upper()} match)")
    except Exception as e:
        print(f"  {nickname:12s} trading_enabled={limits.trading_enabled!s:5s} -- ERROR: {e}")
