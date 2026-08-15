"""Fake ACCT_ACTIVITY emitter -- Phase 1's headline deliverable.

Builds a RAW Schwab account-activity message (the real
MESSAGE_TYPE/MESSAGE_DATA envelope, with .NET-decimal `lo`/`signScale`
fixed-point encoding) and feeds it to the real
schwab_stream._handle_activity_message().

It deliberately does NOT push pre-parsed 6-tuples onto
schwab_stream.FILL_QUEUE: that bypass would skip _parse_activity_message
entirely -- the exact function that silently failed on 100% of real messages
for 13 days (0 successful parses across 110 real messages, including 24
genuine OrderFillCompleted events) and the single reason this harness exists.
See docs/design.md's 2026-08-16 (second pass) entry, HIGH finding.

The template below is transcribed from a REAL captured message
(logs/active_signals.log, 2026-08-07, GDXU, SchwabOrderID 1007506544737) --
field names, nesting and the noise fields around the ones the parser reads are
copied from real traffic rather than guessed, since a guessed envelope shape
is precisely what produced the original bug.

Real-message facts preserved here on purpose:
  - SchwabOrderID/AccountNumber are STRINGS, not ints (drain_fill_queue's
    int() coercion is exercised because of this).
  - AccountNumber is the raw 8-digit Schwab account number, NOT one of this
    project's account aliases.
  - signScale = scale*2 + sign_bit (12 == scale 6 positive, 13 == negative);
    an absent `lo` means zero.
  - one message batches several content entries, most of which are not fills.
"""
import json


def encode_decimal(value, scale=6):
    """.NET decimal serialization as Schwab actually sends it: {"lo": "<int>",
    "signScale": scale*2 + sign_bit}. A zero value omits `lo` entirely, which
    is what real messages do (and what _parse_activity_message's missing-field
    branch must keep rejecting)."""
    sign_bit = 1 if value < 0 else 0
    scaled = int(round(abs(value) * (10 ** scale)))
    field = {"signScale": scale * 2 + sign_bit}
    if scaled:
        field["lo"] = str(scaled)
    return field


def build_fill_message_data(account_number, order_id, ticker, side, price, quantity,
                            order_type_code="Market", scale=6, order_id_as_string=True,
                            leaves_quantity=0.0, order_quantity=None):
    """The MESSAGE_DATA payload of an OrderFillCompleted entry (a JSON string
    on the wire -- returned here as a dict, serialized by the caller).

    order_id_as_string=True is the faithful shape (real messages quote it).
    False exists only for the harness's control leg, which isolates the
    string-vs-int order-id mismatch from everything downstream of it -- see
    fake_venue/scenarios.py's FINDING notes.

    leaves_quantity > 0 produces a PARTIAL execution (LegSubStatusPartiallyFilled,
    non-zero LeavesQuantity, ExecutionQuantity < the order's own Quantity) --
    the shape drain_fill_queue's entire docstring is about ("the stream event
    is only a wake-up signal; never trust its price/quantity, a partial locked
    in would under-record shares and let the top-up place a real second buy").
    Supported by the emitter now; driving a partial-fill leg through the real
    reconciliation path is the next scenario to write, not built in Phase 1."""
    return {
        "SchwabOrderID": str(order_id) if order_id_as_string else order_id,
        "AccountNumber": str(account_number),
        "BaseEvent": {
            "EventType": "OrderFillCompleted",
            "OrderFillCompletedEventOrderLegQuantityInfo": {
                "EventType": "OrderFillCompleted",
                "LegId": str(order_id),
                "LegStatus": "LegClosed" if not leaves_quantity else "LegOpen",
                "QuantityInfo": {
                    "ExecutionID": f"20260815-EST-fakevenue-{order_id}",
                    "CumulativeQuantity": encode_decimal(quantity, scale),
                    "LeavesQuantity": encode_decimal(leaves_quantity, scale),
                    "AveragePrice": encode_decimal(price, scale),
                },
                "LegSubStatus": ("LegSubStatusFilled" if not leaves_quantity
                                  else "LegSubStatusPartiallyFilled"),
                "ExecutionInfo": {
                    "ExecutionSequenceNumber": 1,
                    "ExecutionId": f"20260815-EST-fakevenue-{order_id}",
                    "ExecutionQuantity": encode_decimal(quantity, scale),
                    "ExecutionPrice": encode_decimal(price, scale),
                    "ExecutionTimeStamp": {"DateTimeString": "2026-08-15 10:30:09.293"},
                    "ExecutionTransType": "Fill",
                    "ExecutionCapacityCode": "Agency",
                    "RouteName": "G1X_NMS_F2_J1",
                    "RouteSequenceNumber": 1,
                    "ReportingCapacityCode": "RC_Agency",
                    "PrincipalAmmount": encode_decimal(price * quantity, scale),
                    "ActualChargedCommissionAmount": encode_decimal(0, scale),
                    "AsOfTimeStamp": {},
                    "ClientOrderID": f"{order_id}.1",
                },
                "OrderInfoForTransactionPosting": {
                    "LimitPrice": {},
                    "OrderTypeCode": order_type_code,
                    "BuySellCode": "Buy" if side.upper() == "BUY" else "Sell",
                    # The ORDER's full quantity, which exceeds the execution's
                    # own quantity on a partial.
                    "Quantity": encode_decimal(
                        order_quantity if order_quantity is not None else quantity + leaves_quantity,
                        scale),
                    "StopPrice": {},
                    "Symbol": ticker,
                    "SchwabSecurityID": "85159303",
                    "SolicitedCode": "Unsolicited",
                    "AccountingRuleCode": "Margin",
                    "SettlementType": "SettlementType_Regular",
                    "ClientProductCode": "N1",
                },
            },
        },
    }


