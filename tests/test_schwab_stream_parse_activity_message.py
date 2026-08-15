"""Regression coverage for the 2026-08-15 _parse_activity_message fix.

The original implementation checked content.get("2") in ("OrderFill",
"ExecutionActivity") against a numeric-key envelope that schwab-py's real
ACCT_ACTIVITY stream never actually sends -- confirmed live: 0 successful
parses across 110 real messages in logs/active_signals.log, including 24
genuine OrderFillCompleted events. Real messages use a MESSAGE_TYPE/
MESSAGE_DATA envelope instead. These tests pin the corrected shape against
real captured message content (not synthetic guesses) so a future shape
drift fails loudly instead of silently, the way this one did for 13 days.

Also covers the same-session hardening from a paired Opus review: the
function now returns (events, health) -- health is logged by the caller
AFTER queueing (not before, to avoid stalling the latency-critical fast
path), and 'received' is logged unconditionally per content entry so a
future shape-drift bug can't hide behind a falsely reassuring "no messages
seen" reading the way the original metric design would have."""
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import schwab_stream


@pytest.fixture(autouse=True)
def env(monkeypatch):
    """_parse_activity_message's health results get logged as
    'stream_message_parsed' coverage_events -- without DB isolation, every
    test in this file would write real rows into cache/live/trading_live.db,
    exactly the leak class this project already fixed once for Slack
    messages (2026-07-22). Confirmed live during this session: an earlier
    version of this file did leak 8 real rows before this fixture was
    added; cleaned up by hand."""
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    signals_db.ensure_tables()
    yield
    Path(tmp_db.name).unlink(missing_ok=True)


def _fill_message(account, order_id, ticker, side, price_lo, qty_lo, sign_scale=12):
    return {
        'service': 'ACCT_ACTIVITY', 'timestamp': 1, 'command': 'SUBS',
        'content': [{
            'seq': 1, 'key': 'x', 'ACCOUNT': account, 'MESSAGE_TYPE': 'OrderFillCompleted',
            'MESSAGE_DATA': json.dumps({
                'SchwabOrderID': order_id, 'AccountNumber': account,
                'BaseEvent': {
                    'EventType': 'OrderFillCompleted',
                    'OrderFillCompletedEventOrderLegQuantityInfo': {
                        'EventType': 'OrderFillCompleted', 'LegId': order_id, 'LegStatus': 'LegClosed',
                        'ExecutionInfo': {
                            'ExecutionPrice': {'lo': price_lo, 'signScale': sign_scale},
                            'ExecutionQuantity': {'lo': qty_lo, 'signScale': sign_scale},
                        },
                        'OrderInfoForTransactionPosting': {'Symbol': ticker, 'BuySellCode': side},
                    }
                }
            })
        }]
    }


def test_parses_real_fill_message_shape():
    # Real captured values: RETL SELL, 49 shares @ $9.1813 (trade_log id=97's
    # actual broker fill, 2026-08-14).
    msg = _fill_message('45111931', '1007601467811', 'RETL', 'Sell', '9181300', '49000000')
    events, health = schwab_stream._parse_activity_message(msg)
    assert events == [('45111931', 'RETL', 'SELL', 9.1813, 49.0, '1007601467811')]
    assert health == [('received', None), ('parsed', 'RETL')]


def test_parses_buy_side_and_uppercases():
    # Real captured values: SOXS BUY, 233 shares @ $40.60 (2026-08-14).
    msg = _fill_message('90961256', '1007558792263', 'SOXS', 'Buy', '40600000', '233000000')
    events, health = schwab_stream._parse_activity_message(msg)
    assert events == [('90961256', 'SOXS', 'BUY', 40.60, 233.0, '1007558792263')]


def test_signscale_13_negative_variant_still_divides_by_correct_power_of_ten():
    # Real traffic confirmed (2026-08-15 review): signScale=13 (scale 6, negative
    # sign bit) appears on real fields alongside signScale=12 (scale 6, positive).
    # .NET decimal: signScale = scale*2 + sign_bit, so divisor is 10**(signScale>>1)
    # regardless of the sign bit -- both 12 and 13 must divide by 1e6.
    msg = _fill_message('1', 'A', 'AAA', 'Buy', '10000000', '1000000', sign_scale=13)
    events, health = schwab_stream._parse_activity_message(msg)
    assert events == [('1', 'AAA', 'BUY', 10.0, 1.0, 'A')]


def test_batched_content_parses_every_fill_not_just_the_first():
    # Real messages routinely batch 2+ content entries in one raw payload --
    # the original implementation only ever looked at content[0].
    msg = _fill_message('1', 'A', 'AAA', 'Buy', '10000000', '1000000')
    msg2 = _fill_message('1', 'B', 'BBB', 'Sell', '20000000', '2000000')
    msg['content'].extend(msg2['content'])
    events, health = schwab_stream._parse_activity_message(msg)
    assert len(events) == 2
    assert events[0][5] == 'A' and events[1][5] == 'B'
    assert health == [('received', None), ('parsed', 'AAA'), ('received', None), ('parsed', 'BBB')]


