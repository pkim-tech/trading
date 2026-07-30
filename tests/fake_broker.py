"""Stateful fake Schwab broker for exercising real (non-dry_run) order-placement
code paths against a controlled, evolving order book -- not a per-call mock of
one wrapper function returning a canned value.

Built 2026-07-29 in direct response to the SH stuck-exit incident: the existing
test style (monkeypatch one schwab_client function per test) proved every call
in isolation "worked," but never modeled a real order-book *sequence*
(place -> hours pass -> a second decision reads the still-resting order -> a
guard reacts to it) -- exactly the shape of bug that hid behind passing tests
(the resting-order self-block bug, and the TIME-while-armed bug this fixture
was built to pin down). Patches at the schwab-py client boundary
(`schwab_client._get_client()`), so every real wrapper function in
schwab_client.py and every guard in schwab_safety.py (_all_orders/_open_orders)
runs completely unmodified against fake state.

Usage:
    def test_something(fake_broker):
        fake_broker.set_quote('SH', last=33.61, bid=33.60, ask=33.62)
        order_id = fake_broker.seed_resting_order(
            'soxl_ira', 'SH', 'STOP', 'SELL', 50, stop_price=26.57)
        # ... call real production code (signals_notify.notify_sell_signal, etc) ...
        assert fake_broker.orders[order_id]['status'] == 'FILLED'
"""
import itertools
from datetime import datetime, timezone

import pytest

import schwab_client


class FakeResponse:
    """Mimics the subset of requests.Response schwab-py callers actually use:
    .raise_for_status() (never raises here -- a fake network-level failure
    isn't this fixture's concern) and .json(). order_id is a fixture-only
    escape hatch (see FakeUtils below) so extract_order_id doesn't need to
    replicate schwab-py's real Location-header parsing."""

    def __init__(self, json_data, order_id=None):
        self._json = json_data
        self.order_id = order_id

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class FakeUtils:
    """Drop-in replacement for schwab.utils.Utils -- real extract_order_id()
    parses the Location header of a placement response; the fake response
    just carries the id directly."""

    def __init__(self, *a, **kw):
        pass

    def extract_order_id(self, response):
        return response.order_id


# Statuses that count as "resting" for schwab_safety._open_orders' filter
# (_OPEN_ORDER_STATUSES_EXCLUDED is the terminal set on the real module --
# mirrored here so seeded orders interact correctly with the real guards).
_TERMINAL_STATUSES = {'FILLED', 'CANCELED', 'REJECTED', 'REPLACED', 'EXPIRED'}


