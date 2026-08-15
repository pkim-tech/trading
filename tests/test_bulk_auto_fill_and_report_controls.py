"""Tests for the 2026-08-14 "no manual anything" staging change, three parts:

1. schwab_safety.bulk_enable_auto_fill_detection / resolve_auto_fill_detection_targets
   -- the staged bulk enabler. apply=False must write NOTHING; apply=True must
   flip BOTH flag files (ticker-level AND node-level, since the real gate is an
   AND of the two) for exactly the real target nodes and nobody else.
2. signals_notify._ticker_block -- the "Enable Auto-Fill Detection" button is
   gone from new reports; "Disable" still renders once a node is enabled.
3. The node-scoped 🛑 Stop / ▶️ Start buttons (+ confirm dialogs on those and on
   the global Stop/Start Engine buttons), and that tapping them really calls
   schwab_safety.pause_node_automation/resume_node_automation.

Handler tests call the handler functions directly rather than going through
Bolt's dispatch, matching tests/test_signals_handlers.py's established pattern
(the handlers only exist as module attributes when cfg.SOCKET_MODE was True at
import time -- skip otherwise). Every schwab_safety state path is monkeypatched
into tmp_path: no test here may ever touch the real
cache/live/schwab_auto_fill_detection.json.
"""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import signals_handlers
import signals_notify
import schwab_safety


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH',
                        tmp_path / "schwab_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTO_FILL_DETECTION_PATH',
                        tmp_path / "schwab_node_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTOMATION_PATH',
                        tmp_path / "schwab_node_automation.json")
    monkeypatch.setattr(schwab_safety, 'TICKER_AUTOMATION_PATH',
                        tmp_path / "schwab_ticker_automation.json")
    monkeypatch.setattr(schwab_safety, 'KILL_SWITCH_PATH',
                        tmp_path / "schwab_kill_switch.json")
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    signals_db.ensure_tables()
    schwab_safety.reload_accounts()
    yield
    os.unlink(tmp_db.name)


def _add_live_node(ticker, account, notional, state='live', version='v5'):
    signals_db.add_node(ticker, 'TrailingBothZScoreBreakout', version, window=20,
                        take_profit=10, stop_loss=5, max_hold_hours=56,
                        state=state, account=account, starting_notional=notional)
    rows = [n for n in signals_db.get_watchlist()
            if n['ticker'] == ticker and n['account'] == account and n['version'] == version]
    assert len(rows) == 1, f"expected exactly one fresh node for {ticker}/{account}/{version}"
    return rows[0]


# ---------------------------------------------------------------- item 1


def _seed_mixed_nodes():
    """ira/soxl_ira are trading_enabled=1 in the seeded accounts table;
    roth/brokerage are trading_enabled=0. So only the first three below are
    real targets, and only two of those clear a $1,000 floor."""
    a = _add_live_node('TEST_BULK_A', 'ira', 10_000)
    b = _add_live_node('TEST_BULK_B', 'soxl_ira', 2_500)
    c = _add_live_node('TEST_BULK_C', 'ira', 500)
    d = _add_live_node('TEST_BULK_D', 'roth', 10_000)          # account not trading_enabled
    e = _add_live_node('TEST_BULK_E', 'ira', 10_000, state='paper')  # not live
    return a, b, c, d, e


def test_resolver_excludes_non_live_and_non_trading_enabled_accounts(env):
    a, b, c, d, e = _seed_mixed_nodes()
    ids = {n['id'] for n in schwab_safety.resolve_auto_fill_detection_targets()}
    assert ids == {a['id'], b['id'], c['id']}


def test_resolver_min_notional_floor(env):
    a, b, c, _, _ = _seed_mixed_nodes()
    ids = {n['id'] for n in schwab_safety.resolve_auto_fill_detection_targets(min_notional=1_000)}
    assert ids == {a['id'], b['id']}


def test_node_ids_and_tickers_are_filters_never_an_escape_hatch(env):
    """Naming a node that isn't in the real target set must NOT add it -- the
    args intersect, they never widen."""
    a, _, _, d, e = _seed_mixed_nodes()
    by_id = schwab_safety.resolve_auto_fill_detection_targets(node_ids=[a['id'], d['id'], e['id']])
    assert {n['id'] for n in by_id} == {a['id']}

    by_ticker = schwab_safety.resolve_auto_fill_detection_targets(
        tickers=['TEST_BULK_A', 'TEST_BULK_D', 'TEST_BULK_E'])
    assert {n['id'] for n in by_ticker} == {a['id']}