def test_old_numeric_key_shape_no_longer_matches_and_returns_empty():
    old_shape = {'content': [{'2': 'OrderFill', '3': '{}'}]}
    events, health = schwab_stream._parse_activity_message(old_shape)
    assert events == []
    # 'received' still logged (content entry existed) -- this is exactly the
    # signal that would have caught the original 13-day-silent bug: 0 events,
    # but a nonzero 'received' count proves the stream itself was alive and
    # sending, so evening_status.py can distinguish "shape drift" from "stream
    # is dead."
    assert health == [('received', None)]


def test_non_fill_message_types_are_ignored_but_still_counted_as_received():
    for message_type in ('OrderCreated', 'OrderAccepted', 'ExecutionRequested',
                          'ExecutionRequestCreated', 'ChangeAccepted'):
        msg = {'content': [{'MESSAGE_TYPE': message_type, 'MESSAGE_DATA': '{}'}]}
        events, health = schwab_stream._parse_activity_message(msg)
        assert events == []
        assert health == [('received', None)]


def test_malformed_message_data_returns_empty_not_raises():
    msg = {'content': [{'MESSAGE_TYPE': 'OrderFillCompleted', 'MESSAGE_DATA': 'not json'}]}
    events, health = schwab_stream._parse_activity_message(msg)
    assert events == []
    assert health == [('received', None), ('exception', None)]


def test_missing_price_or_quantity_field_skipped():
    msg = _fill_message('1', 'A', 'AAA', 'Buy', '10000000', '1000000')
    data = json.loads(msg['content'][0]['MESSAGE_DATA'])
    del data['BaseEvent']['OrderFillCompletedEventOrderLegQuantityInfo']['ExecutionInfo']['ExecutionQuantity']
    msg['content'][0]['MESSAGE_DATA'] = json.dumps(data)
    events, health = schwab_stream._parse_activity_message(msg)
    assert events == []
    assert health == [('received', None), ('missing_field', None)]


def test_missing_signscale_treated_as_missing_field_not_trusted_default():
    msg = _fill_message('1', 'A', 'AAA', 'Buy', '10000000', '1000000')
    data = json.loads(msg['content'][0]['MESSAGE_DATA'])
    del data['BaseEvent']['OrderFillCompletedEventOrderLegQuantityInfo']['ExecutionInfo']['ExecutionPrice']['signScale']
    msg['content'][0]['MESSAGE_DATA'] = json.dumps(data)
    events, health = schwab_stream._parse_activity_message(msg)
    assert events == []
    assert health == [('received', None), ('missing_field', None)]


def test_handle_activity_message_queues_every_parsed_event_and_logs_health(monkeypatch):
    printed = []
    monkeypatch.setattr('builtins.print', lambda *a, **k: printed.append(' '.join(str(x) for x in a)))
    while not schwab_stream.FILL_QUEUE.empty():
        schwab_stream.FILL_QUEUE.get_nowait()

    msg = _fill_message('1', 'A', 'AAA', 'Buy', '10000000', '1000000')
    msg2 = _fill_message('1', 'B', 'BBB', 'Sell', '20000000', '2000000')
    msg['content'].extend(msg2['content'])
    schwab_stream._handle_activity_message(msg)

    queued = []
    while not schwab_stream.FILL_QUEUE.empty():
        queued.append(schwab_stream.FILL_QUEUE.get_nowait())
    assert len(queued) == 2

    events = signals_db.get_coverage_events(scenario_key='stream_message_parsed')
    assert len(events) == 4  # 2x received, 2x parsed
    assert sum(1 for e in events if e['result'] == 'received') == 2
    assert sum(1 for e in events if e['result'] == 'parsed') == 2


def test_handle_activity_message_logs_health_after_queueing_not_before(monkeypatch):
    # (2026-08-15 review finding, MEDIUM) -- health logging must not happen
    # ahead of FILL_QUEUE.put, since a DB write ahead of the queue put would
    # contend with the poll loop and could stall a real fill event by seconds.
    order = []
    monkeypatch.setattr(schwab_stream.FILL_QUEUE, 'put', lambda ev: order.append('queued'))
    monkeypatch.setattr(schwab_stream, '_log_parse_health', lambda h, **kw: order.append('logged'))
    monkeypatch.setattr('builtins.print', lambda *a, **k: None)

    msg = _fill_message('1', 'A', 'AAA', 'Buy', '10000000', '1000000')
    schwab_stream._handle_activity_message(msg)
    assert order == ['queued', 'logged']
