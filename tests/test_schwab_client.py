import schwab_client


def test_get_client_applies_short_timeout(monkeypatch):
    """Regression test for the Opus-review finding (2026-07-21): a hung
    Schwab call inside schwab_safety's cross-account lock stalls every
    account's order processing. _get_client() must bound every call to
    _CLIENT_TIMEOUT_SECS, not schwab-py's 30s default."""
    schwab_client._client = None

    calls = []

    class FakeClient:
        def set_timeout(self, timeout):
            calls.append(timeout)

    monkeypatch.setattr(schwab_client.schwab_auth, "get_client", lambda: FakeClient())

    schwab_client._get_client()

    assert calls == [schwab_client._CLIENT_TIMEOUT_SECS]

    schwab_client._client = None
