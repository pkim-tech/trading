"""Regression guard for the recurring duplication bug between
signals_db._PENDING_BUY_NODE_KEYS (the pending_buys.node_json snapshot) and
signals_blocks._build_buy_blocks's Slack BUY-button `value` JSON node
snapshot. Both used to be hand-maintained, independent field lists over the
same watch_list columns -- both independently went stale missing
starting_notional_override during the 2026-08-18 RETL incident review (see
tests/test_pending_buy_snapshot_starting_notional_override.py for the
signals_db side), because a field added to one list had no mechanism forcing
it into the other.

Fixed 2026-08-19 by making signals_blocks.py import and use
db._PENDING_BUY_NODE_KEYS directly instead of maintaining its own tuple. This
test exercises the real _build_buy_blocks() call path (not just a source
inspection) so it fails loudly if that import/usage is ever reverted to a
separate hand-typed list, or if the Slack button's node snapshot ever drops a
field _PENDING_BUY_NODE_KEYS carries."""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db
import signals_blocks

TICKER = 'TEST_NODE_KEYS_CONSISTENCY'


@pytest.fixture
def isolated_db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', Path('/nonexistent/trading_universe.db'))
    db.ensure_tables()
    yield db
    os.unlink(tmp_db.name)


def _fake_sig(price=85.0):
    return {
        'ticker': TICKER, 'current_price': price, 'z_score': -2.1,
        'last_bar': datetime(2026, 8, 19, 14, 30), 'lower_band': price - 0.5,
        'hurst': None, 'adf_p': None,
    }


def test_slack_buy_button_node_snapshot_matches_pending_buy_node_keys(isolated_db, monkeypatch):
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5', window=5, take_profit=0.1,
                stop_loss=1.0, max_hold_hours=48, state='live', account='ira')
    db.set_starting_notional_override(_node_id(), 400.0)
    with db._conn() as c:
        c.execute("UPDATE watch_list SET drought_sl_pct_override=?, drought_arm_pct_override=?, "
                  "drought_trail_pct_override=? WHERE id=?",
                  (2.5, 12.0, 1.5, _node_id()))
        c.commit()
    node = _node()

    monkeypatch.setattr(signals_config, 'INTERACTIVE', True)
    blocks = signals_blocks._build_buy_blocks(node, _fake_sig())

    actions_block = next(b for b in blocks if b.get('type') == 'actions')
    value = json.loads(actions_block['elements'][0]['value'])
    snapshot_keys = set(value['node'].keys())

    assert snapshot_keys == set(db._PENDING_BUY_NODE_KEYS), (
        "signals_blocks._build_buy_blocks's Slack BUY-button node snapshot has "
        "drifted from signals_db._PENDING_BUY_NODE_KEYS -- this is the exact "
        "duplication-bug shape (2x independently fixed, 2026-08-18) this test "
        f"exists to catch. Missing from button: "
        f"{set(db._PENDING_BUY_NODE_KEYS) - snapshot_keys}; extra in button: "
        f"{snapshot_keys - set(db._PENDING_BUY_NODE_KEYS)}"
    )

    # The concrete failure mode from the RETL incident: an override actually
    # set on the node must survive into the button's embedded snapshot, not
    # just be present-as-a-key-with-None. Checked for starting_notional_
    # override (the RETL field) AND the 3 drought override fields newly
    # carried by this unification, so a `{k: None for k in ...}` regression
    # in the snapshot-building code can't pass silently.
    assert value['node']['starting_notional_override'] == 400.0
    assert value['node']['drought_sl_pct_override'] == 2.5
    assert value['node']['drought_arm_pct_override'] == 12.0
    assert value['node']['drought_trail_pct_override'] == 1.5


def _node_id():
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]['id']


def _node():
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]