def test_apply_false_previews_without_writing_anything(env):
    a, b, c, _, _ = _seed_mixed_nodes()
    result = schwab_safety.bulk_enable_auto_fill_detection()

    assert result['apply'] is False
    assert {r['id'] for r in result['changed']} == {a['id'], b['id'], c['id']}
    assert result['already_enabled'] == []
    # The whole staging gate: no state file may exist at all afterwards.
    assert not schwab_safety.AUTO_FILL_DETECTION_PATH.exists()
    assert not schwab_safety.NODE_AUTO_FILL_DETECTION_PATH.exists()
    for node in (a, b, c):
        assert schwab_safety.auto_fill_detection_enabled(node['ticker']) is False
        assert schwab_safety.node_auto_fill_detection_enabled(node['id']) is False
    # And the preview text renders without blowing up.
    assert 'would enable' in schwab_safety.format_bulk_enable_auto_fill_detection(result)


def test_apply_true_flips_both_flags_for_targets_only(env):
    a, b, c, d, e = _seed_mixed_nodes()
    result = schwab_safety.bulk_enable_auto_fill_detection(min_notional=1_000, apply=True)

    assert result['apply'] is True
    assert {r['id'] for r in result['changed']} == {a['id'], b['id']}

    for node in (a, b):
        assert schwab_safety.auto_fill_detection_enabled(node['ticker']) is True
        assert schwab_safety.node_auto_fill_detection_enabled(node['id']) is True

    # Below the floor, wrong account, wrong state -- all untouched on BOTH axes.
    for node in (c, d, e):
        assert schwab_safety.auto_fill_detection_enabled(node['ticker']) is False
        assert schwab_safety.node_auto_fill_detection_enabled(node['id']) is False

    # Raw file contents: only the two intended node ids present and True.
    node_state = json.loads(schwab_safety.NODE_AUTO_FILL_DETECTION_PATH.read_text())
    assert {int(k) for k, v in node_state.items() if v} == {a['id'], b['id']}


def test_min_notional_respects_starting_notional_override(env):
    """The override is what actually sizes real orders once a node has closed
    a real trade, so a floor keyed to the raw column alone misjudges any node
    deliberately resized through it -- in both directions."""
    small = _add_live_node('TEST_BULK_OV1', 'ira', 500)
    big = _add_live_node('TEST_BULK_OV2', 'ira', 50_000)
    signals_db.set_starting_notional_override(small['id'], 20_000)   # small -> above floor
    signals_db.set_starting_notional_override(big['id'], 100)        # big   -> below floor

    ids = {n['id'] for n in schwab_safety.resolve_auto_fill_detection_targets(min_notional=1_000)}
    assert small['id'] in ids
    assert big['id'] not in ids


def test_bulk_skips_nodes_a_human_deliberately_disabled(env):
    a, b, c, _, _ = _seed_mixed_nodes()
    # Simulate the emergency-override path: enable, then a human hits Disable.
    schwab_safety.bulk_enable_auto_fill_detection(apply=True)
    schwab_safety.disable_node_auto_fill_detection(b['id'])

    result = schwab_safety.bulk_enable_auto_fill_detection(apply=True)
    assert {r['id'] for r in result['explicitly_disabled']} == {b['id']}
    assert result['changed'] == []
    # The deliberate Disable survived the sweep.
    assert schwab_safety.node_auto_fill_detection_enabled(b['id']) is False
    assert 'SKIPPED' in schwab_safety.format_bulk_enable_auto_fill_detection(result)


def test_force_re_enables_a_deliberately_disabled_node(env):
    _, b, _, _, _ = _seed_mixed_nodes()
    schwab_safety.bulk_enable_auto_fill_detection(apply=True)
    schwab_safety.disable_node_auto_fill_detection(b['id'])

    result = schwab_safety.bulk_enable_auto_fill_detection(apply=True, force=True)
    assert {r['id'] for r in result['changed']} == {b['id']}
    assert result['explicitly_disabled'] == []
    assert schwab_safety.node_auto_fill_detection_enabled(b['id']) is True


