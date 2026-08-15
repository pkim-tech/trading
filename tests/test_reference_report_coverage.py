"""Test for the Morning Report delivery coverage event (signals_notify.
send_reference_report) -- added 2026-07-27 evening after the user flagged the
accountability grid had no scenario at all for whether the report actually
posts to Slack, distinct from whether it gets built correctly (it silently
posted with zero candidate rows for weeks, 2026-07-23, with nothing tracking
delivery). Isolated DB, no real Slack posts."""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_blocks
import signals_compute
import signals_config
import signals_db
import signals_notify


@pytest.fixture
def env(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    signals_db.ensure_tables()
    yield
    Path(tmp_db.name).unlink()


def test_send_reference_report_logs_sent_on_successful_post(env, monkeypatch):
    # Empty watchlist -> no held/candidate rows -> still goes through the
    # normal _post_chunked path (2026-08-08: the earlier "nothing to report"
    # short-circuit was removed by paired Opus review -- it silently dropped
    # the kill-switch status and Start/Stop Engine buttons from Slack every
    # day once zero nodes crossed the capital-at-stake threshold).
    monkeypatch.setattr(signals_notify, '_post_chunked', lambda *a, **kw: ("C123", "1234.5678"))
    signals_notify.send_reference_report([])
    events = signals_db.get_coverage_events(scenario_key="morning_report_delivery")
    assert len(events) == 1
    assert events[0]['result'] == "sent"


def test_send_reference_report_logs_no_delivery_confirmation_on_failed_post(env, monkeypatch):
    monkeypatch.setattr(signals_notify, '_post_chunked', lambda *a, **kw: (None, None))
    signals_notify.send_reference_report([])
    events = signals_db.get_coverage_events(scenario_key="morning_report_delivery")
    assert len(events) == 1
    assert events[0]['result'] == "no_delivery_confirmation"


def test_send_reference_report_actually_chunks_a_large_watchlist(env, monkeypatch):
    """Exercises the real build_reference_table -> _ticker_block -> _post_chunked
    path end-to-end against a synthetic 60-row watchlist -- the unit tests for
    _post_chunked itself only use synthetic block lists, so nothing previously
    proved real report rows chunk correctly at scale (Opus review, 2026-07-26)."""
    # _node must be a real capital-at-stake node (2026-08-08: Buy Candidates
    # rows are now filtered to has_capital_at_stake) -- state='live' in a
    # trading_enabled account, starting_notional over the $10k bar.
    fake_rows = [
        {"Ticker": f"T{i}", "Version": "v5", "Held": False, "Next Action": "NO_DATA", "Strategy": "Test",
         "_node": {"state": "live", "account": "soxl_ira", "starting_notional": 20_000}}
        for i in range(60)
    ]
    monkeypatch.setattr(signals_notify, 'build_reference_table', lambda watchlist: fake_rows)

    calls = []
    def fake_post(text, blocks=None, thread_ts=None, reply_broadcast=False):
        calls.append(blocks)
        return (f"C{len(calls)}", f"{len(calls)}.0")
    monkeypatch.setattr(signals_blocks, '_post_message', fake_post)

    signals_notify.send_reference_report([])

    assert len(calls) > 1  # 60 rows must not fit in a single 50-block message
    assert all(len(blocks) <= 50 for blocks in calls)
    rendered_tickers = {b["text"]["text"].split("*")[1] for blocks in calls for b in blocks
                         if b.get("type") == "section" and "T" in b.get("text", {}).get("text", "")}
    assert rendered_tickers == {f"T{i}" for i in range(60)}


def test_build_reference_table_shows_a_held_paper_position(env, monkeypatch):
    # Real 2026-07-28 bug: build_reference_table only read open_positions/
    # pending_buys (real+dry_run), so a research-mode node with a real open
    # paper position (SOXL, 463 shares) rendered as flat/all-grey Phase
    # bubbles -- the report claimed nothing was happening while a real paper
    # trade sat open for hours. Fixed by merging in get_open_positions(paper=
    # True)/get_paper_pending_buys(). No test previously created a paper
    # position and checked the resulting row, which is exactly why this went
    # unnoticed for days (test_paper_trading.py tests the fill logic in
    # isolation; this file tested report delivery, never their intersection).
    monkeypatch.setattr(signals_compute, '_current_price', lambda ticker: (100.0, None))
    monkeypatch.setattr(signals_compute, 'compute_buy_signal', lambda node: {
        'ticker': node['ticker'], 'current_price': 100.0, 'z_score': -2.5,
        'lower_band': 95.0, 'prev_close': 98.0, 'last_daily_bar': '2026-07-28',
    })
    signals_db.add_node(
        ticker='PAPERTEST', strategy='TrailingBothZScoreBreakout', version='v5', window=10,
        take_profit=30.0, stop_loss=2, max_hold_hours=70, state='paper', account='ira',
        trail_buy_pct=3.0, trail_pct=1.0,
    )
    node = signals_db.get_watch_list_node(ticker='PAPERTEST')
    opened = signals_db.open_position(
        node, signal_price=105.0, signal_time='2026-07-27 14:30:00',
        entry_price=100.0, entry_time='2026-07-28 11:00:00', shares=100, paper=True,
    )
    assert opened

    rows = signals_notify.build_reference_table([node])
    assert len(rows) == 1
    assert rows[0]['Held'] is True
    assert rows[0]['Phase'] != '⚪⚪⚪⚪'


# ---------------------------------------------------------------------------
# origin column (2026-08-15) -- bugs #54 and #63-64, both fixed via ONE stamped
# column both consumers read, rather than two independent patches that could
# drift. build_reference_table merges paper_positions and open_positions into a
# single wl_id-keyed dict, which destroys the only signal of which table a row
# came from; origin survives that merge.
# ---------------------------------------------------------------------------

def _paper_node_with_position(ticker='PAPERORIGIN', state='paper', shares=100):
    signals_db.add_node(
        ticker=ticker, strategy='TrailingBothZScoreBreakout', version='v5', window=10,
        take_profit=30.0, stop_loss=2, max_hold_hours=70, state=state, account='ira',
        trail_buy_pct=3.0, trail_pct=1.0,
    )
    node = signals_db.get_watch_list_node(ticker=ticker)
    assert signals_db.open_position(
        node, signal_price=105.0, signal_time='2026-07-27 14:30:00',
        entry_price=100.0, entry_time='2026-07-28 11:00:00', shares=shares, paper=True,
    )
    return node


def _patch_prices(monkeypatch):
    monkeypatch.setattr(signals_compute, '_current_price', lambda ticker: (100.0, None))
    monkeypatch.setattr(signals_compute, 'compute_buy_signal', lambda node: {
        'ticker': node['ticker'], 'current_price': 100.0, 'z_score': -2.5,
        'lower_band': 95.0, 'prev_close': 98.0, 'last_daily_bar': '2026-07-28',
    })


def test_paper_position_row_is_stamped_origin_paper(env, monkeypatch):
    _patch_prices(monkeypatch)
    node = _paper_node_with_position()
    rows = signals_notify.build_reference_table([node])
    assert rows[0]['_pos']['origin'] == 'paper', (
        "the column must survive build_reference_table's paper/real dict merge")


def test_real_position_row_is_stamped_origin_live(env, monkeypatch):
    _patch_prices(monkeypatch)
    signals_db.add_node(
        ticker='LIVEORIGIN', strategy='TrailingBothZScoreBreakout', version='v5', window=10,
        take_profit=30.0, stop_loss=2, max_hold_hours=70, state='live', account='ira',
        trail_buy_pct=3.0, trail_pct=1.0,
    )
    node = signals_db.get_watch_list_node(ticker='LIVEORIGIN')
    assert signals_db.open_position(
        node, signal_price=105.0, signal_time='2026-07-27 14:30:00',
        entry_price=100.0, entry_time='2026-07-28 11:00:00', shares=100)
    rows = signals_notify.build_reference_table([node])
    assert rows[0]['_pos']['origin'] == 'live'


def test_bug54_paper_position_renders_a_paper_tag(env, monkeypatch):
    """Bug #54, found live on AGQ: _ticker_block's `if row['Held']:` branch
    tagged is_dry_run_sim but had NO way to tell a paper position from a real
    one, so a simulated position rendered byte-identically to a real held
    position -- same entry price, same share count, same actionable framing."""
    _patch_prices(monkeypatch)
    node = _paper_node_with_position()
    rows = signals_notify.build_reference_table([node])
    blocks = signals_notify._ticker_block(rows[0])
    text = blocks[0]['text']['text']
    assert '📄PAPER' in text, f"a paper position must be visibly marked: {text!r}"


def test_bug54_real_position_gets_no_paper_tag(env, monkeypatch):
    """The tag must discriminate, not decorate everything."""
    _patch_prices(monkeypatch)
    signals_db.add_node(
        ticker='LIVENOTAG', strategy='TrailingBothZScoreBreakout', version='v5', window=10,
        take_profit=30.0, stop_loss=2, max_hold_hours=70, state='live', account='ira',
        trail_buy_pct=3.0, trail_pct=1.0,
    )
    node = signals_db.get_watch_list_node(ticker='LIVENOTAG')
    assert signals_db.open_position(
        node, signal_price=105.0, signal_time='2026-07-27 14:30:00',
        entry_price=100.0, entry_time='2026-07-28 11:00:00', shares=100)
    rows = signals_notify.build_reference_table([node])
    text = signals_notify._ticker_block(rows[0])[0]['text']['text']
    assert '📄PAPER' not in text, text


def test_paper_position_gets_no_manual_close_button(env, monkeypatch):
    """The more dangerous half of bug #54. A paper row used to render a real
    'Manually Close' button carrying position_id=paper_positions.id, while the
    manual_close handler resolves against open_positions -- two INDEPENDENT id
    sequences, so the id could match a completely unrelated REAL position and
    close it."""
    _patch_prices(monkeypatch)
    node = _paper_node_with_position()
    rows = signals_notify.build_reference_table([node])
    blocks = signals_notify._ticker_block(rows[0])
    actions = [b for b in blocks if b.get('type') == 'actions']
    close_buttons = [e for b in actions for e in b.get('elements', [])
                     if e.get('action_id') == 'manual_close']
    assert close_buttons == [], (
        f"a paper position must never offer a real close action: {close_buttons}")


def test_real_position_still_gets_its_manual_close_button(env, monkeypatch):
    """Regression: the suppression must not blind the real case."""
    _patch_prices(monkeypatch)
    signals_db.add_node(
        ticker='LIVECLOSE', strategy='TrailingBothZScoreBreakout', version='v5', window=10,
        take_profit=30.0, stop_loss=2, max_hold_hours=70, state='live', account='ira',
        trail_buy_pct=3.0, trail_pct=1.0,
    )
    node = signals_db.get_watch_list_node(ticker='LIVECLOSE')
    assert signals_db.open_position(
        node, signal_price=105.0, signal_time='2026-07-27 14:30:00',
        entry_price=100.0, entry_time='2026-07-28 11:00:00', shares=100)
    rows = signals_notify.build_reference_table([node])
    blocks = signals_notify._ticker_block(rows[0])
    close_buttons = [e for b in blocks if b.get('type') == 'actions'
                     for e in b.get('elements', []) if e.get('action_id') == 'manual_close']
    assert len(close_buttons) == 1, blocks


def test_bug63_64_phantom_paper_row_for_a_live_node_is_marked_not_silent(env, monkeypatch):
    """Bugs #63-64, found live on SOXL/ira. A stale paper row for a wl_id with
    NO real counterpart survives build_reference_table's merge (real rows only
    overwrite on collision), so it rendered as a phantom REAL held position on
    a live node. It now renders truthfully as paper."""
    _patch_prices(monkeypatch)
    node = _paper_node_with_position(ticker='PHANTOM', state='live')
    assert signals_db.get_open_position('PHANTOM') is None, (
        "test premise: no REAL position exists for this live node")

    rows = signals_notify.build_reference_table([node])
    assert rows[0]['Held'] is True
    text = signals_notify._ticker_block(rows[0])[0]['text']['text']
    assert '📄PAPER' in text, f"phantom must not read as a real position: {text!r}"


def test_real_position_wins_over_a_colliding_paper_row(env, monkeypatch):
    """The merge's existing precedence (real overwrites paper on a wl_id
    collision) must be preserved -- real state must never be shadowed."""
    _patch_prices(monkeypatch)
    node = _paper_node_with_position(ticker='COLLIDE', state='live', shares=100)
    assert signals_db.open_position(
        node, signal_price=105.0, signal_time='2026-07-27 14:30:00',
        entry_price=50.0, entry_time='2026-07-28 12:00:00', shares=7)

    rows = signals_notify.build_reference_table([node])
    assert rows[0]['_pos']['origin'] == 'live'
    assert rows[0]['_pos']['shares'] == 7, "the REAL row must win the collision"
    text = signals_notify._ticker_block(rows[0])[0]['text']['text']
    assert '📄PAPER' not in text


def _stub_signal(monkeypatch):
    monkeypatch.setattr(signals_compute, '_current_price', lambda ticker: (100.0, None))
    monkeypatch.setattr(signals_compute, 'compute_buy_signal', lambda node: {
        'ticker': node['ticker'], 'current_price': 100.0, 'z_score': -2.5,
        'lower_band': 95.0, 'prev_close': 98.0, 'last_daily_bar': '2026-08-14',
    })


def test_reference_row_arm_pct_reads_take_profit_for_trailing_exit_nodes(env, monkeypatch):
    """Real 2026-08-14 bug: the report set 'Arm%' from the raw arm_sell_pct
    column, which is NULL for every TrailingExitZScoreBreakout node (their arm
    value lives in take_profit -- signals_db._tp_or_arm_pct). UDOW (ira,
    take_profit=1) rendered as `arm ?` in the live Morning Report."""
    _stub_signal(monkeypatch)
    signals_db.add_node(
        ticker='ARMTEST', strategy='TrailingExitZScoreBreakout', version='v5', window=10,
        take_profit=1.0, stop_loss=2, max_hold_hours=70, state='paper', account='roth',
        trail_pct=3.0,
    )
    node = signals_db.get_watch_list_node(ticker='ARMTEST')
    assert node['arm_sell_pct'] is None and node['take_profit'] == 1.0

    rows = signals_notify.build_reference_table([node])
    assert rows[0]['Arm%'] == 1.0
    rendered = ' '.join(b.get('text', {}).get('text', '')
                        for b in signals_notify._ticker_block(rows[0]) if isinstance(b, dict))
    assert 'arm `1%`' in rendered and 'arm `?`' not in rendered


def test_held_trailing_exit_position_renders_its_real_arm_level(env, monkeypatch):
    """Same overload on the open-position branch, which also feeds the row's
    'Next Action' string and _position_trigger_summary's nightly-plan line."""
    _stub_signal(monkeypatch)
    signals_db.add_node(
        ticker='ARMHELD', strategy='TrailingExitZScoreBreakout', version='v5', window=10,
        take_profit=1.0, stop_loss=2, max_hold_hours=70, state='paper', account='roth',
        trail_pct=3.0,
    )
    node = signals_db.get_watch_list_node(ticker='ARMHELD')
    assert signals_db.open_position(
        node, signal_price=105.0, signal_time='2026-08-13 14:30:00',
        entry_price=100.0, entry_time='2026-08-14 11:00:00', shares=10, paper=True,
    )
    pos = signals_db.get_open_position('ARMHELD', paper=True)

    rows = signals_notify.build_reference_table([node])
    assert rows[0]['Arm%'] == 1.0
    assert rows[0]['Next Action'] == 'Arm 1%'
    assert 'arms @ $101.00 (1%)' in signals_notify._position_trigger_summary(pos)


def test_reference_row_arm_pct_still_reads_arm_sell_pct_for_trailing_both_nodes(env, monkeypatch):
    """Regression guard for the case that already worked."""
    _stub_signal(monkeypatch)
    signals_db.add_node(
        ticker='ARMBOTH', strategy='TrailingBothZScoreBreakout', version='v5', window=10,
        take_profit=30.0, stop_loss=2, max_hold_hours=70, state='paper', account='roth',
        trail_buy_pct=3.0, trail_pct=1.0,
    )
    node = signals_db.get_watch_list_node(ticker='ARMBOTH')
    assert node['take_profit'] is None and node['arm_sell_pct'] == 30.0

    rows = signals_notify.build_reference_table([node])
    assert rows[0]['Arm%'] == 30.0
