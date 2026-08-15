import schwab_client


class _FakeResponse:
    """Mimics the subset of a real schwab-py placement response that
    _submit_order_with_retry's internal Utils(...).extract_order_id(r) call
    now needs (moved in-loop 2026-08-15, see schwab_client.py's retry
    docstrings) -- is_error/status_code/headers, in addition to the
    raise_for_status()/json() the rest of this module already used."""

    def __init__(self, json_data=None, order_id=None, account_hash='hash123', is_error=False):
        self._json_data = json_data
        self.is_error = is_error
        self.status_code = 400 if is_error else 201
        self.headers = ({'Location': f'https://api.schwabapi.com/trader/v1/accounts/{account_hash}/orders/{order_id}'}
                         if order_id is not None else {})

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_data


class _FakeBalanceClient:
    def __init__(self, balances):
        self._balances = balances

    def get_account(self, account_hash, fields=None):
        return _FakeResponse({"securitiesAccount": {"currentBalances": self._balances}})


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
            return _FakeResponse(order_id=999)

    monkeypatch.setattr(schwab_client, '_get_client', lambda: FakeClient())
    order_id = schwab_client._submit_order_with_retry('hash123', object())
    assert order_id == 999
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
            return _FakeResponse(order_id=555)

    monkeypatch.setattr(schwab_client, '_get_client', lambda: FakeClient())
    order_id = schwab_client._submit_order_with_retry('hash123', object())
    assert order_id == 555
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

    monkeypatch.setattr(schwab_client.schwab_auth, "get_client", lambda interactive=False: FakeClient())

    schwab_client._get_client()

    assert calls == [schwab_client._CLIENT_TIMEOUT_SECS]

    schwab_client._client = None


# ---------------------------------------------------------------------------
# get_account_balance field-preference (2026-08-12)
# ---------------------------------------------------------------------------

def _setup_balance(monkeypatch, balances):
    monkeypatch.setattr(schwab_client, '_get_client', lambda: _FakeBalanceClient(balances))
    monkeypatch.setattr(schwab_client, '_resolve_account_hashes', lambda: {'brokerage': 'hash123'})


def test_get_account_balance_prefers_cash_balance_when_present(monkeypatch):
    """Real settled cash, confirmed 2026-08-12 -- must win over the
    margin-inclusive availableFunds even when they differ (e.g. real margin
    drawn against a held position)."""
    _setup_balance(monkeypatch, {'cashBalance': 20000.0, 'availableFunds': 40000.0})
    assert schwab_client.get_account_balance('brokerage') == 20000.0


def test_get_account_balance_falls_back_to_cash_available_for_trading(monkeypatch):
    _setup_balance(monkeypatch, {'cashAvailableForTrading': 15000.0, 'availableFunds': 40000.0})
    assert schwab_client.get_account_balance('brokerage') == 15000.0


def test_get_account_balance_falls_back_to_available_funds_when_nothing_else_present(monkeypatch):
    """Real response shape confirmed 2026-08-12 to never actually hit this
    branch on a live account (cashBalance always present) -- kept as a
    fail-safe for a response shape not yet seen, not the expected path."""
    _setup_balance(monkeypatch, {'availableFunds': 40000.0})
    assert schwab_client.get_account_balance('brokerage') == 40000.0
