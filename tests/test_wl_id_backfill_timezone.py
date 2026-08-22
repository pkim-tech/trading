"""Regression test for signals_db.py's wl_id-backfill migration (inside
ensure_tables(), ~line 1985) UTC/ET fix (2026-08-21) -- the "trade predates
its candidate node's creation" filter compared watch_list.added_at (UTC,
SQLite's datetime('now') default) directly against trade_log.entry_time
(ET-native, written by application code) with no conversion. Since UTC reads
~4-5h ahead of ET, this made the filter TOO STRICT: a genuinely valid
candidate (real ET creation time actually before the trade) could get
wrongly excluded because its raw UTC-stamped added_at string compared as
later than the ET entry_time -- leaving a legacy row's wl_id incorrectly
stuck at NULL. Usually under-assigns (safe) rather than mis-assigns, but not
strictly guaranteed to be fail-safe: this filter sits ahead of the
`len(candidates) == 1` assignment, so the old needlessly strict version
could in principle have collapsed a genuine multi-candidate tie down to one
arbitrary survivor purely because of the timezone artifact (found by
independent-cold review of the fix, not observed in real production data).
The fix's wider candidate set can only reduce that risk, not introduce a new
instance of it. Still a real gap in a migration this project relies on to
backfill legacy trade_log/paper_trade_log rows. See
docs/deep_backlog.md's 2026-08-21 entries for the full incident/fix
writeup -- this is the item that made the earlier entries' new
comments/analysis identifiable in the first place."""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db

# Same pattern as the sibling UTC/ET fix tests -- pinned explicitly so this file
# passes deterministically on any host regardless of its ambient TZ, rather than
# depending on the dev/CI host happening to be configured ET the way the real
# daemon host is.
os.environ['TZ'] = 'America/New_York'
time.tzset()

TICKER = 'TEST_WL_ID_BACKFILL'
_TMP_DIR = '/dev/shm' if os.path.isdir('/dev/shm') else None


@pytest.fixture(scope='session')
def _schema_template_db():
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False, dir=_TMP_DIR)
    tmp_db.close()
    orig_db_path = signals_config.DB_PATH
    signals_config.DB_PATH = Path(tmp_db.name)
    try:
        db.ensure_tables()
    finally:
        signals_config.DB_PATH = orig_db_path
    yield tmp_db.name
    os.unlink(tmp_db.name)


@pytest.fixture
def isolated_db(monkeypatch, _schema_template_db):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False, dir=_TMP_DIR)
    tmp_db.close()
    shutil.copy2(_schema_template_db, tmp_db.name)
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    yield db
    os.unlink(tmp_db.name)


def _make_node_with_added_at(isolated_db, added_at_utc):
    """Creates a real node via add_node, then overwrites added_at directly to
    a controlled UTC timestamp string -- add_node itself only ever writes the
    SQL default (now), so this is the only way to pin a specific creation
    instant."""
    isolated_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'canary', window=5,
                          take_profit=0.1, stop_loss=0, max_hold_hours=48,
                          fixed_sl_override=15)
    node = [x for x in isolated_db.get_watchlist() if x['ticker'] == TICKER][0]
    with isolated_db._conn() as c:
        c.execute("UPDATE watch_list SET added_at = ? WHERE id = ?", (added_at_utc, node['id']))
        c.commit()
    return node['id']


def _make_legacy_trade_row(isolated_db, node, entry_time):
    """Logs a real closed trade against the node, then nulls its wl_id back
    out directly -- simulating the exact legacy-row shape this migration
    exists to backfill (a trade_log row predating log_trade_entry's own
    wl_id write, per this migration's own comment)."""
    isolated_db.open_position(node, signal_price=100.0, signal_time=entry_time,
                               entry_price=101.0, entry_time=entry_time, shares=10)
    pos = isolated_db.get_open_position(TICKER)
    isolated_db.close_position(pos['id'], exit_signal_price=105.0, exit_price=105.0,
                                exit_time=entry_time, exit_reason='WIN')
    with isolated_db._conn() as c:
        row = c.execute(
            "SELECT id FROM trade_log WHERE ticker=? ORDER BY id DESC LIMIT 1", (TICKER,)
        ).fetchone()
        c.execute("UPDATE trade_log SET wl_id = NULL WHERE id = ?", (row['id'],))
        c.commit()
        return row['id']


def test_candidate_predating_trade_in_et_is_backfilled_despite_later_raw_utc_added_at(isolated_db):
    """Node's real ET creation is 08:00 ET, 2026-08-20 -- 2 hours BEFORE the
    trade's entry_time (10:00 ET, same day) -- a genuinely valid candidate.
    Stored (UTC) added_at is 12:00:00 (EDT, +4h), which compares as LATER
    than entry_time as a raw string -- exactly the shape the old unconverted
    filter got wrong. The fixed migration must still backfill wl_id."""
    entry_time = '2026-08-20 10:00:00'
    node_id = _make_node_with_added_at(isolated_db, '2026-08-20 12:00:00')  # 08:00 ET real creation
    node = [x for x in isolated_db.get_watchlist() if x['ticker'] == TICKER][0]
    trade_id = _make_legacy_trade_row(isolated_db, node, entry_time)

    isolated_db.ensure_tables()

    with isolated_db._conn() as c:
        row = c.execute("SELECT wl_id FROM trade_log WHERE id=?", (trade_id,)).fetchone()
    assert row['wl_id'] == node_id, (
        "a candidate whose real ET creation genuinely predates the trade should be "
        "backfilled, even though its raw UTC-stamped added_at string reads later "
        "than the ET entry_time"
    )


def test_candidate_genuinely_after_trade_in_et_is_not_backfilled(isolated_db):
    """Control case: node's real ET creation is 14:00 ET, 2026-08-20 -- AFTER
    the trade's entry_time (10:00 ET) -- genuinely invalid, must stay
    excluded (wl_id left NULL, this migration's existing "can't determine
    it" convention) under both old and new logic."""
    entry_time = '2026-08-20 10:00:00'
    node_id = _make_node_with_added_at(isolated_db, '2026-08-20 18:00:00')  # 14:00 ET real creation
    node = [x for x in isolated_db.get_watchlist() if x['ticker'] == TICKER][0]
    trade_id = _make_legacy_trade_row(isolated_db, node, entry_time)

    isolated_db.ensure_tables()

    with isolated_db._conn() as c:
        row = c.execute("SELECT wl_id FROM trade_log WHERE id=?", (trade_id,)).fetchone()
    assert row['wl_id'] is None
