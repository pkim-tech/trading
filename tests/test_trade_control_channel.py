"""signals_trade_control.py -- the dedicated trade-control channel: one
self-updating Slack message per real-live node, edited in place through the
whole lifecycle.

Built 2026-08-17 after a real failure: a pending "Trailing Buy Order Placed"
confirmation for SOXS was unfindable in normal channel scrollback during a
Schwab API outage. These tests pin what makes the feature trustworthy:
  * the scope is DERIVED (has_capital_at_stake over db.get_live_nodes, i.e.
    every watchlist), never a hardcoded ticker list;
  * every outstanding control action is reachable from the card, with button
    payloads byte-identical to the main channel's own builders;
  * a routinely-resting automated exit is NOT framed as "action needed" (the
    2026-08-02 reminder-suppression rule, restated for this surface);
  * the sync is inert until configured, and spends no Slack call on an
    unchanged card -- including across a wall-clock minute boundary, which the
    first version of the content hash got wrong.
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from slack_sdk.errors import SlackApiError

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config as cfg
import signals_db as db
import signals_notify
import signals_trade_control as tc
import schwab_safety

TICKER = 'TEST_TRADE_CONTROL'
OTHER = 'TEST_TC_SMALL'
CHANNEL = 'C_TEST_TRADE_CONTROL'


class FakeSlackClient:
    """Records chat_postMessage/chat_update instead of calling Slack. Mirrors
    the real client's return shape -- chat_postMessage always returns the
    resolved channel ID, which is what a later chat_update needs (and the
    reason the tracking row stores the configured string separately)."""

    def __init__(self, resolved_channel=None):
        self.posted = []
        self.updated = []
        self._n = 0
        self.fail_on = set()
        self.resolved_channel = resolved_channel

    def chat_postMessage(self, channel, text, blocks=None, **kw):
        if text and any(t in text for t in self.fail_on):
            raise RuntimeError("simulated slack failure")
        self._n += 1
        ts = f"1700000000.{self._n:06d}"
        resolved = self.resolved_channel or channel
        self.posted.append({'channel': resolved, 'ts': ts, 'text': text, 'blocks': blocks})
        return {'channel': resolved, 'ts': ts}

    def chat_update(self, channel, ts, text, blocks=None, **kw):
        if text and any(t in text for t in self.fail_on):
            raise RuntimeError("simulated slack failure")
        self.updated.append({'channel': channel, 'ts': ts, 'text': text, 'blocks': blocks})
        return {'channel': channel, 'ts': ts, 'ok': True}


@pytest.fixture
def env(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(cfg, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(cfg, 'SLACK_TRADE_CONTROL_CHANNEL', CHANNEL)
    monkeypatch.setattr(cfg, 'SOCKET_MODE', True)
    monkeypatch.setattr(cfg, 'SIM_MODE', False)
    monkeypatch.setattr(cfg, 'INTERACTIVE', True)

    db.ensure_tables()
    schwab_safety.reload_accounts()
    # soxl_ira is trading_enabled in the seeded accounts table, so a live node
    # there is a genuinely real-order-placing node -- the real precondition
    # has_capital_at_stake checks.
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                stop_loss=5, max_hold_hours=56, state='live', account='soxl_ira',
                trail_buy_pct=1.0, trail_pct=3.0, starting_notional=50_000, fixed_sl_override=1.0)

    client = FakeSlackClient()
    monkeypatch.setattr(tc, '_client', lambda: client)
    tc._FAILURE_LOG_STATE['last_logged_at'] = None

    yield client

    p = Path(tmp_db.name)
    if p.exists():
        p.unlink()
    # ACCOUNTS was reloaded against a DB that no longer exists -- reset the
    # singleton's staleness marker so a later test file in the same worker
    # process doesn't read from a deleted file.
    schwab_safety.ACCOUNTS._loaded = False


def _node(ticker=TICKER):
    return [n for n in db.get_live_nodes() if n['ticker'] == ticker][0]


def _sig(price=100.0):
    return {'ticker': TICKER, 'current_price': price, 'z_score': -2.5,
            'last_bar': datetime(2026, 8, 17, 10, 30)}


def _add_pending(order_placed=False):
    node = _node()
    db.add_pending_buy(node, _sig(), channel=None, ts=None)
    if order_placed:
        db.mark_pending_buy_placed_by_wl_id(node['id'])
    return db.get_pending_buy_by_wl_id(node['id'])


def _fake_position(trail_state=None, **overrides):
    node = _node()
    pos = {
        'id': 4242, 'wl_id': node['id'], 'ticker': TICKER, 'account': 'soxl_ira',
        'entry_price': 100.0, 'shares': 400, 'stop_loss': 5.0, 'fixed_sl': 1.0,
        'trail_sell_pct': 3.0, 'take_profit': 7.0, 'arm_sell_pct': None,
        'strategy': 'TrailingBothZScoreBreakout', 'broker_stop_price': None,
        'is_dry_run_sim': 0, 'trail_state': trail_state or {},
    }
    pos.update(overrides)
    return pos


def _exiting_position(order_id=None, reason='TRAIL', **overrides):
    return _fake_position({'trailing': True, 'peak': 110.0, 'order_placed': True, 'exit_pending': {
        'reason': reason, 'current_price': 106.0, 'target_price': 106.7, 'order_id': order_id}},
        **overrides)


def _actions_of(blocks):
    return [b for b in blocks if b['type'] == 'actions'][0]['elements']


def _action_ids(blocks):
    return [e['action_id'] for b in blocks if b['type'] == 'actions' for e in b['elements']]


# ---------------------------------------------------------------------------
# Scope: derived, never hardcoded, and not limited to the active watchlist
# ---------------------------------------------------------------------------

def test_scope_is_derived_from_capital_at_stake(env):
    db.add_node(OTHER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                stop_loss=1, max_hold_hours=56, state='live', account='soxl_ira',
                trail_buy_pct=1.0, trail_pct=3.0, starting_notional=100, fixed_sl_override=1.0)
    tickers = [n['ticker'] for n in tc.real_live_nodes()]
    assert TICKER in tickers
    assert OTHER not in tickers  # sub-threshold notional


def test_scope_excludes_paper_node(env):
    db.add_node(OTHER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                stop_loss=1, max_hold_hours=56, state='paper', account='soxl_ira',
                trail_buy_pct=1.0, trail_pct=3.0, starting_notional=50_000, fixed_sl_override=1.0)
    assert OTHER not in [n['ticker'] for n in tc.real_live_nodes()]


def test_scope_covers_live_nodes_outside_the_active_watchlist(env):
    """Real live nodes span several watchlists (db.get_live_nodes' docstring) --
    scoping to the active one would silently drop real money, and would
    mass-retire every card the day the active watchlist is superseded."""
    other_wl = db.create_watchlist('tc-secondary')
    db.add_node(OTHER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                stop_loss=1, max_hold_hours=56, state='live', account='soxl_ira',
                trail_buy_pct=1.0, trail_pct=3.0, starting_notional=50_000,
                fixed_sl_override=1.0, watchlist_id=other_wl)
    assert db.get_active_watchlist_id() != other_wl
    assert OTHER not in [n['ticker'] for n in db.get_watchlist()]
    assert OTHER in [n['ticker'] for n in tc.real_live_nodes()]


def test_scope_follows_a_threshold_change_without_code_edits(env, monkeypatch):
    monkeypatch.setattr(cfg, 'CAPITAL_AT_STAKE_THRESHOLD', 1_000_000.0)
    assert tc.real_live_nodes() == []


# ---------------------------------------------------------------------------
# The lifecycle state machine
# ---------------------------------------------------------------------------

def test_control_state_covers_the_full_lifecycle(env):
    node = _node()
    assert tc.control_state(node, None, None) == 'flat'
    assert tc.control_state(node, None, _add_pending()) == 'awaiting_order_placed'
    db.mark_pending_buy_placed_by_wl_id(node['id'])
    assert tc.control_state(node, None, db.get_pending_buy_by_wl_id(node['id'])) == 'awaiting_fill'
    assert tc.control_state(node, _fake_position(), None) == 'held'
    # Armed but no trailing-sell order confirmed yet == a real outstanding tap.
    assert tc.control_state(node, _fake_position({'trailing': True, 'peak': 110.0}),
                            None) == 'awaiting_trail_order'
    assert tc.control_state(node, _fake_position({'trailing': True, 'peak': 110.0, 'order_placed': True}),
                            None) == 'armed'
    assert tc.control_state(node, _exiting_position(), None) == 'awaiting_exit'


def test_open_position_outranks_a_surviving_pending_row_but_keeps_its_button(env):
    """_phase_emoji short-circuits on `pos is not None`; the headline must
    agree with the bubble strip rather than describing a buy that already
    happened. The pending row's own tap is still a real outstanding action, so
    it must not disappear from the card either."""
    node = _node()
    pending = _add_pending(order_placed=True)
    pos = _exiting_position()
    assert tc.control_state(node, pos, pending) == 'awaiting_exit'
    _, blocks, _ = tc.build_card(node, pos, pending)
    ids = _action_ids(blocks)
    assert 'sell_exited' in ids       # the position's action leads
    assert 'trail_buy_filled' in ids  # the pending row's action is still reachable


@pytest.mark.parametrize("state_setup,expected_actions", [
    ('awaiting_order_placed', ['trail_buy_order_placed', 'buy_skipped']),
    ('awaiting_fill', ['trail_buy_filled', 'trail_buy_missed', 'trail_buy_cancelled']),
    ('awaiting_exit', ['sell_exited', 'sell_skipped']),
])
def test_pending_control_actions_are_reachable_from_the_card(env, state_setup, expected_actions):
    """The whole point of the feature: whatever tap is outstanding must be on
    the card itself, not only on a message that scrolled away."""
    node = _node()
    pos = pending = None
    if state_setup == 'awaiting_order_placed':
        pending = _add_pending()
    elif state_setup == 'awaiting_fill':
        pending = _add_pending(order_placed=True)
    else:
        pos = _exiting_position()  # no order_id -> genuinely needs a human

    text, blocks, _ = tc.build_card(node, pos, pending)
    ids = _action_ids(blocks)
    for a in expected_actions:
        assert a in ids
    # Mobile convention: the actionable fact leads the notification line.
    assert text.startswith("🔔 ACTION NEEDED")
    assert TICKER in text


def test_armed_without_a_confirmed_trailing_order_is_actionable(env):
    """An armed position whose trailing-sell was never confirmed placed has
    real capital exposed with no protective order resting -- the outstanding
    tap (`trail_order_placed`, otherwise only reachable from
    signals_notify._trailing_order_blocks) has to be on the card too."""
    pos = _fake_position({'trailing': True, 'peak': 110.0})
    text, blocks, _ = tc.build_card(_node(), pos, None)
    assert 'trail_order_placed' in _action_ids(blocks)
    assert "ACTION NEEDED" in text
    canonical = {e['action_id']: e['value']
                 for e in _actions_of(signals_notify._trailing_order_blocks(pos, 106.0))}
    mine = {e['action_id']: e['value'] for e in _actions_of(blocks)}
    assert json.loads(mine['trail_order_placed']) == json.loads(canonical['trail_order_placed'])


def test_market_buy_node_gets_the_confirmation_that_actually_resolves_it(env):
    """A non-trailing-buy node's real entry confirmation is `buy_executed`
    (opens the fill-price modal). Rendering trailing-buy wording/buttons for it
    would put a button on the card that resolves nothing."""
    db.add_node(OTHER, 'TrailingExitZScoreBreakout', 'test', window=20, take_profit=7,
                stop_loss=1, max_hold_hours=56, state='live', account='soxl_ira',
                trail_pct=3.0, starting_notional=50_000, fixed_sl_override=1.0)
    node = _node(OTHER)
    assert not db._is_trailing_buy(node)
    db.add_pending_buy(node, dict(_sig(), ticker=OTHER), channel=None, ts=None)
    pending = db.get_pending_buy_by_wl_id(node['id'])
    text, blocks, _ = tc.build_card(node, None, pending)
    ids = _action_ids(blocks)
    assert 'buy_executed' in ids and 'trail_buy_order_placed' not in ids
    assert 'market buy' in text


def test_non_actionable_states_carry_no_confirmation_buttons(env):
    node = _node()
    armed = _fake_position({'trailing': True, 'peak': 110.0, 'order_placed': True})
    for pos in (None, _fake_position(), armed):
        _, blocks, _ = tc.build_card(node, pos, None)
        ids = set(_action_ids(blocks))
        assert not ({'trail_buy_filled', 'sell_exited', 'trail_buy_order_placed',
                     'trail_order_placed'} & ids)
        # The node-scoped stop/start control is always present, though -- it is
        # the control actually wanted under time pressure.
        assert any(i in ('stop_node_automation', 'start_node_automation') for i in ids)


# ---------------------------------------------------------------------------
# A routinely-resting automated exit must not read as urgent
# ---------------------------------------------------------------------------

def test_tracked_automated_exit_is_not_framed_as_action_needed(env):
    """check_exit_reminders deliberately stays QUIET for a resting automated
    TRAIL exit (2026-08-02). If the card shouted ACTION NEEDED with a primary
    Exited button here, a tap would close the position locally at a typed
    price while the real order is still resting -- local-flat vs broker-long."""
    pos = _exiting_position(order_id='ORD-1')
    text, blocks, _ = tc.build_card(_node(), pos, None)
    assert "ACTION NEEDED" not in text
    assert "should fill on its own" in text
    exited = [e for e in _actions_of(blocks) if e['action_id'] == 'sell_exited'][0]
    assert 'style' not in exited          # not the expected next step
    assert 'sell_exited' in _action_ids(blocks)  # still reachable as a correction


def test_an_escalated_exit_reminder_overrides_the_tracked_order(env):
    """An order_id proves an order was PLACED, never that it still rests -- a
    REJECTED/CANCELED/already-filled order is indistinguishable by id alone.
    check_exit_reminders escalates at reminder_num >= 3 for exactly that
    reason, so this surface has to escalate on the same threshold instead of
    staying quiet forever on the strength of an id."""
    pos = _exiting_position(order_id='ORD-1')
    pos['trail_state']['exit_pending']['reminder_count'] = tc.EXIT_ESCALATION_REMINDERS
    text, blocks, _ = tc.build_card(_node(), pos, None)
    assert "ACTION NEEDED" in text
    assert tc.action_needed(pos, None) is True


def test_below_the_escalation_threshold_a_tracked_exit_stays_quiet(env):
    pos = _exiting_position(order_id='ORD-1')
    pos['trail_state']['exit_pending']['reminder_count'] = tc.EXIT_ESCALATION_REMINDERS - 1
    text, _, _ = tc.build_card(_node(), pos, None)
    assert "ACTION NEEDED" not in text


def test_sl_exit_with_a_broker_stop_on_file_is_not_action_needed(env):
    pos = _exiting_position(reason='SL', broker_stop_price=99.0)
    text, _, _ = tc.build_card(_node(), pos, None)
    assert "ACTION NEEDED" not in text


def test_exit_with_no_tracked_order_is_action_needed(env):
    pos = _exiting_position(reason='SL')  # no order_id, no broker stop
    text, blocks, _ = tc.build_card(_node(), pos, None)
    assert "ACTION NEEDED" in text
    exited = [e for e in _actions_of(blocks) if e['action_id'] == 'sell_exited'][0]
    assert exited.get('style') == 'primary'


# ---------------------------------------------------------------------------
# Real SL price, not the vestigial swept column
# ---------------------------------------------------------------------------

def test_held_card_shows_the_real_fixed_sl_not_the_swept_stop_loss_column(env):
    """For uses_fixed_sl strategies (both live defaults) `stop_loss` is a
    vestigial grid axis -- the real SL is `fixed_sl`, per signals_compute's own
    resolution. entry 100, fixed_sl=1 -> $99.00, NOT stop_loss=5 -> $95.00."""
    pos = _fake_position()  # stop_loss=5.0, fixed_sl=1.0
    _, blocks, _ = tc.build_card(_node(), pos, None)
    body = blocks[0]['text']['text']
    assert "sl $99.00" in body
    assert "$95.00" not in body


def test_broker_stop_price_wins_when_known(env):
    pos = _fake_position(broker_stop_price=98.25)
    _, blocks, _ = tc.build_card(_node(), pos, None)
    assert "sl $98.25" in blocks[0]['text']['text']


# ---------------------------------------------------------------------------
# Payload parity with the main channel's own builders (drift guard)
# ---------------------------------------------------------------------------

def test_buy_button_payload_matches_the_main_channel_builder(env):
    pending = _add_pending(order_placed=True)
    canonical = {e['action_id']: e['value']
                 for e in _actions_of(signals_notify._pending_buy_blocks(pending, 1))}
    _, blocks, _ = tc.build_card(_node(), None, pending)
    mine = {e['action_id']: e['value'] for e in _actions_of(blocks)}
    for action_id, value in canonical.items():
        assert json.loads(mine[action_id]) == json.loads(value), action_id


def test_exit_button_payload_matches_the_main_channel_builder(env, monkeypatch):
    pos = _exiting_position(order_id='X1')
    exit_pending = pos['trail_state']['exit_pending']
    # _exit_pending_blocks makes a real broker round-trip purely to pick its
    # wording -- exactly why build_card doesn't call it.
    monkeypatch.setattr(signals_notify, '_exit_order_resting', lambda *a, **kw: True)
    canonical = {e['action_id']: e['value']
                 for e in _actions_of(signals_notify._exit_pending_blocks(pos, exit_pending, 1))}
    _, blocks, _ = tc.build_card(_node(), pos, None)
    mine = {e['action_id']: e['value'] for e in _actions_of(blocks)}
    for action_id, value in canonical.items():
        assert json.loads(mine[action_id]) == json.loads(value), action_id


# ---------------------------------------------------------------------------
# The content signature must ignore the rendered clock
# ---------------------------------------------------------------------------

def test_signature_is_stable_across_time_when_nothing_changed(env):
    """The first version hashed the rendered blocks, which contain the card's
    own "_updated HH:MM" line -- so the signature changed every poll cycle,
    every card was rewritten ~288x/day, and the forced-refresh path was
    unreachable. Both review agents reproduced this."""
    node, t0 = _node(), datetime(2026, 8, 17, 10, 0)
    _, _, sig_a = tc.build_card(node, None, None, now=t0)
    _, _, sig_b = tc.build_card(node, None, None, now=t0 + timedelta(minutes=47))
    assert sig_a == sig_b


def test_signature_changes_when_real_state_changes(env):
    node = _node()
    _, _, flat = tc.build_card(node, None, None)
    _, _, pending = tc.build_card(node, None, _add_pending())
    assert flat != pending


# ---------------------------------------------------------------------------
# sync: inert by default, cheap when idle, self-healing
# ---------------------------------------------------------------------------

def test_inert_when_no_channel_is_configured(env, monkeypatch):
    monkeypatch.setattr(cfg, 'SLACK_TRADE_CONTROL_CHANNEL', '')
    result = tc.sync_trade_control_channel()
    assert result['posted'] == [] and result['updated'] == []
    assert env.posted == [] and env.updated == []
    assert db.get_trade_control_messages() == []


def test_inert_in_sim_mode(env, monkeypatch):
    """An ad hoc/sim invocation must never write real message timestamps into
    the tracking table -- they'd then be edited forever as if real."""
    monkeypatch.setattr(cfg, 'SIM_MODE', True)
    tc.sync_trade_control_channel()
    assert env.posted == []
    assert db.get_trade_control_messages() == []


def test_first_sync_posts_one_card_per_node_then_stays_quiet(env):
    t0 = datetime(2026, 8, 17, 10, 0)
    tc.sync_trade_control_channel(now=t0)
    assert len(env.posted) == 1
    tracked = db.get_trade_control_messages()
    assert len(tracked) == 1 and tracked[0]['configured_channel'] == CHANNEL

    env.posted.clear()
    # A later minute, still inside the forced-refresh interval: no Slack call.
    result = tc.sync_trade_control_channel(now=t0 + timedelta(minutes=5))
    assert env.posted == [] and env.updated == []
    assert result['posted'] == [] and result['updated'] == []


def test_state_change_edits_the_same_message_in_place(env):
    tc.sync_trade_control_channel()
    original_ts = db.get_trade_control_messages()[0]['message_ts']

    _add_pending()
    tc.sync_trade_control_channel()

    assert len(env.posted) == 1  # still exactly one message, never a second post
    assert len(env.updated) == 1
    assert env.updated[0]['ts'] == original_ts
    assert "ACTION NEEDED" in env.updated[0]['text']
    assert db.get_trade_control_messages()[0]['message_ts'] == original_ts


def test_forced_refresh_repairs_a_card_a_button_handler_overwrote(env):
    """The shared handlers chat_update whatever message was clicked, so a tap
    here temporarily replaces a card with the handler's own text. A tap that
    changed no state (a stale-click guard) leaves the signature unchanged --
    FORCE_REFRESH_MINUTES is what bounds how long that stays broken."""
    t0 = datetime(2026, 8, 17, 10, 0)
    tc.sync_trade_control_channel(now=t0)
    row = db.get_trade_control_messages()[0]

    # Just inside the interval: still quiet (proves _stale is what fires below,
    # not an incidentally-changing signature).
    tc.sync_trade_control_channel(now=t0 + timedelta(minutes=tc.FORCE_REFRESH_MINUTES - 1))
    assert env.updated == []

    tc.sync_trade_control_channel(now=t0 + timedelta(minutes=tc.FORCE_REFRESH_MINUTES + 1))
    assert len(env.updated) == 1
    assert env.updated[0]['ts'] == row['message_ts']


def test_a_channel_name_does_not_produce_duplicate_cards(env, monkeypatch):
    """chat_postMessage returns the resolved ID even when posted to "#name" --
    comparing that ID against the configured name would look like a channel
    change on every single sync and post a fresh card each cycle, each with
    live real-money buttons."""
    monkeypatch.setattr(cfg, 'SLACK_TRADE_CONTROL_CHANNEL', '#trade-control')
    env.resolved_channel = 'C_RESOLVED_ID'
    tc.sync_trade_control_channel()
    tc.sync_trade_control_channel(now=datetime.now() + timedelta(minutes=90))
    assert len(env.posted) == 1
    assert env.updated and env.updated[0]['channel'] == 'C_RESOLVED_ID'


def test_changing_the_configured_channel_retires_the_old_card(env, monkeypatch):
    """Otherwise the old message is orphaned in the abandoned channel with live
    Filled/Exited buttons on it forever."""
    tc.sync_trade_control_channel()
    old_ts = db.get_trade_control_messages()[0]['message_ts']

    monkeypatch.setattr(cfg, 'SLACK_TRADE_CONTROL_CHANNEL', 'C_NEW_CHANNEL')
    tc.sync_trade_control_channel()
    assert any(u['ts'] == old_ts and "no longer tracked" in u['text'] for u in env.updated)
    assert len(env.posted) == 2
    assert db.get_trade_control_messages()[0]['configured_channel'] == 'C_NEW_CHANNEL'


def test_card_is_retired_when_a_node_leaves_the_real_live_set(env, monkeypatch):
    tc.sync_trade_control_channel()
    assert len(db.get_trade_control_messages()) == 1

    monkeypatch.setattr(cfg, 'CAPITAL_AT_STAKE_THRESHOLD', 1_000_000.0)
    result = tc.sync_trade_control_channel()
    assert result['retired'] == [TICKER]
    assert db.get_trade_control_messages() == []
    assert "no longer tracked" in env.updated[-1]['text']


def test_a_node_with_real_exposure_is_never_retired(env, monkeypatch):
    """has_capital_at_stake can dip below the bar transiently (its
    _last_sale_recovery lookup swallows errors and falls back to the static
    column). Losing the card for a node that still holds a position is the
    exact failure this feature exists to prevent."""
    tc.sync_trade_control_channel()
    _add_pending()
    monkeypatch.setattr(cfg, 'CAPITAL_AT_STAKE_THRESHOLD', 1_000_000.0)
    result = tc.sync_trade_control_channel()
    assert result['retired'] == []
    assert len(db.get_trade_control_messages()) == 1


def test_a_node_with_exposure_below_the_bar_keeps_UPDATING_not_just_surviving(env, monkeypatch):
    """The never-retire-with-exposure guard and the render set have to move
    together. If the node is only exempted from retirement, its card is
    neither retired nor refreshed -- frozen mid-lifecycle with live
    real-money buttons on it forever (both reviewers, rebuttal round)."""
    tc.sync_trade_control_channel()
    monkeypatch.setattr(cfg, 'CAPITAL_AT_STAKE_THRESHOLD', 1_000_000.0)
    assert tc.real_live_nodes() == []          # genuinely out of the live set
    _add_pending()                              # ...but now holding a real pending buy

    result = tc.sync_trade_control_channel()
    assert result['retired'] == []
    assert result['updated'] == [TICKER]
    assert "ACTION NEEDED" in env.updated[-1]['text']


def test_a_pre_migration_row_is_not_mistaken_for_a_channel_change(env):
    """A row written before configured_channel existed has it NULL -- treating
    that as a mismatch would retire and re-post every card on the first sync
    after the migration."""
    tc.sync_trade_control_channel()
    row = db.get_trade_control_messages()[0]
    with db._conn() as c:
        c.execute("UPDATE trade_control_messages SET configured_channel=NULL WHERE node_id=?",
                  (row['node_id'],))
        c.commit()
    env.posted.clear()
    _add_pending()
    tc.sync_trade_control_channel()
    assert env.posted == []                                  # no duplicate card
    assert env.updated[-1]['ts'] == row['message_ts']         # same message edited
    assert "no longer tracked" not in env.updated[-1]['text']


def test_one_nodes_slack_failure_does_not_block_the_others(env):
    db.add_node(OTHER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                stop_loss=1, max_hold_hours=56, state='live', account='soxl_ira',
                trail_buy_pct=1.0, trail_pct=3.0, starting_notional=50_000, fixed_sl_override=1.0)
    env.fail_on.add(OTHER)
    result = tc.sync_trade_control_channel()
    assert result['posted'] == [TICKER]
    assert any(OTHER in e for e in result['errors'])
    # A real failure is recorded where check_intraday_risk_review will see it
    # (_CONCERNING_RESULT_SUBSTRINGS matches on "fail").
    events = db.get_coverage_events(scenario_key='trade_control_sync')
    assert any(e['result'] == 'failed' for e in events)


def test_repeated_failures_are_throttled_in_the_coverage_log(env):
    db.add_node(OTHER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                stop_loss=1, max_hold_hours=56, state='live', account='soxl_ira',
                trail_buy_pct=1.0, trail_pct=3.0, starting_notional=50_000, fixed_sl_override=1.0)
    env.fail_on.add(OTHER)
    t0 = datetime(2026, 8, 17, 10, 0)
    for i in range(4):
        tc.sync_trade_control_channel(now=t0 + timedelta(minutes=5 * i))
    failures = [e for e in db.get_coverage_events(scenario_key='trade_control_sync')
                if e['result'] == 'failed']
    assert len(failures) == 1


def test_a_deleted_card_is_reposted_not_retried_forever(env):
    tc.sync_trade_control_channel()
    assert len(db.get_trade_control_messages()) == 1

    def _gone(*a, **kw):
        raise SlackApiError("message_not_found", {'ok': False, 'error': 'message_not_found'})
    # Set/restore directly rather than via monkeypatch: the `monkeypatch`
    # fixture instance is shared with the env fixture, so an undo() here would
    # also revert env's cfg patches mid-test.
    original_update = env.chat_update
    env.chat_update = _gone
    _add_pending()  # force a content change so an update is attempted
    result = tc.sync_trade_control_channel()
    assert result['errors'] and db.get_trade_control_messages() == []

    env.chat_update = original_update
    env.posted.clear()
    tc.sync_trade_control_channel()
    assert len(env.posted) == 1


def test_dry_run_sim_position_is_never_rendered_as_a_real_holding(env, monkeypatch):
    """A synthetic (is_dry_run_sim) position has no real broker fill behind it
    -- same reasoning as the Morning Report's 🧪DRY-RUN-SIM suppression."""
    sim_pos = _fake_position()
    sim_pos['is_dry_run_sim'] = 1
    monkeypatch.setattr(db, 'get_open_positions', lambda *a, **kw: [sim_pos])
    tc.sync_trade_control_channel()
    assert "Flat" in json.dumps(env.posted[0]['blocks'])


def test_a_legacy_pending_row_without_a_node_id_cannot_take_down_the_sync(env, monkeypatch):
    """That comprehension sits outside the per-node try -- one malformed row
    must not cost every card its update."""
    legacy = {'ticker': 'LEGACY', 'wl_id': None, 'node': {}, 'signal_price': 1.0,
              'signal_time': '2026-08-17 10:30:00', 'order_placed': 0}
    monkeypatch.setattr(db, 'get_pending_buys', lambda *a, **kw: [legacy])
    result = tc.sync_trade_control_channel()
    assert result['posted'] == [TICKER]
