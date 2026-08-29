"""SPY corp-action canary: observes whether/when Yahoo's and Massive's data
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

Cadence, 2026-08-28 (revised -- supersedes the original once-daily design from
commit 6114b3a): this script is meant to be invoked every 15 minutes, all day
(not just market hours -- a vendor could push a retroactive rebase at any
time, that's part of the unknown this exists to resolve). 15 minutes was
picked as the midpoint of the requested 5-30 minute range: frequent enough to
pinpoint a rebase to a fairly tight interval without generating an excessive
number of near-identical log lines while nothing is happening (the expected
state for weeks). Rate limiting is NOT a reason to run any of the 3 checks
below on a slower cadence than the other two: this script issues exactly 2
Massive calls per invocation (one minute-bar fetch, one dividends fetch) vs.
Massive's free-tier budget of 5 calls/min (see build_massive_hourly_derived.py
SLEEP_BETWEEN_TICKERS's 12s/call-minimum comment) -- even invoked every 5
minutes (the fastest allowed cadence) that's nowhere close to the budget, so
all 3 checks stay on one shared cadence rather than being decoupled.

Each invocation:
  1. Fetches a fresh window of SPY daily closes from Yahoo (yf.download,
     auto_adjust=True, same convention as data_manager.py's existing hourly
     fetch) and compares them against the LAST POLL's logged closes (not
     specifically "yesterday" -- any prior poll works, since sub-daily
     resolution is the whole point now) for the same already-elapsed dates --
     any change is direct proof of a retroactive Yahoo rebase, and pinpoints
     the exact ~15-minute interval it happened in. This is design.md's part-1
     self-consistency detector, used here purely as an observation -- it is
     NOT wired into signals_compute.py or any live code path.
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

Logs one JSON line per POLL (not per day -- expect many lines/day at a
15-minute cadence) to docs/corp_action_canary_log.jsonl (committed,
append-only). Chosen over docs/research_log.md's free-form prose because this
needs weeks of dense, structured, machine-diffable entries -- a human or a
future script can read consecutive lines to see exactly which poll each
source's data changed, without parsing prose. Each line carries its own
`poll_timestamp` (ISO-8601, local time, second resolution) so consecutive
same-day polls never collide; nothing is ever replaced or deduped by day
anymore -- every invocation appends a new line. (The original once-daily
script's per-day idempotent-replace logic is gone: it doesn't make sense once
multiple polls/day are expected, and the whole point of this revision is
retaining every poll as its own data point.)

Non-goals (explicit, do not extend here): no alerting/paging -- this is silent
data collection; no crontab wiring (a separate step, the user's own to set up
once this script is confirmed working -- this script only needs to behave
correctly when invoked, not schedule itself); SPY only -- not a general
corp-action detection system (see docs/design.md's 2026-08-28 (late) entry for
that separate, bigger-scope, not-yet-built idea).

Usage:
    .venv/bin/python scripts/corp_action_canary.py
"""
import json
import os
import sys
from datetime import date, datetime, timedelta
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
MASSIVE_WINDOW_DAYS = 5  # small, cheap pull -- not a full-history fetch


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
    now = datetime.now()
    today = now.date()
    today_str = today.strftime("%Y-%m-%d")
    poll_timestamp = now.isoformat(timespec="seconds")
    prev_entry = _load_last_entry()

    yahoo_closes, retroactive_changes = _yahoo_observation(prev_entry, today_str)
    massive_minute = _massive_minute_observation()
    massive_div = _massive_dividends_observation()

    entry = {
        "poll_timestamp": poll_timestamp,
        "date": today_str,
        "days_to_exdiv": (EX_DIV_DATE - today).days,
        "yahoo_closes": yahoo_closes,
        "yahoo_retroactive_changes_vs_last_poll": retroactive_changes,
        "massive_minute": massive_minute,
        "massive_dividends": massive_div,
    }

    # Every invocation is its own poll -- append, never replace/dedupe by day
    # (the old once-daily script deduped same-day reruns; that no longer
    # applies now that multiple polls/day are the intended, normal case).
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")

    print(f"{poll_timestamp}: logged (days_to_exdiv={entry['days_to_exdiv']}). "
          f"yahoo_retroactive_changes={len(retroactive_changes)}  "
          f"massive_dividends_endpoint_reflects_target={massive_div['endpoint_reflects_target_exdiv']}  "
          f"massive_minute_rows={massive_minute['rows_fetched']}")
    if retroactive_changes:
        print(f"  *** Yahoo retroactively changed {len(retroactive_changes)} already-elapsed date(s) "
              f"since the last poll: {retroactive_changes}")
    if massive_div["endpoint_reflects_target_exdiv"]:
        print(f"  *** Massive dividends endpoint now reflects the {EX_DIV_DATE} ex-div: "
              f"{massive_div['target_exdiv_entry']}")


if __name__ == "__main__":
    import script_usage
    script_usage.record_invocation()
    main()
