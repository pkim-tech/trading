"""Fake venue wiring: the FakeBroker shim, the fake accounts, the quote bridge.

FakeBroker itself is reused verbatim from tests/fake_broker.py (design
decision: one implementation, not a second copy that can drift). Only the
pytest `fake_broker` fixture is pytest-bound; the class isn't, so all this
needs is the non-pytest equivalent of that fixture's 4 monkeypatch calls
(docs/design.md 2026-08-16 second pass, item 7).

One shared FakeBroker instance serves both fake accounts -- matching reality
(Schwab is a single counterparty across a person's accounts) and already
safe, since FakeBroker's order book is account-keyed and
get_orders_for_account filters on it.
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))


def install_fake_broker(aliases):
    """Patches schwab_client's client boundary in-process, exactly as
    tests/fake_broker.py's pytest fixture does. Returns the FakeBroker.

    Presetting _account_hashes (rather than letting _resolve_account_hashes()
    run) is deliberate and is what makes fake aliases work at all: that
    function needs a SCHWAB_ACCOUNT_<ALIAS> env var per alias, and the harness
    blanks every one of those on purpose.

    THREAD SAFETY: FakeBroker's order/quote dicts carry no lock. Phase 1 drives
    every leg from a single thread (the emitter calls
    _handle_activity_message synchronously rather than from a stream callback
    thread), so this is moot today -- but a persistent/soak mode, where the
    poll loop and the stream thread run concurrently against one broker, needs
    this resolved first (docs/design.md 2026-08-16, item 10).
    """
    import schwab_client
    from fake_broker import FakeBroker, FakeUtils

    broker = FakeBroker()
    broker.account_hashes = {alias: f"HASH_{alias.upper()}" for alias in aliases}
    schwab_client._client = broker
    schwab_client._account_hashes = dict(broker.account_hashes)
    schwab_client._get_client = lambda interactive=False: broker
    schwab_client.Utils = FakeUtils
    return broker


def seed_account_number_env(alias_account_numbers):
    """Sets SCHWAB_ACCOUNT_<ALIAS>=<fake account number> for each alias, so
    schwab_client.resolve_account_alias_from_number() -- the real suffix-match
    helper drain_fill_queue calls to turn a raw stream AccountNumber into an
    alias (2026-08-16 AccountNumber-defect fix) -- has something real to match
    against. Distinct from install_fake_broker's _account_hashes presetting
    above: that bypasses SCHWAB_ACCOUNT_<ALIAS> entirely for order-placement
    hash lookups (the harness blanks every one of those on purpose, see
    isolation.configure_env), but this one specific code path genuinely reads
    the env var, so it needs a real value to exercise faithfully. Uses the
    full fake account number (not a short suffix) -- collision-proof, and
    this harness's own accounts are the only ones with a value set at all
    (every real-account SCHWAB_ACCOUNT_* var is blanked)."""
    for alias, number in alias_account_numbers.items():
        os.environ[f"SCHWAB_ACCOUNT_{alias.upper()}"] = str(number)


def seed_fake_accounts(accounts):
    """Inserts the harness's fake `accounts` rows into the (isolated) DB and
    forces schwab_safety.ACCOUNTS to reload, so real check_order/
    approve_and_record run against real AccountLimits objects built from real
    DB rows -- no fake/real branching anywhere inside schwab_safety.

    accounts: list of dicts with alias/notional_cap/daily_order_cap/
    cash_settlement_type/margin_capable keys.
    """
    import schwab_safety
    import signals_db

    with signals_db._conn() as c:
        for a in accounts:
            c.execute(
                "INSERT OR REPLACE INTO accounts (alias, schwab_name, enabled, notional_cap, "
                "daily_order_cap, trading_enabled, cash_settlement_type, margin_capable, "
                "margin_floor, is_tax_advantaged) VALUES (?, ?, 1, ?, ?, 1, ?, ?, 0.0, 0)",
                (a['alias'], f"FAKE-{a['alias']}", a['notional_cap'], a['daily_order_cap'],
                 a['cash_settlement_type'], int(a.get('margin_capable', 0))),
            )
        c.commit()
    schwab_safety.reload_accounts()


def age_recent_order_records(seconds):
    """Rewinds every timestamp in schwab_safety's local recent-order dedup
    record (STATE_PATH's 'recent_orders'/'recent_order_timestamps') by
    `seconds` -- lets a scenario represent real elapsed wall-clock time
    between two order-placement stages without an actual time.sleep().

    Real production spacing: entry-time SL placement and any later SELL-side
    replace (arm, or an SL/TIME/TP exit) are always >= one full POLL_SECS
    (300s, signals_config.py) apart -- open_positions is snapshotted once per
    poll iteration, and the exit/arm scan reads that PRE-fill snapshot, so a
    position opened by check_auto_fills earlier in the SAME iteration can't
    also be arm/exit-scanned until the NEXT iteration at the earliest. A
    fake_venue scenario driving both stages back-to-back in one process (for
    determinism/speed, not simulated realism) can otherwise trip
    schwab_safety's DUPLICATE_ORDER_WINDOW_SECS=60s dedup guard purely as a
    harness-compression artifact -- same class of environmental gap as
    seed_quote's real-price bridge or the _is_trading_day fake, not a bypass
    of the guard's own logic (it still runs for real; it just correctly sees
    the earlier order as no longer 'recent' once its timestamp is aged past
    the window, exactly as it would after a real 300s poll gap)."""
    import json

    import schwab_safety

    if not schwab_safety.STATE_PATH.exists():
        return
    counts = json.loads(schwab_safety.STATE_PATH.read_text() or "{}")
    for key in ("recent_order_timestamps",):
        if key in counts:
            counts[key] = [t - seconds for t in counts[key]]
    for o in counts.get("recent_orders", []):
        if "ts" in o:
            o["ts"] -= seconds
    schwab_safety.STATE_PATH.write_text(json.dumps(counts))


class QuoteBridgeError(RuntimeError):
    """Raised when no real price could be fetched -- never fall back to 0.0."""


def live_price(ticker):
    """Real market data (yfinance fast_info, the same source paper_trading.py
    uses for live prices) -- Phase 1 runs against the real feed; historical
    replay is Phase 2.

    Fails loud rather than returning 0.0: an unseeded FakeBroker quote returns
    0.0 today, which would silently pass every notional cap and corrupt all
    sizing math instead of erroring (docs/design.md 2026-08-16, item 4).
    """
    import yfinance as yf

    try:
        price = float(yf.Ticker(ticker).fast_info['last_price'])
    except Exception as e:
        raise QuoteBridgeError(f"no live price for {ticker}: {e}") from e
    if not price > 0:
        raise QuoteBridgeError(f"live price for {ticker} came back as {price!r}")
    return price


def seed_quote(broker, ticker, price=None, price_source_ticker=None):
    """Bridges a real (or explicitly supplied) price into the fake broker's own
    quote state. `price` is an override for deterministic/offline runs (the
    pytest harness test uses it); the default path hits the real feed.

    Seeded ONCE, never refreshed -- Phase 1's scenario is a fixed-price
    sequence. A soak/replay mode (Phase 2) needs a real repeating feed here,
    and TRAILING_STOP trigger simulation inside FakeBroker would consume it."""
    if price is None:
        # The quote is real market data for a real symbol, seeded onto the
        # harness's synthetic ticker -- see scenarios_meta.PRICE_SOURCE_TICKER.
        price = live_price(price_source_ticker or ticker)
    price = float(price)
    if not price > 0:
        raise QuoteBridgeError(f"refusing to seed a non-positive quote for {ticker}: {price!r}")
    broker.set_quote(ticker, last=price, bid=round(price - 0.01, 4), ask=round(price + 0.01, 4))
    return price
