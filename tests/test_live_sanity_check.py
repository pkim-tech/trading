"""Unit coverage for scripts.live_sanity_check.run_one's post-placement outcome
classification (Task #7/incident #16 fix, 2026-08-31): the real bug was
declaring 'unexpectedly_accepted' off a bare HTTP 201 (r.raise_for_status()
succeeding) with no follow-up poll of the order's real terminal status --
the same false-positive pattern that already produced 2 historical manual DB
corrections (2026-07-23/24, docs/deep_backlog.md ~line 6470). Fix polls via
schwab_client._confirm_order_status, same as every other real placement call
site. No fake_broker fixture used here -- this script bypasses schwab_safety/
active_signals entirely by design (calls the schwab-py client directly), so
there's nothing for fake_broker's order book to intercept; mocked at the
client/schwab_client boundary instead."""
from unittest.mock import MagicMock

import pytest

import scripts.live_sanity_check as lsc


class _FakeResponse:
    def __init__(self, status_code=201):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def common_mocks(monkeypatch):
    monkeypatch.setattr(lsc, "_confirm", lambda prompt: "SOXL")  # always confirms
    monkeypatch.setattr(lsc, "_post_message", MagicMock())
    monkeypatch.setattr(lsc, "get_current_price", lambda ticker: 10.0)
    monkeypatch.setattr(lsc, "_real_balance_and_position", lambda client, h, t: (1000.0, 0.0))
    events = []
    incidents = []
    monkeypatch.setattr(lsc.db, "log_coverage_event",
                         lambda *a, **kw: events.append((a, kw)))
    monkeypatch.setattr(lsc.db, "log_incident",
                         lambda *a, **kw: incidents.append((a, kw)))
    monkeypatch.setattr(lsc.Utils, "extract_order_id", lambda self, r: 999)
    return events, incidents


def _client_with_place_order(response_or_exc):
    client = MagicMock()
    if isinstance(response_or_exc, Exception):
        client.place_order = MagicMock(side_effect=response_or_exc)
    else:
        client.place_order = MagicMock(return_value=response_or_exc)
    return client


def test_http_rejection_logs_rejected_as_expected(common_mocks, monkeypatch):
    """place_order itself raises (HTTP-level rejection) -- no status poll needed,
    real pre-existing behavior, still correct after the fix."""
    events, incidents = common_mocks
    poll = MagicMock()
    monkeypatch.setattr(lsc.schwab_client, "_confirm_order_status", poll)
    client = _client_with_place_order(RuntimeError("HTTP 400"))
    lsc.run_one(client, "hash", "SOXL", "naked_sell", "test_account")
    assert events[-1][1]["result"] == "rejected_as_expected"
    assert incidents == []
    poll.assert_not_called()  # short-circuits on the HTTP-level exception, no poll needed


def test_status_poll_confirms_rejected(common_mocks, monkeypatch):
    """HTTP succeeded (201) but the real status poll confirms REJECTED -- the
    exact historical false-positive case (id=216/148 in the live DB) this fix
    targets. Must NOT log 'unexpectedly_accepted'."""
    events, incidents = common_mocks
    monkeypatch.setattr(lsc.schwab_client, "_confirm_order_status", lambda h, oid: "REJECTED")
    client = _client_with_place_order(_FakeResponse(201))
    lsc.run_one(client, "hash", "SOXL", "naked_sell", "test_account")
    assert events[-1][1]["result"] == "rejected_as_expected"
    assert "status=REJECTED" in events[-1][1]["detail"]
    assert incidents == []


def test_status_poll_confirms_filled_is_real_risk(common_mocks, monkeypatch):
    """Status poll confirms FILLED -- genuine acceptance, the real-risk case."""
    events, incidents = common_mocks
    monkeypatch.setattr(lsc.schwab_client, "_confirm_order_status", lambda h, oid: "FILLED")
    client = _client_with_place_order(_FakeResponse(201))
    lsc.run_one(client, "hash", "SOXL", "naked_sell", "test_account")
    assert events[-1][1]["result"] == "unexpectedly_accepted"
    assert "status=FILLED" in events[-1][1]["detail"]
    assert len(incidents) == 1
    assert incidents[0][1]["real_money_impact"] is True


def test_status_poll_unconfirmed_fails_cautious(common_mocks, monkeypatch):
    """Poll itself fails (None) -- must NOT guess either outcome; logs a
    distinct 'status_unconfirmed' result and still raises an incident (this
    position is untracked either way)."""
    events, incidents = common_mocks
    monkeypatch.setattr(lsc.schwab_client, "_confirm_order_status", lambda h, oid: None)
    client = _client_with_place_order(_FakeResponse(201))
    lsc.run_one(client, "hash", "SOXL", "naked_sell", "test_account")
    assert events[-1][1]["result"] == "status_unconfirmed"
    assert len(incidents) == 1
    assert incidents[0][1]["real_money_impact"] is True


def test_status_poll_working_treated_as_accepted(common_mocks, monkeypatch):
    """A non-terminal-bad, non-FILLED status (e.g. still WORKING) is real
    acceptance too, not a rejection -- must not be misread as success."""
    events, incidents = common_mocks
    monkeypatch.setattr(lsc.schwab_client, "_confirm_order_status", lambda h, oid: "WORKING")
    client = _client_with_place_order(_FakeResponse(201))
    lsc.run_one(client, "hash", "SOXL", "oversized_buy", "test_account")
    assert events[-1][1]["result"] == "unexpectedly_accepted"
    assert len(incidents) == 1
