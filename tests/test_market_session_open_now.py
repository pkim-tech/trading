"""Direct unit coverage for signals_notify._market_session_open_now, added
2026-08-19/20 to close the NYSE-early-close gap flagged in docs/backlog_cache.md's
"Deferred 2026-08-19 (from market-hours-guard paired review)" finding (1):
the function used to hardcode a 16:00:00 regular-session close, so a real
NYSE early-close day (day after Thanksgiving, Christmas Eve, ~2x/year,
13:00 ET) would read as still-open for 3 extra hours -- reproducing incident
#13's exact shape (a MARKET order replacing a resting stop with nothing to
fill against)."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_notify

# 2026-11-27 is the real day-after-Thanksgiving NYSE early close (13:00 ET).
EARLY_CLOSE_DATE = '2026-11-27'
# A completely ordinary regular-session Tuesday, no holiday involved.
REGULAR_DATE = '2026-11-24'


def test_regular_day_open_during_normal_hours():
    now = datetime.strptime(f"{REGULAR_DATE} 15:59:59", '%Y-%m-%d %H:%M:%S')
    assert signals_notify._market_session_open_now(now) is True


def test_regular_day_closed_at_and_after_16_00_00():
    at_close = datetime.strptime(f"{REGULAR_DATE} 16:00:00", '%Y-%m-%d %H:%M:%S')
    assert signals_notify._market_session_open_now(at_close) is False
    after_close = datetime.strptime(f"{REGULAR_DATE} 16:00:52", '%Y-%m-%d %H:%M:%S')
    assert signals_notify._market_session_open_now(after_close) is False


def test_regular_day_closed_before_9_30():
    before_open = datetime.strptime(f"{REGULAR_DATE} 09:29:59", '%Y-%m-%d %H:%M:%S')
    assert signals_notify._market_session_open_now(before_open) is False


def test_early_close_day_open_before_13_00():
    now = datetime.strptime(f"{EARLY_CLOSE_DATE} 12:59:59", '%Y-%m-%d %H:%M:%S')
    assert signals_notify._market_session_open_now(now) is True


def test_early_close_day_closed_at_and_after_13_00():
    """The real fix, asserted directly: incident #13's exact shape (a bar-close
    exit decision landing seconds after the real close) reproduced on an
    early-close day, at the EARLY close time -- 16:00:52 would have (wrongly)
    read as open under the old hardcoded-16:00:00 bound."""
    at_close = datetime.strptime(f"{EARLY_CLOSE_DATE} 13:00:00", '%Y-%m-%d %H:%M:%S')
    assert signals_notify._market_session_open_now(at_close) is False
    # The old hardcoded bound would have said "open" here -- this is the
    # actual regression the fix closes.
    still_before_old_hardcoded_close = datetime.strptime(
        f"{EARLY_CLOSE_DATE} 15:30:00", '%Y-%m-%d %H:%M:%S')
    assert signals_notify._market_session_open_now(still_before_old_hardcoded_close) is False


def test_weekend_closed():
    # 2026-11-28 is a Saturday.
    now = datetime.strptime("2026-11-28 12:00:00", '%Y-%m-%d %H:%M:%S')
    assert signals_notify._market_session_open_now(now) is False


def test_market_holiday_closed():
    # Thanksgiving itself, 2026-11-26 -- a full NYSE holiday, not an early close.
    now = datetime.strptime("2026-11-26 12:00:00", '%Y-%m-%d %H:%M:%S')
    assert signals_notify._market_session_open_now(now) is False
