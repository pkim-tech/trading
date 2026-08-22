"""Regression test for scripts/verify_real_trades_vs_kernel.py's
is_staged_or_manual() UTC/ET fix (2026-08-21) -- the coverage_events staged-
test window check compared entry_time (ET-native, from trade_log) against a
raw UTC coverage_events.ts with no timezone conversion. At
STAGED_TEST_WINDOW_HOURS=24 this rarely changes the outcome (the window is
wide relative to the ~4-5h UTC/ET offset), but a coverage_events row near
either edge of the window (roughly the 20-28h mark either side of
entry_time) could be misclassified in either direction:

- A genuinely staged/manual trade whose coverage_events row lands ~22-24h
  after entry_time (real ET terms) reads as OUTSIDE the raw-UTC window
  (false negative -- the offset shrinks the window's forward edge).
- A genuinely organic trade whose coverage_events row (unrelated, from a
  different node/day) lands ~24-26h before entry_time reads as INSIDE the
  raw-UTC window (false positive -- the offset widens the window's backward
  edge).

Both directions verified independently via raw SQL against a scratch DB
before being hardcoded here -- see docs/deep_backlog.md's 2026-08-21 entries
for the full incident/fix writeup and the fourth-item chain this closes."""
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import scripts.verify_real_trades_vs_kernel as vrtvk

# Same pattern as the sibling UTC/ET fix tests (test_coverage_ticket_table_timing.py,
# test_evening_status_event_days.py) -- pinned explicitly so this file passes
# deterministically on ANY host regardless of its ambient TZ (verified: passes under
# TZ=UTC too), rather than depending on the CI/dev host happening to be configured ET
# the way the real daemon host is. Correctness of the PRODUCTION code (datetime(ts,
# 'localtime')) still genuinely depends on the daemon host's real tz being ET --
# unrelated to this pin, which only controls what this test file itself observes.
os.environ['TZ'] = 'America/New_York'
time.tzset()

TICKER = 'TEST_STAGED_WINDOW'
ENTRY_TIME = '2026-08-20 10:00:00'  # ET-native, arbitrary fixed anchor -- fine here since
# this test doesn't touch trade_log's own trailing-window logic, only the fixed-offset
# arithmetic around a single entry_time value.

_SCHEMA = """
    CREATE TABLE coverage_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        scenario_key TEXT NOT NULL,
        ticker TEXT
    )
"""


def _et_delta_to_utc_ts(delta_hours):
    """UTC timestamp string for entry_time's ET wall-clock instant shifted by
    delta_hours -- built via SQLite's own datetime() engine (matching the
    production code's own arithmetic) rather than hand-computed, so a wrong
    DST assumption can't silently creep in."""
    c = sqlite3.connect(':memory:')
    try:
        et_real = c.execute("SELECT datetime(?, ?)", (ENTRY_TIME, f"{delta_hours:+d} hours")).fetchone()[0]
        return c.execute("SELECT datetime(?, 'utc')", (et_real,)).fetchone()[0]
    finally:
        c.close()


def _check(monkeypatch, tmp_path, ts_utc, scenario_key='staged_live_test'):
    db_path = tmp_path / 'scratch.db'
    con = sqlite3.connect(str(db_path))
    con.execute(_SCHEMA)
    con.execute("INSERT INTO coverage_events (ts, scenario_key, ticker) VALUES (?, ?, ?)",
                (ts_utc, scenario_key, TICKER))
    con.commit()
    con.close()
    monkeypatch.setattr(vrtvk, 'LIVE_DB', db_path)
    return vrtvk.is_staged_or_manual(TICKER, ENTRY_TIME, exit_reason='WIN')


def test_event_22h_after_entry_in_et_is_classified_staged(monkeypatch, tmp_path):
    """The actual false-negative bug shape: a coverage_events row genuinely
    22h after entry_time in ET terms -- well inside the intended +/-24h
    window -- must be classified as staged. Verified directly (2026-08-21):
    the old raw-ts comparison excluded this (UTC clock reads ~4h ahead of
    ET, pushing the row's raw ts past the naive +24h boundary)."""
    ts_utc = _et_delta_to_utc_ts(22)
    assert _check(monkeypatch, tmp_path, ts_utc) is True


def test_event_26h_before_entry_in_et_is_not_classified_staged(monkeypatch, tmp_path):
    """The mirror false-positive bug shape: a coverage_events row genuinely
    26h before entry_time in ET terms -- outside the intended +/-24h window
    -- must NOT be classified as staged. Verified directly (2026-08-21): the
    old raw-ts comparison included this (UTC clock reads ~4h ahead, pulling
    the row's raw ts inside the naive -24h boundary)."""
    ts_utc = _et_delta_to_utc_ts(-26)
    assert _check(monkeypatch, tmp_path, ts_utc) is False


def test_event_well_inside_window_is_classified_staged(monkeypatch, tmp_path):
    """Control: a coverage_events row comfortably inside the window (2h after
    entry) must be classified as staged under both old and new logic."""
    ts_utc = _et_delta_to_utc_ts(2)
    assert _check(monkeypatch, tmp_path, ts_utc) is True


def test_event_well_outside_window_is_not_classified_staged(monkeypatch, tmp_path):
    """Control: a coverage_events row comfortably outside the window (48h
    after entry) must NOT be classified as staged under both old and new
    logic."""
    ts_utc = _et_delta_to_utc_ts(48)
    assert _check(monkeypatch, tmp_path, ts_utc) is False


def test_manual_exit_reason_short_circuits_without_touching_the_db(monkeypatch, tmp_path):
    """exit_reason='MANUAL' is a direct trade_log signal -- must return True
    without even querying coverage_events (LIVE_DB deliberately left
    unpatched/invalid to prove this)."""
    monkeypatch.setattr(vrtvk, 'LIVE_DB', tmp_path / 'does_not_exist.db')
    assert vrtvk.is_staged_or_manual(TICKER, ENTRY_TIME, exit_reason='MANUAL') is True