def test_never_set_is_not_treated_as_deliberately_disabled(env):
    """Absence is not a decision -- a fresh node must still be enabled."""
    a, _, _, _, _ = _seed_mixed_nodes()
    result = schwab_safety.bulk_enable_auto_fill_detection(node_ids=[a['id']], apply=True)
    assert {r['id'] for r in result['changed']} == {a['id']}
    assert result['explicitly_disabled'] == []


def test_sibling_node_on_same_ticker_stays_gated_by_its_own_flag(env):
    """The docstring's ticker-sharing caveat, asserted: enabling node A flips
    the SHARED ticker-level gate, but sibling B must stay off via its own
    node-level flag."""
    a = _add_live_node('TEST_SHARED', 'ira', 10_000)
    b = _add_live_node('TEST_SHARED', 'soxl_ira', 100, version='v4')

    schwab_safety.bulk_enable_auto_fill_detection(min_notional=1_000, apply=True)

    assert schwab_safety.auto_fill_detection_enabled('TEST_SHARED') is True   # shared, now on
    assert schwab_safety.node_auto_fill_detection_enabled(a['id']) is True
    assert schwab_safety.node_auto_fill_detection_enabled(b['id']) is False   # still gated


def test_apply_true_is_idempotent_and_reports_already_enabled(env):
    a, b, c, _, _ = _seed_mixed_nodes()
    schwab_safety.bulk_enable_auto_fill_detection(apply=True)
    second = schwab_safety.bulk_enable_auto_fill_detection(apply=True)

    assert second['changed'] == []
    assert {r['id'] for r in second['already_enabled']} == {a['id'], b['id'], c['id']}


# ---------------------------------------------------------------- render helpers


def _row(ticker, node, held=False, pos=None):
    return {
        'Ticker': ticker, 'Version': 'v5', 'Account': node.get('account'),
        'Next Action': 'WAIT', 'Phase': '', 'Now': 10.0, 'Next Trigger $': 9.5,
        'Proximity': 5.0, 'Held': held, '_node': node, '_pos': pos,
        'Overnight %': 0.1, 'TrailBuy%': 1, 'Arm%': 20, 'TrailSell%': 7,
        'Last Sale $': None, 'Z Trigger': -2.0, 'Trigger Label': 'buy',
        'Z': -1.5, 'State': 'live', 'PnL %': 1.0, 'SL $': 9.0, 'Hold': '2h',
    }


def _action_ids(blocks):
    return [e['action_id'] for b in blocks if b['type'] == 'actions' for e in b['elements']]


def _element(blocks, action_id):
    for b in blocks:
        if b['type'] != 'actions':
            continue
        for e in b['elements']:
            if e['action_id'] == action_id:
                return e
    return None


@pytest.fixture
def interactive(monkeypatch):
    monkeypatch.setattr(signals_notify.cfg, 'INTERACTIVE', True)


# ---------------------------------------------------------------- item 2


def test_enable_auto_fill_button_is_never_rendered(env, interactive, monkeypatch):
    node = _add_live_node('TEST_RENDER_A', 'ira', 10_000)
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {'TEST_RENDER_A'})

    blocks = signals_notify._ticker_block(_row('TEST_RENDER_A', node))
    ids = _action_ids(blocks)
    assert 'enable_auto_fill_detection' not in ids
    # Not enabled yet, so no Disable button either -- the row just has no
    # fill-detection control at all.
    assert 'disable_auto_fill_detection' not in ids


def test_disable_auto_fill_button_renders_once_node_is_enabled(env, interactive, monkeypatch):
    node = _add_live_node('TEST_RENDER_B', 'ira', 10_000)
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {'TEST_RENDER_B'})
    schwab_safety.bulk_enable_auto_fill_detection(node_ids=[node['id']], apply=True)

    blocks = signals_notify._ticker_block(_row('TEST_RENDER_B', node))
    ids = _action_ids(blocks)
    assert 'disable_auto_fill_detection' in ids
    assert 'enable_auto_fill_detection' not in ids


# ---------------------------------------------------------------- item 3