class FakeBroker:
    """One fake broker instance = one account's-eye view of the order book,
    shared across every account nickname for simplicity (real Schwab scopes
    orders per account_hash; tests that need genuine cross-account isolation
    should seed orders with different `account` values and filter by it --
    get_orders_for_account already does this correctly)."""

    def __init__(self):
        self.orders: dict[int, dict] = {}
        self.quotes: dict[str, dict] = {}
        self._id_counter = itertools.count(9_000_000_001)
        self.account_hashes = {}  # nickname -> fake hash
        self.cash_balances = {}   # nickname -> available cash (defaults to a large number)

    # ------------------------------------------------------------------
    # Test-side setup helpers
    # ------------------------------------------------------------------

    def set_cash_balance(self, account, cash):
        self.cash_balances[account] = cash

    def set_quote(self, ticker, last, bid=None, ask=None):
        self.quotes[ticker] = {
            'lastPrice': last,
            'bidPrice': bid if bid is not None else last,
            'askPrice': ask if ask is not None else last,
        }

    def seed_resting_order(self, account, ticker, order_type, side, quantity,
                            stop_price=None, trail_offset=None, status='WORKING'):
        """Directly inserts an order as if it were already resting at the
        broker before the test starts -- mirrors reconstructing real state
        (e.g. SH's real STOP order found mid-incident), not placing fresh."""
        order_id = next(self._id_counter)
        self.orders[order_id] = self._make_order(
            order_id, account, ticker, order_type, side, quantity,
            status=status, stop_price=stop_price, trail_offset=trail_offset,
        )
        return order_id

    def advance_price(self, ticker, last, bid=None, ask=None):
        """Updates the quote AND runs the broker's own trigger check against
        every resting STOP/TRAILING_STOP order for this ticker -- mirrors a
        real resting order firing on its own between polls, independent of
        whether our code is watching."""
        self.set_quote(ticker, last, bid, ask)
        real_bid = bid if bid is not None else last
        for o in self.orders.values():
            if o['status'] in _TERMINAL_STATUSES:
                continue
            symbol = o['orderLegCollection'][0]['instrument']['symbol']
            if symbol != ticker:
                continue
            if o['orderType'] == 'STOP' and o.get('stopPrice') is not None:
                if real_bid <= o['stopPrice']:
                    self._fill(o, real_bid)
            # TRAILING_STOP trigger simulation intentionally not modeled here
            # (would need running-peak tracking) -- tests needing that should
            # call force_fill() explicitly instead of relying on price-based
            # auto-trigger.

    def force_fill(self, order_id, price=None):
        o = self.orders[order_id]
        fill_price = price if price is not None else self.quotes.get(
            o['orderLegCollection'][0]['instrument']['symbol'], {}).get('lastPrice', 0.0)
        self._fill(o, fill_price)

    # ------------------------------------------------------------------
    # schwab-py client interface -- called by real schwab_client.py code
    # ------------------------------------------------------------------

    def set_timeout(self, secs):
        pass

    def get_account_numbers(self):
        return FakeResponse([
            {'accountNumber': h, 'hashValue': h} for h in self.account_hashes.values()
        ])

    def get_account(self, account_hash):
        cash = self.cash_balances.get(self._account_for_hash(account_hash), 1_000_000.0)
        return FakeResponse({'securitiesAccount': {'currentBalances': {'availableFunds': cash}}})

    def get_quote(self, ticker):
        q = self.quotes.get(ticker, {'lastPrice': 0.0, 'bidPrice': 0.0, 'askPrice': 0.0})
        return FakeResponse({ticker: {
            'quote': dict(q),
            'extended': {'lastPrice': q['lastPrice']},
        }})

    def get_orders_for_account(self, account_hash):
        account = self._account_for_hash(account_hash)
        return FakeResponse([o for o in self.orders.values() if o['account'] == account])

    def place_order(self, account_hash, order):
        account = self._account_for_hash(account_hash)
        order_id = next(self._id_counter)
        spec = self._parse_order_builder(order)
        self.orders[order_id] = self._make_order(order_id, account, **spec)
        self._maybe_immediate_fill(self.orders[order_id])
        return FakeResponse(None, order_id=order_id)

    def replace_order(self, account_hash, order_id, order):
        account = self._account_for_hash(account_hash)
        old = self.orders.get(order_id)
        if old is not None and old['status'] not in _TERMINAL_STATUSES:
            old['status'] = 'REPLACED'
        new_id = next(self._id_counter)
        spec = self._parse_order_builder(order)
        self.orders[new_id] = self._make_order(new_id, account, **spec)
        self._maybe_immediate_fill(self.orders[new_id])
        return FakeResponse(None, order_id=new_id)

    def cancel_order(self, account_hash, order_id):
        o = self.orders.get(order_id)
        if o is not None and o['status'] not in _TERMINAL_STATUSES:
            o['status'] = 'CANCELED'
        return FakeResponse(None)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _account_for_hash(self, account_hash):
        for nickname, h in self.account_hashes.items():
            if h == account_hash:
                return nickname
        return account_hash

    def _make_order(self, order_id, account, ticker=None, order_type='MARKET', side='BUY',
                     quantity=0, status=None, stop_price=None, trail_offset=None):
        if status is None:
            status = 'WORKING' if order_type in ('STOP', 'TRAILING_STOP') else 'FILLED'
        return {
            'orderId': order_id, 'account': account, 'orderType': order_type,
            'status': status, 'stopPrice': stop_price, 'stopPriceOffset': trail_offset,
            'enteredTime': datetime.now(timezone.utc).isoformat(),
            'orderLegCollection': [{
                'instruction': side, 'quantity': quantity,
                'instrument': {'symbol': ticker},
            }],
            'orderActivityCollection': [],
        }

    def _parse_order_builder(self, order):
        """Extracts the fields this fixture cares about from a real
        schwab.orders.generic.OrderBuilder instance (built by
        schwab_client._build_market_order/_build_trailing_order) via its
        public .build() dict -- avoids needing a parallel order-spec format."""
        spec = order.build()
        leg = spec['orderLegCollection'][0]
        return dict(
            ticker=leg['instrument']['symbol'],
            order_type=spec['orderType'],
            side=leg['instruction'],
            quantity=int(leg['quantity']),
            stop_price=spec.get('stopPrice'),
            trail_offset=spec.get('stopPriceOffset'),
        )

    def _maybe_immediate_fill(self, o):
        """A plain MARKET order fills immediately at the current quote --
        matches real same-tick market-order behavior. STOP/TRAILING_STOP
        orders stay WORKING until advance_price()/force_fill()."""
        if o['orderType'] == 'MARKET':
            symbol = o['orderLegCollection'][0]['instrument']['symbol']
            price = self.quotes.get(symbol, {}).get('lastPrice', 0.0)
            self._fill(o, price)

    def _fill(self, o, price):
        o['status'] = 'FILLED'
        qty = o['orderLegCollection'][0]['quantity']
        o['orderActivityCollection'] = [{
            'executionLegs': [{'price': price, 'quantity': qty}],
        }]


@pytest.fixture
def fake_broker(monkeypatch):
    broker = FakeBroker()
    broker.account_hashes = {
        'brokerage': 'HASH_BROKERAGE', 'sep': 'HASH_SEP', 'roth': 'HASH_ROTH',
        'ira': 'HASH_IRA', 'soxl_ira': 'HASH_SOXL_IRA',
    }
    monkeypatch.setattr(schwab_client, '_client', broker)
    monkeypatch.setattr(schwab_client, '_account_hashes', dict(broker.account_hashes))
    monkeypatch.setattr(schwab_client, '_get_client', lambda interactive=False: broker)
    monkeypatch.setattr(schwab_client, 'Utils', FakeUtils)
    return broker
