"""Real parametrized truth table for signals_notify.check_entry_abandon's
(account, order_placed, order_id) state space -- 12 cells, one @given/
parametrize test asserting the correct outcome per cell in a single place.

Distinct from tests/test_entry_abandon.py's scenario tests: those cover most
of the same cells but as separately hand-named tests, not a systematic
enumeration -- built 2026-07-31 after the user asked for this specific
artifact (a comprehensive table, not scattered examples) as a more
deterministic alternative to relying on independent code review.

account_kind: 'unrecognized' (limits is None), 'dry_run' (real dry_run=True
account, 'roth'), 'real' (dry_run=False account, 'soxl_ira')."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db
import signals_notify
import schwab_client
import schwab_safety
from tests.conftest import make_synthetic_csv, cleanup_csv, _synthetic_timestamps

TICKER = 'TEST_ABANDON_TRUTH_TABLE'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    posted = []
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda *a, **kw: (posted.append(a[0] if a else kw.get('text')), (None, None))[1])
    signals_notify._ENTRY_ABANDON_ALERTED.clear()

    db.ensure_tables()
    make_synthetic_csv(TICKER, last_close=100.0)
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                stop_loss=1, max_hold_hours=7, state='live',
                trail_buy_pct=1.0, trail_pct=1.0, starting_notional=5000, fixed_sl_override=1.0)

    yield posted

    cleanup_csv(TICKER)
    tmp_db_path = Path(tmp_db.name)
    if tmp_db_path.exists():
        tmp_db_path.unlink()


def _node():
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]


def _sig(price, hours_ago):
    timestamps = _synthetic_timestamps()
    last_bar = timestamps[-1 - hours_ago] if hours_ago < len(timestamps) else timestamps[0]
    return {'ticker': TICKER, 'current_price': price, 'z_score': -2.5, 'last_bar': last_bar}


_ACCOUNT_FOR_KIND = {
    'unrecognized': 'totally_unknown_xyz',
    'dry_run': 'roth',        # real ACCOUNTS['roth'].dry_run == True
    'real': 'soxl_ira',      # real ACCOUNTS['soxl_ira'].dry_run == False
}

# One row per real (account_kind, order_placed, order_id_present) cell --
# expected: ('cleared' | 'kept', real cancel_order attempted or not).
TRUTH_TABLE = [
    # account_kind,   order_placed, order_id_present -> expect_cleared, expect_cancel_call
    ('unrecognized',  False,        False,              False, False),
    ('unrecognized',  False,        True,               False, False),
    ('unrecognized',  True,         False,              False, False),
    ('unrecognized',  True,         True,               False, False),
    ('dry_run',       False,        False,              True,  False),
    ('dry_run',       False,        True,               True,  False),
    ('dry_run',       True,         False,              True,  False),
    ('dry_run',       True,         True,               True,  False),
    ('real',          False,        False,              True,  False),
    ('real',          False,        True,               True,  True),
    ('real',          True,         False,              False, False),  # HIGH real-money bug's cell
    ('real',          True,         True,               True,  True),
]


@pytest.mark.parametrize(
    "account_kind,order_placed,order_id_present,expect_cleared,expect_cancel_call",
    TRUTH_TABLE,
    ids=[f"{a}-placed={p}-order_id={o}" for a, p, o, _, _ in TRUTH_TABLE],
)
def test_entry_abandon_truth_table(env, monkeypatch, account_kind, order_placed,
                                    order_id_present, expect_cleared, expect_cancel_call):
    posted = env
    account = _ACCOUNT_FOR_KIND[account_kind]
    with db._conn() as c:
        c.execute("UPDATE watch_list SET account = ? WHERE ticker = ?", (account, TICKER))
        c.commit()
    node = _node()

    order_id = 555 if order_id_present else None
    db.add_pending_buy(node, _sig(100.0, hours_ago=10), channel=None, ts=None, order_id=order_id)
    if order_placed:
        db.mark_pending_buy_placed_by_wl_id(node['id'])

    calls = []
    monkeypatch.setattr(
        schwab_client, 'cancel_order',
        lambda acct, ticker, oid: (calls.append((acct, ticker, oid)), (None, 'CANCELED'))[1],
    )

    signals_notify.check_entry_abandon()

    remaining = db.get_pending_buys()
    if expect_cleared:
        assert remaining == [], (
            f"[{account_kind}, placed={order_placed}, order_id={order_id_present}] "
            f"expected the row cleared, still present: {remaining}"
        )
    else:
        assert len(remaining) == 1, (
            f"[{account_kind}, placed={order_placed}, order_id={order_id_present}] "
            f"expected the row kept (fail closed), got: {remaining}"
        )
        assert posted, "expected an alert when the row is kept"

    assert bool(calls) == expect_cancel_call, (
        f"[{account_kind}, placed={order_placed}, order_id={order_id_present}] "
        f"expected cancel_order called={expect_cancel_call}, got calls={calls}"
    )