def test_manual_open_button_no_longer_renders(env, interactive):
    """Manually Open is superseded by Start (a live node with no position has
    nothing to manually open into -- the action was always "wait for a real
    signal" anyway). Manually Close is NOT superseded -- see
    test_held_real_position_still_gets_manual_close_button below -- a Stop
    press doesn't touch an already-open position, so the correction path a
    misclick needs still has to exist independently."""
    node = _add_live_node('TEST_RENDER_C', 'ira', 10_000)
    flat = signals_notify._ticker_block(_row('TEST_RENDER_C', node))
    assert 'manual_open' not in _action_ids(flat)


def test_held_real_position_still_gets_manual_close_button(env, interactive):
    node = _add_live_node('TEST_RENDER_C2', 'ira', 10_000)
    held = signals_notify._ticker_block(_row(
        'TEST_RENDER_C2', node, held=True,
        pos={'id': 1, 'entry_price': 10.0, 'shares': 5, 'trail_state': {}, 'origin': 'live'}))
    assert 'manual_close' in _action_ids(held)
    assert 'manual_open' not in _action_ids(held)


def test_stop_button_renders_when_node_running(env, interactive):
    node = _add_live_node('TEST_RENDER_D', 'ira', 10_000)
    blocks = signals_notify._ticker_block(_row('TEST_RENDER_D', node))

    assert 'stop_node_automation' in _action_ids(blocks)
    assert 'start_node_automation' not in _action_ids(blocks)
    btn = _element(blocks, 'stop_node_automation')
    assert json.loads(btn['value']) == {'ticker': 'TEST_RENDER_D', 'wl_id': node['id']}
    assert btn['confirm']['title']['type'] == 'plain_text'
    assert btn['confirm']['confirm']['type'] == 'plain_text'
    assert btn['confirm']['deny']['type'] == 'plain_text'
    assert btn['confirm']['style'] == 'danger'


def test_start_button_renders_when_node_already_paused(env, interactive):
    """Toggle must reflect REAL current state -- a paused node showing 'Stop'
    would silently no-op and still read as 'I stopped it'."""
    node = _add_live_node('TEST_RENDER_E', 'ira', 10_000)
    schwab_safety.pause_node_automation(node['id'], reason='test')

    blocks = signals_notify._ticker_block(_row('TEST_RENDER_E', node))
    assert 'start_node_automation' in _action_ids(blocks)
    assert 'stop_node_automation' not in _action_ids(blocks)
    assert _element(blocks, 'start_node_automation')['confirm']['title']['type'] == 'plain_text'


def test_stop_confirm_discloses_that_sells_are_blocked_too(env, interactive):
    """A node pause also blocks the automated exit SELL (schwab_safety.check_order's
    gate sits above the BUY/SELL split; signals_notify._attempt_automated_sell /
    _attempt_automated_exit_sell both early-return). The dialog must say so --
    'no new orders' alone reads as 'entries stop, protection stays'."""
    node = _add_live_node('TEST_RENDER_SELL', 'ira', 10_000)
    blocks = signals_notify._ticker_block(_row('TEST_RENDER_SELL', node))
    text = _element(blocks, 'stop_node_automation')['confirm']['text']['text']
    assert 'SELL' in text
    assert 'exit' in text.lower()


def test_stop_button_does_not_claim_running_when_kill_switch_engaged(env, interactive):
    node = _add_live_node('TEST_RENDER_KS', 'ira', 10_000)
    schwab_safety.engage_kill_switch(reason='test')

    btn = _element(signals_notify._ticker_block(_row('TEST_RENDER_KS', node)),
                   'stop_node_automation')
    assert 'already halted' in btn['text']['text']
    assert 'engine stopped' in btn['text']['text']


def test_stop_button_does_not_claim_running_when_ticker_paused(env, interactive):
    node = _add_live_node('TEST_RENDER_TP', 'ira', 10_000)
    schwab_safety.pause_ticker_automation('TEST_RENDER_TP', reason='test')

    btn = _element(signals_notify._ticker_block(_row('TEST_RENDER_TP', node)),
                   'stop_node_automation')
    assert 'already halted' in btn['text']['text']
    assert 'ticker paused' in btn['text']['text']


