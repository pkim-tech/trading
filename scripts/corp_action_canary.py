"""Daily SPY corp-action canary: observes whether/when Yahoo's and Massive's data
each reflect SPY's real, known upcoming ex-dividend date (2026-09-18) -- pure
observation/logging, NOT a live-trading alert, NOT wired into any live signal
computation path.

Closes 2 real open unknowns from docs/design.md's 2026-08-28 (late) entry
("corporate-action detection for the yahoo _1h.csv live-signal cache..."):
(1) whether Massive's /stocks/v1/dividends endpoint reflects a dividend
promptly AFTER the real ex-date (confirmed only that it does NOT show one in
advance, checked 2026-08-22); (2) neither Massive's nor Yahoo's split/dividend
rebase timing has ever been empirically observed by this project for either
source.

Each run:
  1. Fetches a fresh window of SPY daily closes from Yahoo (yf.download,
     auto_adjust=True, same convention as data_manager.py's existing hourly
     fetch) and compares them against YESTERDAY's logged closes for the same
     already-elapsed dates -- any change is direct proof of a retroactive
     Yahoo rebase. This is design.md's part-1 self-consistency detector, used
     here purely as an observation -- it is NOT wired into signals_compute.py
     or any live code path.
  2. Fetches a fresh, small window of SPY raw minute data directly from
     Massive (scripts.fetch_massive_minute_data.fetch_ticker, reused as-is --
     this script never writes to or promotes the canonical minute cache) and
     records the raw Close of the first bar on/after the ex-div date, once
     available. Informational only: Massive's raw minute is split-adjusted
     ONLY (no dividend adjustment expected here at all, per that script's own
     docs) -- an unexpected jump would itself be the interesting finding.
  3. Calls scripts.build_massive_hourly_derived.fetch_dividends('SPY') (the
     existing, cached /stocks/v1/dividends call -- reused, not reimplemented)
     and records whether SPY's real 2026-09-18 ex-dividend entry is present
     yet.

Logs one JSON line per day to docs/corp_action_canary_log.jsonl (committed,
append-only). Chosen over docs/research_log.md's free-form prose because this
needs ~3-4 weeks of daily, structured, machine-diffable entries -- a human or
a future script can read consecutive lines to see exactly which day each
source's data changed, without parsing prose. Idempotent: a second same-day
run replaces (not duplicates) that day's line.

Non-goals (explicit, do not extend here): no alerting/paging -- this is silent
data collection; no crontab wiring (a separate step, handled once this script
is confirmed working); SPY only -- not a general corp-action detection system
(see docs/design.md's 2026-08-28 (late) entry for that separate, bigger-scope,
not-yet-built idea).

Usage:
    .venv/bin/python scripts/corp_action_canary.py
"""
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # cron runs from an unknown cwd -- every relative path here assumes repo root

import pandas as pd
import yfinance as yf

from scripts.fetch_massive_minute_data import fetch_ticker as fetch_massive_minute_ticker
from scripts.build_massive_hourly_derived import fetch_dividends

TICKER = "SPY"
EX_DIV_DATE = date(2026, 9, 18)
LOG_PATH = ROOT / "docs" / "corp_action_canary_log.jsonl"
YAHOO_WINDOW_DAYS = 30   # covers the ex-div date with buffer once it's elapsed
MASSIVE_WINDOW_DAYS = 5  # small, cheap daily pull -- not a full-history fetch


def _load_last_entry():
    if not LOG_PATH.exists():
        return None
    lines = [l for l in LOG_PATH.read_text().splitlines() if l.strip()]
    if not lines:
        return None
    return json.loads(lines[-1])


def _yahoo_observation(prev_entry, today_str):
    df = yf.download(TICKER, period=f"{YAHOO_WINDOW_DAYS}d", interval="1d", auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    closes = {ts.strftime("%Y-%m-%d"): round(float(c), 6) for ts, c in df["Close"].items()}

    retroactive_changes = {}
    if prev_entry and prev_entry.get("yahoo_closes"):
        prev_closes = prev_entry["yahoo_closes"]
        for d, c in closes.items():
            if d == today_str:
                continue  # today's own close is still provisional/live, not "elapsed" data yet
            if d in prev_closes and abs(prev_closes[d] - c) > 1e-6:
                retroactive_changes[d] = {"was": prev_closes[d], "now": c}
    return closes, retroactive_changes


def _massive_minute_observation():
    end = date.today()
    start = end - timedelta(days=MASSIVE_WINDOW_DAYS)
    rows = fetch_massive_minute_ticker(TICKER, start, end)
    if not rows:
        return {"rows_fetched": 0, "exdiv_first_bar_raw_close": None}
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert("America/New_York")
    exdiv_bars = df[df["timestamp"].dt.date == EX_DIV_DATE].sort_values("timestamp")
    exdiv_first_close = float(exdiv_bars.iloc[0]["c"]) if not exdiv_bars.empty else None
    return {"rows_fetched": len(df), "exdiv_first_bar_raw_close": exdiv_first_close}


def _massive_dividends_observation():
    divs = fetch_dividends(TICKER)
    exdiv_str = EX_DIV_DATE.strftime("%Y-%m-%d")
    match = next((d for d in divs if d["ex_dividend_date"] == exdiv_str), None)
    return {
        "endpoint_reflects_target_exdiv": match is not None,
        "target_exdiv_entry": match,
        "total_dividend_rows": len(divs),
    }


def main():
    today = date.today()
    today_str = today.strftime("%Y-%m-%d")
    prev_entry = _load_last_entry()

    yahoo_closes, retroactive_changes = _yahoo_observation(prev_entry, today_str)
    massive_minute = _massive_minute_observation()
    massive_div = _massive_dividends_observation()

    entry = {
        "date": today_str,
        "days_to_exdiv": (EX_DIV_DATE - today).days,
        "yahoo_closes": yahoo_closes,
        "yahoo_retroactive_changes_vs_yesterday": retroactive_changes,
        "massive_minute": massive_minute,
        "massive_dividends": massive_div,
    }

    lines = []
    if LOG_PATH.exists():
        lines = [l for l in LOG_PATH.read_text().splitlines() if l.strip()]
    lines = [l for l in lines if json.loads(l)["date"] != today_str]  # idempotent: replace today's entry, don't duplicate
    lines.append(json.dumps(entry))
    LOG_PATH.write_text("\n".join(lines) + "\n")

    print(f"{today_str}: logged (days_to_exdiv={entry['days_to_exdiv']}). "
          f"yahoo_retroactive_changes={len(retroactive_changes)}  "
          f"massive_dividends_endpoint_reflects_target={massive_div['endpoint_reflects_target_exdiv']}  "
          f"massive_minute_rows={massive_minute['rows_fetched']}")
    if retroactive_changes:
        print(f"  *** Yahoo retroactively changed {len(retroactive_changes)} already-elapsed date(s): "
              f"{retroactive_changes}")
    if massive_div["endpoint_reflects_target_exdiv"]:
        print(f"  *** Massive dividends endpoint now reflects the {EX_DIV_DATE} ex-div: "
              f"{massive_div['target_exdiv_entry']}")


if __name__ == "__main__":
    import script_usage
    script_usage.record_invocation()
    main()
