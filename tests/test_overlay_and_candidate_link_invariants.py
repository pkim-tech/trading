"""Task #7 (2026-08-19): two new signals_invariants.py checks.

1. check_live_node_missing_candidate_link -- promotes scripts/evening_status.py
   Part 2's real live nodes with no watch_list_candidate_link report (printed
   only, never alerted) to a real check.
2. check_live_overlay_missing_validation_link -- the new overlay-level
   traceability check, built after finding SOXS/ira (real $10k) ran with
   drought_overlay_enabled=1/confirm_days=1 for a week with zero validation
   ever run. Backed by a new watch_list_overlay_link table
   (set_overlay_link/get_overlay_links)."""
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import signals_invariants


@pytest.fixture
def env(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    signals_db.ensure_tables()
    yield
    Path(tmp_db.name).unlink()


def _add_live_node(ticker, drought=False, addon=False, account='ira'):
    signals_db.add_node(
        ticker=ticker, strategy='TrailingBothZScoreBreakout', version='v5',
        window=10, take_profit=9.0, stop_loss=1.0, max_hold_hours=48,
        label='test', trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
        account=account, starting_notional=10000.0, state='live',
    )
    node = [n for n in signals_db.get_watchlist() if n['ticker'] == ticker][0]
    if drought or addon:
        with signals_db._conn() as c:
            c.execute("UPDATE watch_list SET drought_overlay_enabled=?, addon_enabled=? WHERE id=?",
                       (int(drought), int(addon), node['id']))
            c.commit()
    return signals_db.get_watch_list_node_by_id(node['id'])


# --- candidate link check ---------------------------------------------------

def test_flags_live_node_with_no_candidate_link(env):
    node = _add_live_node('CANDTEST')
    violations = signals_invariants.check_live_node_missing_candidate_link()
    assert any(f"wl_id={node['id']}" in v for v in violations)


def test_does_not_flag_live_node_with_a_candidate_link(env):
    node = _add_live_node('CANDTEST2')
    # candidate_node_id doesn't need to resolve to a real candidate_nodes row
    # for THIS check -- set_overlay_link/set_candidate_link's own validation
    # is a separate concern (write-time trust); this check only asks "is
    # there a row at all". Insert directly to avoid needing a real
    # cache/research/trading_universe.db in this isolated test.
    with signals_db._conn() as c:
        c.execute("INSERT INTO watch_list_candidate_link (wl_id, candidate_node_id, role) VALUES (?, ?, 'core')",
                   (node['id'], 999))
        c.commit()
    violations = signals_invariants.check_live_node_missing_candidate_link()
    assert not any(f"wl_id={node['id']}" in v for v in violations)


def test_does_not_flag_paper_node_with_no_candidate_link(env):
    signals_db.add_node(
        ticker='PAPERTEST', strategy='TrailingBothZScoreBreakout', version='v5',
        window=10, take_profit=9.0, stop_loss=1.0, max_hold_hours=48,
        label='test', trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
        account='roth', starting_notional=10000.0, state='paper',
    )
    violations = signals_invariants.check_live_node_missing_candidate_link()
    assert not any('PAPERTEST' in v for v in violations)


# --- overlay validation link check ------------------------------------------

def test_flags_live_node_with_drought_enabled_and_no_overlay_link(env):
    node = _add_live_node('DROUGHTTEST', drought=True)
    violations = signals_invariants.check_live_overlay_missing_validation_link()
    assert any(f"wl_id={node['id']}" in v and 'drought_overlay' in v for v in violations)


def test_flags_live_node_with_addon_enabled_and_no_overlay_link(env):
    node = _add_live_node('ADDONTEST', addon=True)
    violations = signals_invariants.check_live_overlay_missing_validation_link()
    assert any(f"wl_id={node['id']}" in v and "overlay_type='addon'" in v for v in violations)


def test_does_not_flag_live_node_with_neither_overlay_enabled(env):
    node = _add_live_node('PLAINTEST')
    violations = signals_invariants.check_live_overlay_missing_validation_link()
    assert not any(f"wl_id={node['id']}" in v for v in violations)


def test_does_not_flag_after_backfilling_a_real_validation_link(env):
    """Regression for the exact incident this check exists to close: SOXL's
    drought config has a real, on-file validation (docs/research_log.md
    2026-08-07, REAL_SELECTION) -- once linked, it must stop being flagged."""
    node = _add_live_node('SOXLLIKE', drought=True)
    signals_db.set_overlay_link(
        wl_id=node['id'], overlay_type='drought_overlay',
        validation_ref="docs/research_log.md 2026-08-07 entry, confirm_days=3/vol_gate=0.4",
        verdict='REAL_SELECTION',
    )
    violations = signals_invariants.check_live_overlay_missing_validation_link()
    assert not any(f"wl_id={node['id']}" in v for v in violations)


def test_drought_link_does_not_satisfy_addon_requirement(env):
    """A node with BOTH overlays enabled needs BOTH links -- validating one
    must not silently satisfy the other (the exact RETL/ERY shape: drought
    config values match SOXL's but were explicitly NOT validated per-ticker,
    and addon is a separate mechanism entirely)."""
    node = _add_live_node('BOTHTEST', drought=True, addon=True)
    signals_db.set_overlay_link(
        wl_id=node['id'], overlay_type='drought_overlay',
        validation_ref="some real validation", verdict='REAL_SELECTION',
    )
    violations = signals_invariants.check_live_overlay_missing_validation_link()
    assert not any(f"wl_id={node['id']}" in v and 'drought_overlay' in v for v in violations)
    assert any(f"wl_id={node['id']}" in v and "overlay_type='addon'" in v for v in violations)


# --- set_overlay_link / get_overlay_links -----------------------------------

def test_set_overlay_link_requires_a_real_watch_list_row(env):
    with pytest.raises(ValueError):
        signals_db.set_overlay_link(wl_id=999999, overlay_type='drought_overlay',
                                     validation_ref="doesn't matter")


def test_set_overlay_link_requires_a_non_empty_validation_ref(env):
    node = _add_live_node('REFTEST')
    with pytest.raises(ValueError):
        signals_db.set_overlay_link(wl_id=node['id'], overlay_type='drought_overlay', validation_ref="   ")


def test_relinking_preserves_note_when_omitted(env):
    node = _add_live_node('RELINKTEST', drought=True)
    signals_db.set_overlay_link(wl_id=node['id'], overlay_type='drought_overlay',
                                 validation_ref="v1", verdict='REAL_SELECTION', note="original note")
    signals_db.set_overlay_link(wl_id=node['id'], overlay_type='drought_overlay',
                                 validation_ref="v2 (updated)", verdict='REAL_SELECTION')
    links = signals_db.get_overlay_links(wl_id=node['id'])
    assert len(links) == 1
    assert links[0]['validation_ref'] == "v2 (updated)"
    assert links[0]['note'] == "original note"


def test_relinking_preserves_verdict_when_omitted(env):
    """Regression for a real bug caught by cold review, 2026-08-19: a
    re-link that only updates validation_ref (no verdict= passed) used to
    silently NULL a previously-set REAL_SELECTION verdict."""
    node = _add_live_node('VERDICTPRESERVE', drought=True)
    signals_db.set_overlay_link(wl_id=node['id'], overlay_type='drought_overlay',
                                 validation_ref="v1", verdict='REAL_SELECTION')
    signals_db.set_overlay_link(wl_id=node['id'], overlay_type='drought_overlay',
                                 validation_ref="v2, re-confirmed same verdict")
    links = signals_db.get_overlay_links(wl_id=node['id'])
    assert len(links) == 1
    assert links[0]['verdict'] == 'REAL_SELECTION', (
        f"verdict should be preserved across a re-link that omits it, got {links[0]['verdict']!r}"
    )


def test_overlay_type_is_normalized_case_and_whitespace(env):
    """Regression for a real bug caught by cold review, 2026-08-19:
    'Drought_Overlay' used to create a SECOND row alongside 'drought_overlay'
    instead of colliding on the UNIQUE constraint, and would never satisfy
    the check (which compares against the literal lowercase tuple)."""
    node = _add_live_node('NORMTEST', drought=True)
    signals_db.set_overlay_link(wl_id=node['id'], overlay_type=' Drought_Overlay ',
                                 validation_ref="real validation", verdict='REAL_SELECTION')
    links = signals_db.get_overlay_links(wl_id=node['id'])
    assert len(links) == 1
    assert links[0]['overlay_type'] == 'drought_overlay'
    violations = signals_invariants.check_live_overlay_missing_validation_link()
    assert not any(f"wl_id={node['id']}" in v for v in violations), (
        "a case/whitespace-variant overlay_type must still satisfy the check"
    )


def test_no_real_selection_verdict_flagged_distinctly_not_silenced(env):
    """Regression for a real bug caught by paired review, 2026-08-19: the
    real KORU shape (drought config found to be a pure overfitting artifact
    and REJECTED, yet still live) must NOT read identically to "never
    checked" -- a NO_REAL_SELECTION-verdict link is flagged separately and
    more loudly, not treated as satisfying the check."""
    node = _add_live_node('REJECTEDTEST', drought=True)
    signals_db.set_overlay_link(wl_id=node['id'], overlay_type='drought_overlay',
                                 validation_ref="docs/research_log.md -- found to be overfitting, rejected",
                                 verdict='NO_REAL_SELECTION')
    violations = signals_invariants.check_live_overlay_missing_validation_link()
    matches = [v for v in violations if f"wl_id={node['id']}" in v]
    assert len(matches) == 1, f"expected exactly one violation for the rejected-verdict node, got {matches}"
    assert 'NO_REAL_SELECTION' in matches[0] and 'REJECTED' in matches[0].upper()


def test_traceability_checks_excluded_from_run_all(env):
    """Regression for the paired review's HIGH finding, 2026-08-19: these
    two checks surface real, expected-to-persist backlog gaps (not "should
    always be zero" bugs) -- folding them into CHECKS/run_all() would turn a
    genuinely clean 0-violation signal into a permanent noisy wall with no
    ack mechanism, alerted at every daemon startup/07:00/EOD. They must be
    reachable only via TRACEABILITY_CHECKS/run_traceability_checks(), not
    run_all()."""
    assert signals_invariants.check_live_node_missing_candidate_link not in signals_invariants.CHECKS
    assert signals_invariants.check_live_overlay_missing_validation_link not in signals_invariants.CHECKS
    assert signals_invariants.check_live_node_missing_candidate_link in signals_invariants.TRACEABILITY_CHECKS
    assert signals_invariants.check_live_overlay_missing_validation_link in signals_invariants.TRACEABILITY_CHECKS

    node = _add_live_node('RUNALLSCOPE')
    # check_market_data_freshness (2026-08-28, real CHECKS member as of
    # yesterday's session) reads cache/research/{ticker}_1h.csv directly off
    # disk, unrelated to this test's tmp-DB isolation -- a synthetic ticker
    # with no real cache file would otherwise legitimately fail that check
    # too, which isn't what this test is regression-testing (that's the
    # freshness check's own job, covered by its own test elsewhere). Give it
    # a fresh, current bar so only the traceability-link gap under test can
    # produce a wl_id match here.
    csv_path = Path('cache/research/RUNALLSCOPE_1h.csv')
    csv_path.write_text(
        "Datetime,Close,High,Low,Open,Volume\n"
        f"{pd.Timestamp.now(tz='America/New_York').tz_localize(None).normalize() + pd.Timedelta(hours=9, minutes=30)},"
        "10.0,10.1,9.9,10.0,1000000\n"
    )
    try:
        assert not any(f"wl_id={node['id']}" in v for v in signals_invariants.run_all()), (
            "an unlinked live node must not appear in run_all()'s output"
        )
    finally:
        csv_path.unlink(missing_ok=True)
    assert any(f"wl_id={node['id']}" in v for v in signals_invariants.run_traceability_checks())
