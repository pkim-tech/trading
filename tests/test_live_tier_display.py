"""Display-layer split between "real live" and "test live" nodes (2026-08-17).

Both tiers are state='live' and both really do place orders at the real
broker -- the only difference is deliberate size (soxl_ira's $50-$800
staged-test nodes vs. ira/roth/brokerage's real capital-at-stake ones).
watch_list.state is real gating logic (schwab_safety/signals_notify key off
'live'), so this distinction is derived for DISPLAY ONLY, from the already-
existing has_capital_at_stake/effectively_dry_run facts -- these tests pin
both halves of that contract: the new granular labels, and the fact that
mode_tag's DEFAULT output (used inside real order-placement alerts) is
byte-identical to before.
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_blocks
import signals_config
import signals_db as db
import signals_helpers as helpers
import signals_notify
import schwab_safety


REAL_ACCOUNT = 'roth'      # trading_enabled=True in the fixture below
DORMANT_ACCOUNT = 'brokerage'  # trading_enabled=False in the fixture below


@pytest.fixture
def env(monkeypatch, tmp_path):
    # tmp_path, not NamedTemporaryFile(delete=False) -- the latter leaks one
    # file per test (cold review, 2026-08-17).
    monkeypatch.setattr(signals_config, 'DB_PATH', tmp_path / 'tier_test.db')
    # Explicit accounts dict, not the real one -- these assertions must not
    # depend on whichever accounts happen to be trading_enabled today.
    monkeypatch.setattr(schwab_safety, 'ACCOUNTS', {
        REAL_ACCOUNT: schwab_safety.AccountLimits(
            enabled=True, notional_cap=100_000, daily_order_cap=100,
            trading_enabled=True, cash_settlement_type='cash'),
        DORMANT_ACCOUNT: schwab_safety.AccountLimits(
            enabled=True, notional_cap=100_000, daily_order_cap=100,
            trading_enabled=False, cash_settlement_type='margin'),
    })
    monkeypatch.setattr(signals_config, 'CAPITAL_AT_STAKE_THRESHOLD', 5_000.0)
    db.ensure_tables()
    yield


def _node(**kw):
    node = {'ticker': 'TEST_TIER', 'strategy': 'TrailingBothZScoreBreakout',
            'version': 'v5', 'window': 10, 'state': 'live',
            'account': REAL_ACCOUNT, 'starting_notional': 10_000}
    node.update(kw)
    return node


# ── the real-vs-test predicate ────────────────────────────────────────────
def test_real_live_node_is_not_test_live(env):
    assert helpers.has_capital_at_stake(_node()) is True
    assert helpers.is_test_live(_node()) is False


def test_small_notional_live_node_is_test_live(env):
    node = _node(starting_notional=500)
    assert helpers.has_capital_at_stake(node) is False
    assert helpers.is_test_live(node) is True


def test_dry_run_and_paper_nodes_are_never_test_live(env):
    # state != 'live' -> simulated, not the "real orders, small size" tier.
    assert helpers.is_test_live(_node(state='dry_run', starting_notional=500)) is False
    assert helpers.is_test_live(_node(state='paper', starting_notional=500)) is False
    # state == 'live' but the ACCOUNT ceiling blocks real submission: also
    # not test-live (nothing reaches the broker at all).
    assert helpers.is_test_live(
        _node(account=DORMANT_ACCOUNT, starting_notional=500)) is False


def test_unknown_account_is_not_labelled_test_live(env):
    # Must fail toward mode_tag's alarming UNKNOWN, never toward a
    # reassuring "it's only a test" (same failure direction as mode_tag's
    # own None-account rule).
    assert helpers.is_test_live(_node(account=None, starting_notional=500)) is False
    assert helpers.is_test_live(_node(account='not_a_real_account',
                                      starting_notional=500)) is False
    assert helpers.is_test_live(None) is False


# ── mode_tag: default output must be unchanged ────────────────────────────
def test_mode_tag_default_output_is_unchanged_for_every_tier(env):
    """The ~90 existing call sites (incl. schwab_client's real order-
    rejection alerts) must keep getting exactly LIVE/DRY-RUN/UNKNOWN."""
    assert helpers.mode_tag(REAL_ACCOUNT, _node()) == 'LIVE'
    assert helpers.mode_tag(REAL_ACCOUNT, _node(starting_notional=500)) == 'LIVE'
    assert helpers.mode_tag(REAL_ACCOUNT, _node(state='dry_run')) == 'DRY-RUN'
    assert helpers.mode_tag(DORMANT_ACCOUNT, _node()) == 'DRY-RUN'
    assert helpers.mode_tag(None, _node()) == 'UNKNOWN'
    assert helpers.mode_tag('not_a_real_account', _node()) == 'UNKNOWN'
    assert helpers.mode_tag(REAL_ACCOUNT) == 'LIVE'  # node-less legacy call


def test_mode_tag_granular_splits_live_into_real_and_test(env):
    assert helpers.mode_tag(REAL_ACCOUNT, _node(), granular=True) == 'LIVE'
    assert helpers.mode_tag(REAL_ACCOUNT, _node(starting_notional=500),
                            granular=True) == 'TEST-LIVE'


def test_mode_tag_granular_leaves_dry_run_and_unknown_alone(env):
    assert helpers.mode_tag(REAL_ACCOUNT, _node(state='dry_run'), granular=True) == 'DRY-RUN'
    assert helpers.mode_tag(DORMANT_ACCOUNT, _node(), granular=True) == 'DRY-RUN'
    assert helpers.mode_tag(None, _node(), granular=True) == 'UNKNOWN'
    # No node to judge size from -- must not claim TEST-LIVE on a guess.
    assert helpers.mode_tag(REAL_ACCOUNT, None, granular=True) == 'LIVE'


# ── state_label: the shared CLI/Streamlit label ───────────────────────────
def test_state_label_splits_live_and_passes_other_states_through(env):
    assert helpers.state_label(_node()) == 'live-real'
    assert helpers.state_label(_node(starting_notional=500)) == 'live-test'
    assert helpers.state_label(_node(state='paper')) == 'paper'
    assert helpers.state_label(_node(state='dry_run')) == 'dry_run'
    assert helpers.state_label(_node(account=DORMANT_ACCOUNT)) == 'live-dryrun'
    assert helpers.state_label(_node(account=None)) == 'live-unknown'
    assert helpers.state_label(None) == ''


def test_state_label_never_reads_as_real_for_a_test_node(env):
    """Regression guard on the whole point: the two live tiers must never
    render as the same string."""
    assert helpers.state_label(_node()) != helpers.state_label(_node(starting_notional=500))


# ── the reference table (CLI view) carries it ─────────────────────────────
def test_reference_table_has_a_tier_column(env):
    assert 'Tier' in signals_notify._REF_TABLE_COLS
    rendered = signals_notify.format_reference_table([
        {'Ticker': 'AAA', 'Tier': 'live-real', 'Account': REAL_ACCOUNT},
        {'Ticker': 'BBB', 'Tier': 'live-test', 'Account': REAL_ACCOUNT},
    ])
    assert 'Tier' in rendered.splitlines()[0]
    assert 'live-real' in rendered
    assert 'live-test' in rendered


# ── unknown size must not read as the reassuring "only a test" ────────────
def test_live_node_with_no_configured_size_is_not_labelled_test_live(env):
    """Paired Opus review, 2026-08-17 (cold found, contextual reproduced):
    has_capital_at_stake falls back to `starting_notional or 0` when
    _last_sale_recovery can't resolve, and 0 >= threshold is False --
    indistinguishable from a genuinely small node. A size-less live node
    must fail toward 'we don't know', never toward 'it's only a test'."""
    for missing in (None, 0):
        node = _node(starting_notional=missing)
        assert helpers.node_size_is_known(node) is False
        assert helpers.is_test_live(node) is False
        assert helpers.state_label(node) == 'live-unknown'
        assert helpers.mode_tag(REAL_ACCOUNT, node, granular=True) == 'LIVE'


def test_starting_notional_override_counts_as_a_known_size(env):
    # RETL (id=143) really runs with an override in production, so this is
    # the live-relevant path, not a hypothetical.
    node = _node(starting_notional=None, starting_notional_override=500)
    assert helpers.node_size_is_known(node) is True
    assert helpers.is_test_live(node) is True
    assert helpers.state_label(node) == 'live-test'
    big = _node(starting_notional=None, starting_notional_override=25_000)
    assert helpers.is_test_live(big) is False
    assert helpers.state_label(big) == 'live-real'


def test_is_test_live_honours_the_account_it_is_given(env):
    """mode_tag decides DRY-RUN from its own `account` argument; is_test_live
    must judge the SAME account, not re-derive a different one from the node
    (paired review LOW finding -- schwab_client._mode_tag_for and
    signals_handlers both pair an account with a separately-looked-up node)."""
    node = _node(starting_notional=500)          # node says REAL_ACCOUNT
    assert helpers.is_test_live(node, DORMANT_ACCOUNT) is False
    assert helpers.mode_tag(DORMANT_ACCOUNT, node, granular=True) == 'DRY-RUN'
    assert helpers.is_test_live(node, REAL_ACCOUNT) is True


# ── the Slack per-ticker row carries it ───────────────────────────────────
def _flat_row(tier):
    return {'Ticker': 'AAA', 'Version': 'v5', 'Tier': tier, 'Account': REAL_ACCOUNT,
            'Held': False, 'Next Action': 'Waiting Buy Trigger', 'Trigger Label': 'z-cross',
            'Next Trigger $': 10.0, 'Now': 11.0, 'Proximity': 10.0, 'Z': -1.0,
            'Overnight %': 0.5, 'TrailBuy%': 1, 'Arm%': 16, 'TrailSell%': 7,
            'State': 'live', '_node': _node(), '_pos': None}


def test_ticker_block_marks_a_test_live_row(env):
    """The signal-window alert renders through this block from the UNFILTERED
    watchlist, so mixed-tier rows are the normal case there (both reviewers)."""
    test_text = signals_blocks._ticker_block(_flat_row('live-test'))[0]['text']['text']
    real_text = signals_blocks._ticker_block(_flat_row('live-real'))[0]['text']['text']
    assert '🧪TEST-LIVE' in test_text
    assert '🧪TEST-LIVE' not in real_text


def test_build_reference_table_populates_tier(env, monkeypatch):
    db.add_node('TEST_TIER_REAL', 'TrailingBothZScoreBreakout', 'v5', window=10,
                take_profit=16.0, stop_loss=1, max_hold_hours=105, state='live',
                fixed_sl_override=1.0, account=REAL_ACCOUNT, starting_notional=10_000)
    db.add_node('TEST_TIER_SMALL', 'TrailingBothZScoreBreakout', 'v5', window=10,
                take_profit=16.0, stop_loss=1, max_hold_hours=105, state='live',
                fixed_sl_override=1.0, account=REAL_ACCOUNT, starting_notional=500)
    nodes = {n['ticker']: n for n in db.get_watchlist()}
    # No price data for these synthetic tickers -> the NO_DATA row branch,
    # which is exactly where the Tier field has to be populated too.
    monkeypatch.setattr(signals_notify.compute, 'compute_buy_signal', lambda node, **kw: None)
    rows = {r['Ticker']: r for r in signals_notify.build_reference_table(
        [nodes['TEST_TIER_REAL'], nodes['TEST_TIER_SMALL']])}
    assert rows['TEST_TIER_REAL']['Tier'] == 'live-real'
    assert rows['TEST_TIER_SMALL']['Tier'] == 'live-test'


def test_build_reference_table_populates_tier_on_flat_and_held_branches(env, monkeypatch):
    """The NO_DATA branch above is only 1 of the 3 rows.append() sites --
    cold review flagged the flat/pending and held-position branches as
    untested (each has its own dict literal, so each can drift alone)."""
    db.add_node('TEST_TIER_FLAT', 'TrailingBothZScoreBreakout', 'v5', window=10,
                take_profit=16.0, stop_loss=1, max_hold_hours=105, state='live',
                fixed_sl_override=1.0, account=REAL_ACCOUNT, starting_notional=500)
    db.add_node('TEST_TIER_HELD', 'TrailingBothZScoreBreakout', 'v5', window=10,
                take_profit=16.0, stop_loss=1, max_hold_hours=105, state='live',
                fixed_sl_override=1.0, account=REAL_ACCOUNT, starting_notional=10_000)
    nodes = {n['ticker']: n for n in db.get_watchlist()}
    held_node = nodes['TEST_TIER_HELD']
    now_iso = datetime(2026, 8, 17, 10, 30).isoformat()
    assert db.open_position(held_node, 100.0, now_iso, 100.0, now_iso, shares=100) is True

    fake_sig = {'current_price': 100.0, 'lower_band': 90.0, 'z_score': -1.0,
                'prev_close': 99.0, 'last_daily_bar': '2026-08-17'}
    monkeypatch.setattr(signals_notify.compute, 'compute_buy_signal',
                        lambda node, **kw: dict(fake_sig))
    monkeypatch.setattr(signals_notify.compute, '_bars_held', lambda *a, **kw: 1.0)

    rows = {r['Ticker']: r for r in signals_notify.build_reference_table(
        [nodes['TEST_TIER_FLAT'], held_node])}
    assert rows['TEST_TIER_FLAT']['Held'] is False       # flat/pending branch
    assert rows['TEST_TIER_FLAT']['Tier'] == 'live-test'
    assert rows['TEST_TIER_HELD']['Held'] is True        # held-position branch
    assert rows['TEST_TIER_HELD']['Tier'] == 'live-real'