def build_activity_message(account_number, order_id, ticker, side, price, quantity,
                            with_noise_entries=True, scale=6, order_id_as_string=True,
                            leaves_quantity=0.0):
    """A full raw ACCT_ACTIVITY websocket message, batching the fill entry
    behind an unrelated non-fill entry -- real messages routinely carry 2+
    content entries, and the parser's per-entry loop plus its unconditional
    'received' health metric only get real coverage when they do."""
    content = []
    if with_noise_entries:
        content.append({
            "seq": 3,
            "key": "e4dc964d-c645-7fc9-9879-2385d3abeca9",
            "ACCOUNT": str(account_number),
            "MESSAGE_TYPE": "OrderMonitorCreated",
            "MESSAGE_DATA": json.dumps({
                "SchwabOrderID": str(order_id),
                "AccountNumber": str(account_number),
                "BaseEvent": {"EventType": "OrderMonitorCreated",
                               "OrderMonitorCreatedEvent": {"EventType": "OrderMonitorCreated",
                                                             "LegId": str(order_id)}},
            }),
        })
    content.append({
        "seq": 4,
        "key": "e4dc964d-c645-7fc9-9879-2385d3abeca9",
        "ACCOUNT": str(account_number),
        "MESSAGE_TYPE": "OrderFillCompleted",
        "MESSAGE_DATA": json.dumps(build_fill_message_data(
            account_number, order_id, ticker, side, price, quantity, scale=scale,
            order_id_as_string=order_id_as_string, leaves_quantity=leaves_quantity)),
    })
    return {
        "service": "ACCT_ACTIVITY",
        "timestamp": 1786109410227,
        "command": "SUBS",
        "content": content,
    }


def emit_fill(account_number, order_id, ticker, side, price, quantity, **kw):
    """Drives the REAL stream handler with a realistic raw message. Returns the
    message that was sent (for logging/assertions on the wire shape itself)."""
    import schwab_stream

    msg = build_activity_message(account_number, order_id, ticker, side, price, quantity, **kw)
    schwab_stream._handle_activity_message(msg)
    return msg


def queued_events():
    """Non-destructive peek at schwab_stream.FILL_QUEUE -- lets the harness
    assert on the parser's OUTPUT (the 6-tuple, incl. the decoded decimals)
    without consuming the event drain_fill_queue is about to pop."""
    import schwab_stream

    return list(schwab_stream.FILL_QUEUE.queue)
