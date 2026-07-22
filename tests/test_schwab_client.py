import schwab_client


class _FakeResponse:
    def raise_for_status(self):
        pass


def test_submit_order_with_retry_succeeds_after_transient_failures(monkeypatch):
    """A generic (non-SafetyViolation) exception -- timeout, connection error,
    a transient 5xx -- is worth retrying: the same call may simply succeed a
    moment later. Requested 2026-07-22 for any real order-placement path
    (BUY, top-up, SELL, SL), not just the SL-placement fallback alert."""
    monkeypatch.setattr(schwab_client, '_ORDER_SUBMIT_RETRY_INTERVAL_SECS', 0)
    calls = []

    class FakeClient:
        def place_order(self, account_hash, order):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("connection reset")
            return _FakeResponse()

    monkeypatch.setattr(schwab_client, '_get_client', lambda: FakeClient())
    r = schwab_client._submit_order_with_retry('hash123', object())
    assert isinstance(r, _FakeResponse)
    assert len(calls) == 3


def test_submit_order_with_retry_raises_after_exhausting_attempts(monkeypatch):
    monkeypatch.setattr(schwab_client, '_ORDER_SUBMIT_RETRY_INTERVAL_SECS', 0)
    calls = []

    class FakeClient:
        def place_order(self, account_hash, order):
            calls.append(1)
            raise RuntimeError("connection reset")

    monkeypatch.setattr(schwab_client, '_get_client', lambda: FakeClient())
    try:
        schwab_client._submit_order_with_retry('hash123', object())
        assert False, "expected RuntimeError to propagate"
    except RuntimeError as e:
        assert "connection reset" in str(e)
    assert len(calls) == schwab_client._ORDER_SUBMIT_RETRY_ATTEMPTS


def test_submit_order_with_retry_succeeds_first_try_no_retry_needed(monkeypatch):
    calls = []

    class FakeClient:
        def place_order(self, account_hash, order):
            calls.append(1)
            return _FakeResponse()

    monkeypatch.setattr(schwab_client, '_get_client', lambda: FakeClient())
    schwab_client._submit_order_with_retry('hash123', object())
    assert len(calls) == 1


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