def test_start_confirm_warns_when_another_layer_still_blocks(env, interactive):
    node = _add_live_node('TEST_RENDER_SB', 'ira', 10_000)
    schwab_safety.pause_node_automation(node['id'], reason='test')
    schwab_safety.engage_kill_switch(reason='test')

    btn = _element(signals_notify._ticker_block(_row('TEST_RENDER_SB', node)),
                   'start_node_automation')
    assert 'still blocked' in btn['confirm']['text']['text']


def test_clean_running_node_gets_an_unqualified_stop_label(env, interactive):
    node = _add_live_node('TEST_RENDER_CLEAN', 'ira', 10_000)
    btn = _element(signals_notify._ticker_block(_row('TEST_RENDER_CLEAN', node)),
                   'stop_node_automation')
    assert btn['text']['text'] == '🛑 Stop TEST_RENDER_CLEAN'


@pytest.mark.parametrize('state,version', [('paper', 'v5'), ('dry_run', 'v5-canary')])
def test_no_stop_button_for_paper_or_canary_rows(env, interactive, state, version):
    """_ticker_block is also rendered by _send_window_alert against a fully
    unfiltered watchlist, so these rows really do reach a user."""
    node = _add_live_node(f'TEST_NB_{state}', 'ira', 10_000, state=state, version=version)
    ids = _action_ids(signals_notify._ticker_block(_row(f'TEST_NB_{state}', node)))
    assert 'stop_node_automation' not in ids
    assert 'start_node_automation' not in ids


def test_paper_trading_honors_node_automation_pause(env, monkeypatch):
    """The button is hidden for paper rows, but the gate must be real, not
    cosmetic -- a node paused via script/console must stop simulating too."""
    import paper_trading
    node = _add_live_node('TEST_PAPER_GATE', 'ira', 10_000, state='paper')
    monkeypatch.setattr(paper_trading.db, '_is_trailing_buy', lambda n: True)
    sig = {'ticker': 'TEST_PAPER_GATE', 'current_price': 10.0, 'z_score': -2.5,
           'last_bar': datetime(2026, 8, 14, 10, 30)}

    schwab_safety.pause_node_automation(node['id'], reason='test')
    paper_trading.start_paper_buy(node, sig)
    assert signals_db.get_paper_pending_buy(node['id']) is None

    schwab_safety.resume_node_automation(node['id'])
    paper_trading.start_paper_buy(node, sig)
    assert signals_db.get_paper_pending_buy(node['id']) is not None


def test_paper_drought_entry_honors_node_automation_pause(env, monkeypatch):
    """check_paper_drought_entry is a SECOND paper entry path, wired into the
    poll loop independently of start_paper_buy -- gating only the latter would
    still let a stopped node open drought-overlay positions."""
    import paper_trading
    node = _add_live_node('TEST_DROUGHT_GATE', 'ira', 10_000, state='paper')
    opened = []
    monkeypatch.setattr(paper_trading, 'evaluate_drought_entry',
                        lambda n, paper=True: {'price': 10.0, 'shares': 5, 'confirm_days': 3,
                                                'vol_gate': 1.0, 'vol_pctile': 0.5,
                                                'gap_start': '2026-08-01'})
    monkeypatch.setattr(paper_trading.db, 'open_drought_overlay_position',
                        lambda *a, **k: opened.append(a))

    schwab_safety.pause_node_automation(node['id'], reason='test')
    paper_trading.check_paper_drought_entry(node)
    assert opened == []

    schwab_safety.resume_node_automation(node['id'])
    paper_trading.check_paper_drought_entry(node)
    assert len(opened) == 1


def test_bulk_refuses_to_run_on_unreadable_state_file(env):
    """Failing open here would make every explicit human Disable invisible and
    let apply=True silently re-enable all of them."""
    _seed_mixed_nodes()
    schwab_safety.NODE_AUTO_FILL_DETECTION_PATH.write_text("{not valid json")

    with pytest.raises(RuntimeError, match="refusing to run"):
        schwab_safety.bulk_enable_auto_fill_detection(apply=True)
    # ...and it refuses on a preview too, rather than printing a plan derived
    # from state it couldn't read.
    with pytest.raises(RuntimeError, match="refusing to run"):
        schwab_safety.bulk_enable_auto_fill_detection()


