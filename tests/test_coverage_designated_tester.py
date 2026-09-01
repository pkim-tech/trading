"""Tests for scripts/coverage_designated_tester.py's compute_designations() --
covers the 2026-08-15/16 SCRIPT_BASED_TESTERS extension: a row proven by directly
running a script (no node/ticker to track) must render as a distinguishable
('SCRIPT', path) sentinel, without disturbing node-based designation for every
other row. See docs/design.md's 2026-08-15 "Script-based test-plan design" entry."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import scripts.coverage_designated_tester as tester_mod
from scripts.coverage_designated_tester import compute_designations


NODE_ROW = dict(id='node_based_row', check_mechanism='scenario_expectations',
                 scenario_key='node_based_row')
SCRIPT_ROW = dict(id='script_based_row', check_mechanism='coverage_events',
                   scenario_key='script_based_row')
NEITHER_ROW = dict(id='undesignated_row', check_mechanism='coverage_events',
                    scenario_key='undesignated_row')


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(tester_mod, 'REGISTRY', [NODE_ROW, SCRIPT_ROW, NEITHER_ROW])
    monkeypatch.setattr(tester_mod.db, 'get_staged_test_configs', lambda: [])
    monkeypatch.setattr(
        tester_mod.db, 'get_scenario_expectations',
        lambda active_only=False: [dict(scenario_key='node_based_row', ticker='SOXL', node_id=92)],
    )
    monkeypatch.setattr(tester_mod, 'SCRIPT_BASED_TESTERS',
                         {'script_based_row': 'scripts/stage_check_order_guard_scenarios.py'})
    return tester_mod


def test_node_based_designation_unaffected(patched):
    result = compute_designations()
    assert result['node_based_row'] == [('SOXL', 92)]


def test_script_based_designation_returns_sentinel(patched):
    result = compute_designations()
    assert result['script_based_row'] == [('SCRIPT', 'scripts/stage_check_order_guard_scenarios.py')]


def test_row_with_neither_returns_empty(patched):
    result = compute_designations()
    assert result['undesignated_row'] == []


def test_real_script_based_testers_keys_are_real_registry_rows():
    from scripts.coverage_registry import REGISTRY, SCRIPT_BASED_TESTERS
    real_ids = {r['id'] for r in REGISTRY}
    missing = set(SCRIPT_BASED_TESTERS) - real_ids
    assert not missing, f"SCRIPT_BASED_TESTERS references nonexistent REGISTRY ids: {missing}"
