"""Regression test for scripts/evening_status.py's event_days_by_scenario()
UTC/ET fix (2026-08-21) -- the daily-vs-edge-case classification query used to
group coverage_events by raw UTC date(ts), so a scenario that fires close to
midnight ET could have its real single ET calendar day split across two UTC
calendar days (or two real ET days folded into the same UTC day), skewing the
distinct-day count that decides whether a scenario reads as "daily" (>=7 of
last 14 days) vs "edge case". Also covers the WHERE-bound fix (independent-
cold review, same day, found the original "leave it UTC" reasoning was
empirically wrong against real data: a plain `date('now','-14 days')` bound
resolves to UTC midnight of day-14, which is 20:00 ET on day-15, so it could
both wrongly include an event that's genuinely more than 14 ET-days old and
make the window's real width vary by up to a full ET day depending on what
time of evening this ET-named script happens to run). See
docs/deep_backlog.md's 2026-08-21 entries for the full incident/fix writeup.

All test timestamps are constructed via SQLite's own datetime()/'localtime'
modifiers relative to the real current time, verified independently against a
scratch in-memory DB before being hardcoded as UTC strings here -- NOT
hardcoded to a specific calendar date. A first version of this file used fixed
2026-08-20/21/22 date literals, which would have silently started failing
once those dates aged out of the function's own trailing-14-day WHERE filter
(~2026-09-04) -- a bug in the test itself, caught by the same review round
that found the WHERE-bound production issue."""
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import scripts.evening_status as es

# Every assertion below depends on SQLite's 'localtime' modifier resolving to ET --
# pinned explicitly (same pattern as coverage_check.py-adjacent tests) rather than
# relying on the host's ambient TZ, so this file fails loudly and obviously under a
# UTC-configured CI runner instead of silently asserting the wrong thing.
os.environ['TZ'] = 'America/New_York'
time.tzset()

_SCHEMA = """
    CREATE TABLE coverage_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        scenario_key TEXT NOT NULL
    )
"""
_FMT = '%Y-%m-%d %H:%M:%S'


def _db_with_events(rows):
    """rows: list of (scenario_key, ts_utc) -- builds an in-memory DB, no
    real trading_live.db touched."""
    con = sqlite3.connect(':memory:')
    con.execute(_SCHEMA)
    con.executemany("INSERT INTO coverage_events (scenario_key, ts) VALUES (?, ?)", rows)
    con.commit()
    return con


def _et_instant_utc(offset_expr):
    """Returns the UTC timestamp string for an ET wall-clock instant expressed
    as a SQLite modifier chain applied after 'now','localtime' (e.g.
    "'-2 days','start of day','+23 hours','+50 minutes'" for 23:50 ET, 2 ET-
    calendar-days ago). Building these via SQLite's own datetime engine (the
    same one event_days_by_scenario relies on) instead of hand-computed
    offsets avoids silently baking in a wrong DST assumption."""
    probe = sqlite3.connect(':memory:')
    try:
        return probe.execute(
            f"SELECT datetime(datetime('now','localtime',{offset_expr}), 'utc')"
        ).fetchone()[0]
    finally:
        probe.close()


def _utc_now():
    return datetime.utcnow()


def _fmt(dt):
    return dt.strftime(_FMT)


def test_near_midnight_et_events_on_same_real_et_day_count_as_one_day():
    """Two events 40 minutes apart, both clearly inside the 14-day window and
    both really landing on the same ET calendar day -- a sanity/control case:
    one real ET day must count as exactly one day."""
    base = _utc_now() - timedelta(days=2)
    con = _db_with_events([
        ('sk', _fmt(base)),
        ('sk', _fmt(base + timedelta(minutes=40))),
    ])
    assert es.event_days_by_scenario(con) == {'sk': 1}


def test_events_on_two_different_real_et_days_sharing_one_utc_date_count_as_two_days():
    """The actual bug shape: an event at 23:50 ET on one real ET calendar day
    and an event at 00:10 ET the following real ET calendar day are two
    DIFFERENT ET days, but (given ET runs ~4-5h behind UTC) can land on the
    SAME raw UTC calendar date -- the old `COUNT(DISTINCT date(ts))` grouping
    would have collapsed these into a single day, undercounting real distinct
    firing days by one. The fixed date(ts,'localtime') grouping correctly
    resolves these to two distinct ET dates."""
    day1_2350_et = _et_instant_utc("'-2 days','start of day','+23 hours','+50 minutes'")
    day2_0010_et = _et_instant_utc("'-1 days','start of day','+0 hours','+10 minutes'")
    con = _db_with_events([
        ('sk', day1_2350_et),
        ('sk', day2_0010_et),
    ])
    assert es.event_days_by_scenario(con) == {'sk': 2}, (
        "two events on genuinely different ET calendar days (23:50 one day, "
        "00:10 the next) must count as 2 distinct days even though their raw "
        "UTC timestamps can share a calendar date"
    )


def test_events_on_two_different_real_utc_dates_sharing_one_et_day_count_as_one_day():
    """Mirror-image case: two events on different raw UTC calendar dates that
    are actually the SAME real ET day (early-morning and late-evening ET, the
    UTC date rolls over in between). The old grouping would have overcounted
    this as 2 days; the fix correctly collapses it to 1."""
    et_0100 = _et_instant_utc("'-2 days','start of day','+1 hours'")
    et_2300 = _et_instant_utc("'-2 days','start of day','+23 hours'")
    con = _db_with_events([
        ('sk', et_0100),
        ('sk', et_2300),
    ])
    assert es.event_days_by_scenario(con) == {'sk': 1}


def test_events_over_14_day_window_still_grouped_per_scenario():
    """Multiple scenarios, mix of same-day and different-day events, confirms
    grouping stays correctly keyed per scenario_key after the fix."""
    base = _utc_now() - timedelta(days=3)
    con = _db_with_events([
        ('daily_sk', _fmt(base)),
        ('daily_sk', _fmt(base + timedelta(days=1))),
        ('other_sk', _fmt(base)),
    ])
    result = es.event_days_by_scenario(con)
    assert result['daily_sk'] == 2
    assert result['other_sk'] == 1


def test_event_just_inside_14_et_day_window_is_counted():
    """WHERE-bound test: an event 13 ET-calendar-days ago (comfortably inside
    any reasonable interpretation of a 14-day window) must always be counted."""
    inside = _et_instant_utc("'-13 days','start of day','+12 hours'")
    con = _db_with_events([('sk', inside)])
    assert es.event_days_by_scenario(con) == {'sk': 1}


def test_event_more_than_14_et_days_old_is_excluded_even_though_old_utc_bound_included_it():
    """WHERE-bound boundary test, the actual finding: 15 real ET-calendar-days
    ago at 20:30 ET. Verified (2026-08-21) that a plain `ts >= date('now',
    '-14 days')` UTC bound wrongly included this event (its raw UTC timestamp
    still compares >= the UTC-anchored boundary string), while the fixed
    `date(ts,'localtime') >= date('now','localtime','-14 days')` bound
    correctly excludes it, since its real ET calendar date IS 15 days old.
    This only passes once the WHERE bound itself is converted via
    'localtime', not just the per-day grouping."""
    just_outside = _et_instant_utc("'-15 days','start of day','+20 hours','+30 minutes'")
    con = _db_with_events([('sk', just_outside)])
    assert es.event_days_by_scenario(con) == {}, (
        "an event genuinely more than 14 ET-calendar-days old must be "
        "excluded -- the old plain-UTC WHERE bound would have wrongly "
        "included this one"
    )