def test_missing_state_file_is_not_treated_as_corrupt(env):
    """Absence is genuinely 'no decisions recorded' -- must still work."""
    a, _, _, _, _ = _seed_mixed_nodes()
    assert not schwab_safety.NODE_AUTO_FILL_DETECTION_PATH.exists()
    result = schwab_safety.bulk_enable_auto_fill_detection(node_ids=[a['id']], apply=True)
    assert {r['id'] for r in result['changed']} == {a['id']}


def test_stop_confirm_text_fits_without_truncation(env, interactive):
    """The SELL warning must not be the clause that gets clipped at the 300
    char cap -- check with a wide node id."""
    node = _add_live_node('TEST_RENDER_LONG', 'ira', 10_000)
    blocks = signals_notify._ticker_block(_row('TEST_RENDER_LONG', node))
    text = _element(blocks, 'stop_node_automation')['confirm']['text']['text']
    assert len(text) <= 300
    assert '…' not in text
    assert 'NOT be placed' in text


def test_confirm_dialog_marks_truncation_visibly(env):
    obj = signals_notify._confirm_dialog("t", "x" * 400, "ok")
    assert len(obj['text']['text']) == 300
    assert obj['text']['text'].endswith('…')


def test_preview_reports_effective_notional_not_raw(env):
    node = _add_live_node('TEST_PREVIEW_OV', 'ira', 500)
    signals_db.set_starting_notional_override(node['id'], 25_000)
    result = schwab_safety.bulk_enable_auto_fill_detection(
        min_notional=1_000, node_ids=[node['id']])
    assert result['changed'][0]['starting_notional'] == 25_000


def test_engine_buttons_carry_confirm_dialogs(env, interactive, monkeypatch):
    """The global kill switch had no confirmation at all before 2026-08-14."""
    posted = []

    def _capture(text, fixed_blocks, units, **kw):
        posted.append(fixed_blocks)
        return 'C1', '1.0'

    monkeypatch.setattr(signals_notify, '_post_chunked', _capture)

    signals_notify.send_reference_report([])
    blocks = posted[-1]
    stop = _element(blocks, 'stop_engine')
    assert stop is not None and 'confirm' in stop
    assert stop['confirm']['style'] == 'danger'

    schwab_safety.engage_kill_switch(reason='test')
    posted.clear()
    signals_notify.send_reference_report([])
    start = _element(posted[-1], 'start_engine')
    assert start is not None and 'confirm' in start


def _ack():
    return None


class _FakeClient:
    def chat_update(self, **kw):
        pass


@pytest.fixture
def handlers(env, monkeypatch):
    if not hasattr(signals_handlers, 'handle_stop_node_automation'):
        pytest.skip("signals_handlers handlers only defined when cfg.SOCKET_MODE was True at import time")
    # send_reference_report at the end of each handler would do real work
    # (price fetches); the handler's job under test is the safety call.
    monkeypatch.setattr(signals_handlers, 'send_reference_report', lambda *a, **k: None)
    return signals_handlers


def test_stop_handler_really_pauses_the_node(handlers, env):
    node = _add_live_node('TEST_HANDLER_A', 'ira', 10_000)
    body = {'actions': [{'value': json.dumps({'ticker': 'TEST_HANDLER_A', 'wl_id': node['id']})}],
            'user': {'username': 'tester'}}

    assert schwab_safety.node_automation_enabled(node['id']) is True
    handlers.handle_stop_node_automation(_ack, body, _FakeClient())
    assert schwab_safety.node_automation_enabled(node['id']) is False


def test_start_handler_does_not_claim_success_while_still_blocked(handlers, env, monkeypatch):
    node = _add_live_node('TEST_HANDLER_BLK', 'ira', 10_000)
    schwab_safety.pause_node_automation(node['id'], reason='test')
    schwab_safety.engage_kill_switch(reason='test')
    posted = []
    monkeypatch.setattr(signals_handlers, '_post_message', lambda *a, **k: posted.append(a[0]))
    body = {'actions': [{'value': json.dumps({'ticker': 'TEST_HANDLER_BLK', 'wl_id': node['id']})}],
            'user': {'username': 'tester'}}

    handlers.handle_start_node_automation(_ack, body, _FakeClient())
    assert schwab_safety.node_automation_enabled(node['id']) is True   # node flag did resume
    assert 'STILL BLOCKED' in posted[0]
    assert 'engine stopped' in posted[0]


