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


# ---------------------------------------------------------------------------
# get_current_price -- stale extended.lastPrice (2026-08-18 real incident)
# ---------------------------------------------------------------------------

class _FakeQuoteClient:
    def __init__(self, payload):
        self._payload = payload

    def get_quote(self, ticker):
        return _FakeResponse({ticker: self._payload})


def test_get_current_price_ignores_stale_extended_lastprice(monkeypatch):
    """Real bug confirmed live 2026-08-18 ~18:37 ET (market closed): SOXL's
    raw quote had extended.lastPrice=142.98 with extended.quoteTime=0 and
    tradeTime ~14h stale (bid/ask/size all zeroed -- a leftover from a prior
    session), while quote.lastPrice=127.55 had a tradeTime from the same
    minute as the fetch and agreed with regular.regularMarketLastPrice. The
    old `extended.get('lastPrice') or quote['quote']['lastPrice']` returned
    the stale extended price -- 11%+ off the real tradable price. Must
    prefer quote.lastPrice whenever extended isn't actually fresher."""
    fresh_time = 1755548400000  # some ms epoch "now"
    stale_time = fresh_time - 14 * 3600 * 1000
    payload = {
        "extended": {
            "lastPrice": 142.98,
            "quoteTime": 0,
            "tradeTime": stale_time,
            "bidPrice": 0,
            "askPrice": 0,
            "bidSize": 0,
            "askSize": 0,
        },
        "quote": {
            "lastPrice": 127.55,
            "quoteTime": fresh_time,
            "tradeTime": fresh_time,
        },
    }
    monkeypatch.setattr(schwab_client, '_get_client', lambda: _FakeQuoteClient(payload))
    assert schwab_client.get_current_price('SOXL') == 127.55


def test_get_current_price_prefers_extended_when_genuinely_fresher(monkeypatch):
    """Positive path: a real pre-market/after-hours tick with a newer
    timestamp than the last regular-session trade should still win --
    preserves the original intent behind checking extended.lastPrice at all."""
    quote_time = 1755500000000
    ext_time = quote_time + 5 * 60 * 1000  # 5 minutes newer
    payload = {
        "extended": {
            "lastPrice": 130.25,
            "quoteTime": ext_time,
            "tradeTime": ext_time,
            "bidPrice": 130.0,
            "askPrice": 130.5,
            "bidSize": 100,
            "askSize": 100,
        },
        "quote": {
            "lastPrice": 129.10,
            "quoteTime": quote_time,
            "tradeTime": quote_time,
        },
    }
    monkeypatch.setattr(schwab_client, '_get_client', lambda: _FakeQuoteClient(payload))
    assert schwab_client.get_current_price('SOXL') == 130.25


def test_get_current_price_tie_timestamps_prefers_quote(monkeypatch):
    """Equal (or both-zero/both-missing) tradeTime on both sides must not
    treat extended as fresher -- strict `>` means quote.lastPrice wins the
    tie, the conservative default (never trust extended without positive
    evidence it's newer)."""
    payload = {
        "extended": {"lastPrice": 999.0, "tradeTime": 100},
        "quote": {"lastPrice": 127.55, "tradeTime": 100},
    }
    monkeypatch.setattr(schwab_client, '_get_client', lambda: _FakeQuoteClient(payload))
    assert schwab_client.get_current_price('SOXL') == 127.55


def test_get_current_price_falls_through_to_extended_when_quote_lastprice_is_zero(monkeypatch):
    """Real regression risk in the fix: quote.lastPrice=0/None (e.g. a
    halted or not-yet-opened symbol) must not return 0.0 into SL/gap-resize/
    top-up sizing -- the old `or` chain would have used extended's price
    here, and that fallback must survive."""
    payload = {
        "extended": {"lastPrice": 42.0, "tradeTime": 0},
        "quote": {"lastPrice": 0, "tradeTime": 0},
    }
    monkeypatch.setattr(schwab_client, '_get_client', lambda: _FakeQuoteClient(payload))
    assert schwab_client.get_current_price('SOXL') == 42.0


def test_get_current_price_missing_quote_block_falls_through_to_extended(monkeypatch):
    """A payload carrying `extended` but no `quote` key at all must not
    KeyError before the extended-price fallback is even considered."""
    payload = {
        "extended": {"lastPrice": 42.0, "tradeTime": 0},
    }
    monkeypatch.setattr(schwab_client, '_get_client', lambda: _FakeQuoteClient(payload))
    assert schwab_client.get_current_price('SOXL') == 42.0


def test_get_current_price_falls_back_to_yfinance_when_both_prices_missing(monkeypatch):
    """Neither side has a usable price -- must raise internally and hit the
    yfinance fallback rather than return 0.0."""
    payload = {
        "extended": {"lastPrice": None, "tradeTime": 0},
        "quote": {"lastPrice": 0, "tradeTime": 0},
    }
    monkeypatch.setattr(schwab_client, '_get_client', lambda: _FakeQuoteClient(payload))

    class _FakeFastInfo:
        last_price = 55.5

    class _FakeYfTicker:
        def __init__(self, ticker):
            pass
        fast_info = _FakeFastInfo()

    import yfinance
    monkeypatch.setattr(yfinance, 'Ticker', _FakeYfTicker)
    assert schwab_client.get_current_price('SOXL') == 55.5
