"""Tests for db_cache.sweep_tranches -- the DB-backed replacement for
scripts/liquidity_tranches.txt's hand-edited ticker list (2026-08-12).
Pins: add/remove are idempotent upserts, removal requires and preserves a
reason, active_only filtering, and the render round-trip stays lossless."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import db_cache

CAMPAIGN = 'test_campaign'


@pytest.fixture
def isolated_db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(db_cache, 'DB_PATH', tmp_db.name)
    yield db_cache
    os.unlink(tmp_db.name)


def test_add_tranche_ticker_is_idempotent(isolated_db):
    db_cache.add_tranche_ticker(CAMPAIGN, 1, 'AAA')
    db_cache.add_tranche_ticker(CAMPAIGN, 1, 'AAA')
    tranches = db_cache.get_tranches(CAMPAIGN)
    assert tranches[1] == ['AAA']


def test_get_tranches_groups_by_tranche_num_in_added_order(isolated_db):
    db_cache.add_tranche_ticker(CAMPAIGN, 2, 'BBB')
    db_cache.add_tranche_ticker(CAMPAIGN, 1, 'AAA')
    db_cache.add_tranche_ticker(CAMPAIGN, 2, 'CCC')
    tranches = db_cache.get_tranches(CAMPAIGN)
    assert tranches[1] == ['AAA']
    assert tranches[2] == ['BBB', 'CCC']


def test_remove_tranche_ticker_excluded_from_active_only(isolated_db):
    db_cache.add_tranche_ticker(CAMPAIGN, 1, 'AAA')
    db_cache.add_tranche_ticker(CAMPAIGN, 1, 'BBB')
    db_cache.remove_tranche_ticker(CAMPAIGN, 1, 'BBB', reason='disqualified: test reason')

    active = db_cache.get_tranches(CAMPAIGN, active_only=True)
    assert active[1] == ['AAA']

    everything = db_cache.get_tranches(CAMPAIGN, active_only=False)
    assert set(everything[1]) == {'AAA', 'BBB'}


def test_remove_tranche_ticker_preserves_reason_in_audit(isolated_db):
    db_cache.add_tranche_ticker(CAMPAIGN, 1, 'AAA')
    db_cache.remove_tranche_ticker(CAMPAIGN, 1, 'AAA', reason='concentration -- 4 holdings')

    audit = db_cache.get_tranche_audit(CAMPAIGN)
    row = [r for r in audit if r['ticker'] == 'AAA'][0]
    assert row['active'] == 0
    assert row['reason'] == 'concentration -- 4 holdings'
    assert row['removed_at'] is not None


def test_re_adding_a_removed_ticker_reactivates_it(isolated_db):
    db_cache.add_tranche_ticker(CAMPAIGN, 1, 'AAA')
    db_cache.remove_tranche_ticker(CAMPAIGN, 1, 'AAA', reason='temporarily disqualified')
    db_cache.add_tranche_ticker(CAMPAIGN, 1, 'AAA', reason='reinstated')

    tranches = db_cache.get_tranches(CAMPAIGN, active_only=True)
    assert tranches[1] == ['AAA']
    audit = db_cache.get_tranche_audit(CAMPAIGN)
    row = [r for r in audit if r['ticker'] == 'AAA'][0]
    assert row['active'] == 1
    assert row['removed_at'] is None


def test_sweep_campaign_config_upserts(isolated_db):
    db_cache.set_sweep_campaign_config(CAMPAIGN, version='v5', fixed_sls='1 2', strategies='S1', entry_timing='close')
    db_cache.set_sweep_campaign_config(CAMPAIGN, version='v5.1', fixed_sls='1 2 3', strategies='S1 S2', entry_timing='open_check')

    cfg = db_cache.get_sweep_campaign_config(CAMPAIGN)
    assert cfg['version'] == 'v5.1'
    assert cfg['fixed_sls'] == '1 2 3'
    assert cfg['entry_timing'] == 'open_check'


def test_get_sweep_campaign_config_returns_none_when_unset(isolated_db):
    assert db_cache.get_sweep_campaign_config('nonexistent_campaign') is None