def test_stop_start_handlers_log_a_coverage_event(handlers, env):
    node = _add_live_node('TEST_HANDLER_EV', 'ira', 10_000)
    body = {'actions': [{'value': json.dumps({'ticker': 'TEST_HANDLER_EV', 'wl_id': node['id']})}],
            'user': {'username': 'tester'}}

    handlers.handle_stop_node_automation(_ack, body, _FakeClient())
    handlers.handle_start_node_automation(_ack, body, _FakeClient())

    results = [e['result'] for e in
               signals_db.get_coverage_events(scenario_key='node_automation_pause_button')]
    assert 'paused_by_user' in results
    assert 'resumed_by_user' in results
    # Must NOT borrow the order-blocking guard's key -- a button tap is not
    # evidence check_order blocks anything, and compute_status would flip that
    # row to verified-live on the first tap.
    assert signals_db.get_coverage_events(scenario_key='node_level_automation_pause') == []


def test_ticker_level_disable_does_not_mis_skip_a_sibling_node(env):
    """The ticker-level flag is shared; only the node-level flag records a
    per-node human decision."""
    a = _add_live_node('TEST_SIB_DIS', 'ira', 10_000)
    b = _add_live_node('TEST_SIB_DIS', 'soxl_ira', 5_000, version='v4')
    schwab_safety.disable_auto_fill_detection('TEST_SIB_DIS')   # ticker-wide, not per-node

    result = schwab_safety.bulk_enable_auto_fill_detection(min_notional=1_000, apply=True)
    assert result['explicitly_disabled'] == []
    assert {r['id'] for r in result['changed']} >= {a['id'], b['id']}


def test_start_handler_really_resumes_the_node(handlers, env):
    node = _add_live_node('TEST_HANDLER_B', 'ira', 10_000)
    schwab_safety.pause_node_automation(node['id'], reason='test')
    body = {'actions': [{'value': json.dumps({'ticker': 'TEST_HANDLER_B', 'wl_id': node['id']})}],
            'user': {'username': 'tester'}}

    handlers.handle_start_node_automation(_ack, body, _FakeClient())
    assert schwab_safety.node_automation_enabled(node['id']) is True


def test_stop_handler_scope_is_one_node_not_the_ticker(handlers, env):
    """Sibling node on the same ticker in another account must keep running."""
    a = _add_live_node('TEST_HANDLER_C', 'ira', 10_000)
    b = _add_live_node('TEST_HANDLER_C', 'soxl_ira', 500, version='v4')
    assert a['id'] != b['id']
    body = {'actions': [{'value': json.dumps({'ticker': 'TEST_HANDLER_C', 'wl_id': a['id']})}],
            'user': {'username': 'tester'}}

    handlers.handle_stop_node_automation(_ack, body, _FakeClient())
    assert schwab_safety.node_automation_enabled(a['id']) is False
    assert schwab_safety.node_automation_enabled(b['id']) is True
    assert schwab_safety.ticker_automation_enabled('TEST_HANDLER_C') is True


def test_stale_stop_button_payload_is_guarded_not_guessed(handlers, env, monkeypatch):
    """A pre-2026-08-14 report in scrollback carries a bare ticker string. It
    must not be resolved to some node by guessing."""
    node = _add_live_node('TEST_HANDLER_D', 'ira', 10_000)
    posted = []
    monkeypatch.setattr(signals_handlers, '_post_message', lambda *a, **k: posted.append(a))
    body = {'actions': [{'value': 'TEST_HANDLER_D'}], 'user': {'username': 'tester'}}

    handlers.handle_stop_node_automation(_ack, body, _FakeClient())
    assert schwab_safety.node_automation_enabled(node['id']) is True
    assert posted and 'Stale' in posted[0][0]


def test_manual_open_close_handlers_stay_registered(handlers):
    """Unrendered, but deliberately still wired for old scrollback + as a real
    correction path -- deleting them would break clicks on old reports."""
    for name in ('handle_manual_open', 'handle_manual_close',
                 'handle_manual_open_price', 'handle_manual_close_price',
                 'handle_enable_auto_fill_detection'):
        assert hasattr(signals_handlers, name), name
