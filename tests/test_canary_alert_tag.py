"""Tests for the 2026-08-09 fix: the real per-signal BUY/SELL Slack alerts
(signals_blocks._build_buy_blocks/_build_sell_blocks) never carried a
🧪CANARY marker, unlike the Reference Report and paper-trading's console
tag (found 2026-07-24). Currently unreachable in practice -- canary nodes
never cross has_capital_at_stake, so should_alert_live suppresses the real
Slack post entirely -- but the block-building functions themselves are
still directly testable and should tag correctly regardless of whether the
post fires."""
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

TICKER = 'TEST_CANARY_TAG'


@pytest.fixture
def isolated_db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    db.ensure_tables()
    yield db
    os.unlink(tmp_db.name)


def _fake_sig(price=85.0):
    return {
        'ticker': TICKER, 'current_price': price, 'z_score': -2.1,
        'last_bar': datetime(2026, 8, 9, 14, 30), 'lower_band': price - 0.5,
        'hurst': None, 'adf_p': None,
    }


def _add_node(version, account='ira', state='live'):
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', version, window=5, take_profit=0.1,
                stop_loss=1.0, max_hold_hours=48, state=state, account=account)
    with db._conn() as c:
        row = c.execute("SELECT * FROM watch_list WHERE ticker=? AND version=?", (TICKER, version)).fetchone()
    node = dict(row)
    node['id'] = None  # skip the avg_vol_10d cache-write branch, not under test here
    return node


def test_build_buy_blocks_tags_canary_node(isolated_db, monkeypatch):
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', Path('/nonexistent/trading_universe.db'))
    node = _add_node('canary')
    blocks = signals_blocks._build_buy_blocks(node, _fake_sig())
    assert '🧪CANARY' in blocks[0]['text']['text']


def test_build_buy_blocks_no_canary_tag_for_real_node(isolated_db, monkeypatch):
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', Path('/nonexistent/trading_universe.db'))
    node = _add_node('v5')
    blocks = signals_blocks._build_buy_blocks(node, _fake_sig())
    assert '🧪CANARY' not in blocks[0]['text']['text']


def test_build_buy_blocks_tags_canary_family_variant(isolated_db, monkeypatch):
    """2026-08-09 paired review finding: exact version=='canary' equality
    missed real canary-family variant nodes (e.g. 'v5-canary-drought-addon')
    -- these are the ONLY canary-family nodes reachable via a real Slack post
    today (via notify_drought_buy_signal's separate missing-gate bug, fixed
    same session), so the exact-match gap wasn't just theoretical."""
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', Path('/nonexistent/trading_universe.db'))
    node = _add_node('v5-canary-drought-addon')
    blocks = signals_blocks._build_buy_blocks(node, _fake_sig())
    assert '🧪CANARY' in blocks[0]['text']['text']


def _fake_pos(wl_id, account='ira'):
    return {
        'id': 1, 'ticker': TICKER, 'entry_price': 80.0, 'account': account, 'wl_id': wl_id,
    }


def test_build_sell_blocks_tags_canary_node(isolated_db):
    node = _add_node('canary')
    with db._conn() as c:
        wl_id = c.execute("SELECT id FROM watch_list WHERE ticker=? AND version='canary'", (TICKER,)).fetchone()[0]
    blocks = signals_blocks._build_sell_blocks(_fake_pos(wl_id), 'TP', 85.0, 84.0)
    assert '🧪CANARY' in blocks[0]['text']['text']


def test_build_sell_blocks_no_canary_tag_for_real_node(isolated_db):
    node = _add_node('v5')
    with db._conn() as c:
        wl_id = c.execute("SELECT id FROM watch_list WHERE ticker=? AND version='v5'", (TICKER,)).fetchone()[0]
    blocks = signals_blocks._build_sell_blocks(_fake_pos(wl_id), 'TP', 85.0, 84.0)
    assert '🧪CANARY' not in blocks[0]['text']['text']


def test_build_sell_blocks_no_canary_tag_when_node_unresolvable(isolated_db):
    """wl_id pointing at a since-deleted/nonexistent node -- must not crash
    on (_node or {}).get('version') and must not fabricate a canary tag."""
    blocks = signals_blocks._build_sell_blocks(_fake_pos(999999), 'TP', 85.0, 84.0)
    assert '🧪CANARY' not in blocks[0]['text']['text']
