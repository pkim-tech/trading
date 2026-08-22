"""One-off test of the Massive.com API (MASSIVE_API_KEY in .env) -- confirms real
1-minute historical bar access before committing to any larger pull. See
docs/backlog_cache.md's data-acquisition discussion, 2026-08-21 (very late).

Pulls one recent week of SOXL 1-minute bars and reports row count/shape/sample.

Usage:
    .venv/bin/python scripts/test_massive_api.py
"""
import os
import sys
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("MASSIVE_API_KEY")
if not API_KEY:
    print("MASSIVE_API_KEY not set in .env", file=sys.stderr)
    sys.exit(1)

end = date.today()
start = end - timedelta(days=7)
ticker = "SOXL"

url = f"https://api.massive.com/v2/aggs/ticker/{ticker}/range/1/minute/{start.isoformat()}/{end.isoformat()}"
params = {"limit": 50000, "sort": "asc", "adjusted": "true", "apiKey": API_KEY}

resp = requests.get(url, params=params, timeout=30)
print("status:", resp.status_code)
print("url (no key):", resp.url.split('apiKey=')[0] + 'apiKey=<redacted>')

if resp.status_code != 200:
    print(resp.text[:2000])
    sys.exit(1)

data = resp.json()
results = data.get("results", [])
print("resultsCount:", data.get("resultsCount"))
print("row count:", len(results))
print("has next_url:", "next_url" in data)
if results:
    print("first row:", results[0])
    print("last row:", results[-1])
