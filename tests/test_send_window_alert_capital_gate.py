"""Task #4 (2026-08-19): _send_window_alert's real-time "Signal window" push
had no mode/capital-at-stake filter at all -- confirmed live the same day: a
canary (TWM) and a paper node within 5% of trigger rode along into the push
alongside a real position (YINN). Contradicts the 2026-08-08 capital-at-stake
alerting redesign (real-time Slack should be capital-at-stake-gated only,
same as every other real-time alert). build_reference_table itself is
untouched -- only _send_window_alert's own `hot` filter is gated here."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_notify


def _row(ticker, proximity, has_capital):
    return {'Ticker': ticker, 'Proximity': proximity, '_node': {'ticker': ticker, '_has_capital': has_capital}}


def test_send_window_alert_excludes_sub_threshold_nodes_even_when_hot(monkeypatch):
    rows = [
        _row('REAL', 2.0, True),
        _row('CANARY', 1.0, False),
        _row('PAPER', 0.5, False),
    ]
    monkeypatch.setattr(signals_notify, 'build_reference_table', lambda wl: rows)
    monkeypatch.setattr(signals_notify, 'has_capital_at_stake', lambda node: node['_has_capital'])

    posted = []
    monkeypatch.setattr(signals_notify, '_post_chunked',
                         lambda header, fixed_blocks, units: posted.append((header, units)))
    monkeypatch.setattr(signals_notify, '_ticker_block', lambda r: r['Ticker'])

    signals_notify._send_window_alert('10:25', [])

    assert len(posted) == 1, "expected exactly one chunked post (REAL is hot and has capital at stake)"
    header, units = posted[0]
    assert units == ['REAL'], f"CANARY/PAPER must not ride along into the real-time push, got {units}"


def test_send_window_alert_posts_nothing_hot_when_only_sub_threshold_nodes_are_close(monkeypatch):
    rows = [_row('CANARY', 1.0, False), _row('PAPER', 0.5, False)]
    monkeypatch.setattr(signals_notify, 'build_reference_table', lambda wl: rows)
    monkeypatch.setattr(signals_notify, 'has_capital_at_stake', lambda node: node['_has_capital'])

    posted = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: posted.append(a[0] if a else kw))
    monkeypatch.setattr(signals_notify, '_post_chunked',
                         lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not chunk -- nothing hot")))

    signals_notify._send_window_alert('15:25', [])

    assert len(posted) == 1
    assert 'nothing within range' in posted[0], (
        "with no capital-at-stake node hot, the alert must render as the "
        "quiet 'nothing within range' header, not silently drop the post"
    )
