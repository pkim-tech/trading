"""stop_status (2026-08-01): distinguishes what broker_stop_price actually
tells us for a position, since None was previously ambiguous between "an
automated stop should exist but doesn't" (a real anomaly) and "this position
was never automation-scoped" (expected, nothing to detect a failure
against) -- both used to render the identical generic guess in the SL alert.

The dry-run branch was added after an Opus review of the first version
caught it rendering a permanent false "placement failure" alarm for every
manually-confirmed position in a dry_run account -- schwab_client.place_stop_
loss short-circuits and never places anything for those, so broker_stop_price
can structurally never be recorded there regardless of automation scope."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import schwab_safety
from signals_helpers import stop_status


def test_known_when_broker_stop_price_on_file(monkeypatch):
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {'AGQ'})
    status, bsp = stop_status({'ticker': 'AGQ', 'account': 'soxl_ira', 'broker_stop_price': 62.83})
    assert status == 'known'
    assert bsp == 62.83


def test_automation_pending_when_scoped_real_account_no_price_on_file(monkeypatch):
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {'AGQ'})
    status, bsp = stop_status({'ticker': 'AGQ', 'account': 'soxl_ira', 'broker_stop_price': None})
    assert status == 'automation-pending'
    assert bsp is None


def test_manual_when_not_automation_scoped_real_account(monkeypatch):
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {'AGQ'})
    status, bsp = stop_status({'ticker': 'SH', 'account': 'soxl_ira', 'broker_stop_price': None})
    assert status == 'manual'
    assert bsp is None


def test_zero_price_is_not_trusted_as_known(monkeypatch):
    # A falsy-but-present broker_stop_price (0.0) should never be trusted as
    # 'known' -- only a real positive price counts. Uses an in-scope ticker
    # on a real account so it actually exercises the falsy check rather than
    # short-circuiting on scope/account first (the original version of this
    # test used an out-of-scope ticker and passed vacuously).
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {'AGQ'})
    status, bsp = stop_status({'ticker': 'AGQ', 'account': 'soxl_ira', 'broker_stop_price': 0.0})
    assert status == 'automation-pending'
    assert bsp is None


def test_dry_run_account_never_renders_as_automation_pending(monkeypatch):
    # 'ira' is dry_run=True in schwab_safety.ACCOUNTS -- even though AGQ is
    # automation-scoped, no automated placement ever really happens for this
    # account, so this must NOT render as a placement-failure anomaly.
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {'AGQ'})
    status, bsp = stop_status({'ticker': 'AGQ', 'account': 'ira', 'broker_stop_price': None})
    assert status == 'dry-run'
    assert bsp is None


def test_dry_run_takes_precedence_over_ticker_scope_regardless_of_scope():
    status, bsp = stop_status({'ticker': 'SH', 'account': 'ira', 'broker_stop_price': None})
    assert status == 'dry-run'


def test_unrecognized_account_falls_back_to_scope_check(monkeypatch):
    # No entry in schwab_safety.ACCOUNTS (e.g. a legacy/unmapped position) --
    # should fail toward the scope check rather than crash or silently assume
    # dry-run.
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {'AGQ'})
    status, bsp = stop_status({'ticker': 'AGQ', 'account': 'nonexistent_account', 'broker_stop_price': None})
    assert status == 'automation-pending'
