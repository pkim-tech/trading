"""slack_message_log's blocks_json rolling-window trim (db.trim_old_slack_blocks).

Guards the storage-cost fix: blocks_json is 10-50x `text`, and live_backups/
keeps 720 hourly DB snapshots, so the column has to be bounded -- but the row
itself (text/error/ts/mode) must survive indefinitely, since
scripts/recent_slack_messages.py's history is text-based.
"""
import pytest

import signals_config
import signals_db


@pytest.fixture
def bare_db(monkeypatch, tmp_path):
    """Temp DB at whatever schema ensure_tables() currently produces -- no
    blocks_json guaranteed either way."""
    monkeypatch.setattr(signals_config, 'DB_PATH', tmp_path / 'trim_test.db')
    signals_db.ensure_tables()
    return signals_db


@pytest.fixture
def db(bare_db):
    with signals_db._conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(slack_message_log)").fetchall()}
        if 'blocks_json' not in cols:
            # Conditional so this test is valid both before and after the
            # ensure_tables() migration that adds the column lands.
            c.execute("ALTER TABLE slack_message_log ADD COLUMN blocks_json TEXT")
            c.commit()
    return signals_db


def _seed(rows):
    with signals_db._conn() as c:
        for ts_expr, text, blocks in rows:
            c.execute(
                "INSERT INTO slack_message_log (ts, mode, text, blocks_json) "
                f"VALUES (datetime('now', '{ts_expr}'), 'sim', ?, ?)",
                (text, blocks),
            )
        c.commit()


def _fetch():
    with signals_db._conn() as c:
        return {r['text']: dict(r) for r in c.execute("SELECT * FROM slack_message_log")}


def test_trim_nulls_only_old_blocks_and_never_touches_text(db):
    _seed([
        ('-30 days', 'ancient', '[{"type":"section"}]'),
        ('-8 days',  'just_outside', '[{"type":"section"}]'),
        ('-6 days',  'just_inside', '[{"type":"section"}]'),
        ('-1 hours', 'fresh', '[{"type":"section"}]'),
        ('-30 days', 'ancient_already_null', None),
    ])

    assert db.trim_old_slack_blocks(days=7) == 2

    rows = _fetch()
    assert len(rows) == 5, "trim must null the column, never delete the row"
    assert rows['ancient']['blocks_json'] is None
    assert rows['just_outside']['blocks_json'] is None
    assert rows['just_inside']['blocks_json'] is not None
    assert rows['fresh']['blocks_json'] is not None
    for name, r in rows.items():
        assert r['text'] == name
        assert r['mode'] == 'sim'
        assert r['ts']


def test_trim_is_idempotent(db):
    _seed([
        ('-30 days', 'ancient', '[{"type":"section"}]'),
        ('-1 hours', 'fresh', '[{"type":"section"}]'),
    ])
    assert db.trim_old_slack_blocks(days=7) == 1
    # Already-nulled rows are skipped, not rewritten -- a same-day rerun (e.g.
    # a daemon restart past the 16:05 EOD slot) is a clean no-op, not an error.
    assert db.trim_old_slack_blocks(days=7) == 0
    assert db.trim_old_slack_blocks(days=7) == 0
    assert _fetch()['fresh']['blocks_json'] is not None


def test_trim_on_empty_table_is_a_noop(db):
    assert db.trim_old_slack_blocks(days=7) == 0


def test_window_size_is_honored(db):
    _seed([('-10 days', 'ten_days_old', '[{"type":"section"}]')])
    assert db.trim_old_slack_blocks(days=30) == 0
    assert db.trim_old_slack_blocks(days=7) == 1


def test_boundary_rows_land_on_the_correct_side(db):
    _seed([
        # 7 days == 10080 minutes; one minute either side of the exact cutoff.
        ('-10081 minutes', 'just_past_cutoff', '[{"type":"section"}]'),
        ('-10079 minutes', 'just_inside_cutoff', '[{"type":"section"}]'),
    ])
    assert db.trim_old_slack_blocks(days=7) == 1
    rows = _fetch()
    assert rows['just_past_cutoff']['blocks_json'] is None
    assert rows['just_inside_cutoff']['blocks_json'] is not None


def test_missing_column_is_a_clean_noop(bare_db, monkeypatch):
    """The guard exists because blocks_json's own migration may not be present
    yet -- on a pre-migration schema the trim must return 0, not raise."""
    with signals_db._conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(slack_message_log)").fetchall()}
        if 'blocks_json' in cols:
            c.execute("DROP TABLE slack_message_log")
            c.execute("""
                CREATE TABLE slack_message_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL DEFAULT (datetime('now')),
                    mode TEXT NOT NULL,
                    text TEXT NOT NULL,
                    error TEXT
                )
            """)
            c.commit()
    with signals_db._conn() as c:
        c.execute("INSERT INTO slack_message_log (ts, mode, text) "
                  "VALUES (datetime('now', '-30 days'), 'sim', 'ancient')")
        c.commit()

    assert bare_db.trim_old_slack_blocks(days=7) == 0
    assert _fetch()['ancient']['text'] == 'ancient'
