"""Pins slack_message_log's blocks_json column (2026-08-14).

`text` is only Slack's fallback/notification string -- for any Block Kit
message (the multi-ticker signal-window digest, the morning report, every
button-bearing alert) it carries almost none of what a human actually saw,
so a real posted message was unreconstructible after the fact. These tests
call the REAL, un-mocked _post_message (captured at collection time, before
conftest.py's autouse _no_real_slack_posts fixture replaces it), with
SOCKET_MODE/SLACK_HOOK forced off so no network call happens -- same
pattern and rationale as tests/test_post_message_alert_gate.py.
"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_blocks
import signals_config
import signals_db

_REAL_POST_MESSAGE = signals_blocks._post_message

TICKER = 'TEST_BLOCKS_LOG'


@pytest.fixture
def env(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'SOCKET_MODE', False)
    monkeypatch.setattr(signals_config, 'SLACK_HOOK', '')
    monkeypatch.setattr(signals_config, 'SIM_MODE', False)
    signals_db.ensure_tables()
    yield
    Path(tmp_db.name).unlink(missing_ok=True)


def _last():
    msgs = signals_db.get_slack_messages(limit=1)
    return msgs[0] if msgs else None


def test_blocks_are_captured_alongside_text(env):
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Signal window — 10:25 ET"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*SOXL* z=-2.14 → $18.22"}},
    ]
    _REAL_POST_MESSAGE("Signal window — 10:25 ET | HIGH ALERT", blocks=blocks)
    row = _last()
    assert row['text'] == "Signal window — 10:25 ET | HIGH ALERT"
    assert json.loads(row['blocks_json']) == blocks
    # The real point of the column: per-ticker content lives only in blocks.
    assert 'SOXL' not in row['text']
    assert 'SOXL' in row['blocks_json']


def test_plain_text_message_stores_null_blocks(env):
    _REAL_POST_MESSAGE("plain text, no blocks")
    row = _last()
    assert row['text'] == "plain text, no blocks"
    assert row['blocks_json'] is None


def test_exotic_member_degrades_via_default_str(env):
    """default=str keeps an unexpected object from raising -- the row still
    logs, with the member stringified rather than dropped."""
    blocks = [{"type": "section", "obj": object()}]
    _REAL_POST_MESSAGE("has an exotic member", blocks=blocks)
    row = _last()
    assert row['text'] == "has an exotic member"
    assert json.loads(row['blocks_json'])[0]['type'] == 'section'


def test_circular_blocks_hit_the_fallback_and_stay_valid_json(env):
    """The one case default=str genuinely can't handle. The fallback must
    still be parseable JSON (not an f-string that a quote in str(e) could
    corrupt) and must not cost the row its text."""
    circular = []
    circular.append(circular)
    _REAL_POST_MESSAGE("has circular blocks", blocks=circular)
    row = _last()
    assert row['text'] == "has circular blocks"
    parsed = json.loads(row['blocks_json'])  # must not raise
    assert 'unserializable blocks' in parsed[0]


def test_migration_adds_column_to_preexisting_table(env):
    """ensure_tables() must ALTER an existing slack_message_log that predates
    the column, not just create a fresh one with it -- the real live DB is an
    already-existing file."""
    with signals_db._conn() as c:
        c.execute("DROP TABLE slack_message_log")
        c.execute("""
            CREATE TABLE slack_message_log (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                ts    TEXT NOT NULL DEFAULT (datetime('now')),
                mode  TEXT NOT NULL,
                text  TEXT NOT NULL,
                error TEXT
            )
        """)
        c.execute("INSERT INTO slack_message_log (mode, text) VALUES ('live', 'legacy row')")
        c.commit()
    signals_db.ensure_tables()
    with signals_db._conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(slack_message_log)").fetchall()}
    assert 'blocks_json' in cols
    # An in-place ALTER must preserve existing rows -- the one thing this
    # could plausibly get wrong versus a rebuild-table migration.
    with signals_db._conn() as c:
        legacy = c.execute("SELECT text, blocks_json FROM slack_message_log").fetchall()
    assert [(r['text'], r['blocks_json']) for r in legacy] == [('legacy row', None)]
    # Idempotent: a second run must not raise (duplicate column error).
    signals_db.ensure_tables()
    signals_db.log_slack_message('live', 'after migration', blocks=[{"type": "divider"}])
    row = _last()
    assert json.loads(row['blocks_json']) == [{"type": "divider"}]
