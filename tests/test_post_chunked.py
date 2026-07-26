"""Test for signals_blocks._post_chunked -- added 2026-07-26 after the Morning
Report hit Slack's 50-block-per-message limit a second time (25 nodes, up from
16 at the first 2026-07-22 fix). Chunking is the sustainable fix: no per-row
block-count budget will hold forever as the watchlist grows."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_blocks


def test_fits_in_one_message_under_the_limit(monkeypatch):
    calls = []
    monkeypatch.setattr(signals_blocks, '_post_message',
                         lambda text, blocks=None, thread_ts=None, reply_broadcast=False:
                             calls.append((text, blocks, thread_ts, reply_broadcast)) or ("C1", "1.0"))
    fixed = [{"type": "header"}]
    units = [[{"type": "section"}] for _ in range(10)]
    channel, ts = signals_blocks._post_chunked("Report", fixed, units)
    assert len(calls) == 1
    assert channel == "C1" and ts == "1.0"
    assert len(calls[0][1]) == 11
    assert calls[0][2] is None


def test_splits_into_threaded_chunks_when_over_the_limit(monkeypatch):
    calls = []
    def fake_post(text, blocks=None, thread_ts=None, reply_broadcast=False):
        idx = len(calls)
        calls.append((text, blocks, thread_ts, reply_broadcast))
        return (f"C{idx}", f"{idx}.0")
    monkeypatch.setattr(signals_blocks, '_post_message', fake_post)

    fixed = [{"type": "header"}, {"type": "context"}]
    units = [[{"type": "section"}, {"type": "actions"}] for _ in range(30)]  # 60 blocks + 2 fixed
    channel, ts = signals_blocks._post_chunked("Report", fixed, units, max_blocks=50)

    assert len(calls) > 1
    assert all(len(blocks) <= 50 for _, blocks, _, _ in calls)
    first_ts = calls[0][2]
    assert first_ts is None
    # every overflow chunk threads under the first message's real ts, broadcast
    # so it still triggers a mobile notification (not just chunk 1)
    assert all(thread_ts == "0.0" and broadcast for _, _, thread_ts, broadcast in calls[1:])
    assert calls[0][3] is False
    # every unit's blocks show up somewhere, none dropped or duplicated
    total_blocks = sum(len(blocks) for _, blocks, _, _ in calls)
    assert total_blocks == len(fixed) + sum(len(u) for u in units)
    assert channel == "C0" and ts == "0.0"


def test_no_units_still_sends_fixed_blocks(monkeypatch):
    calls = []
    monkeypatch.setattr(signals_blocks, '_post_message',
                         lambda text, blocks=None, thread_ts=None, reply_broadcast=False:
                             calls.append((text, blocks, thread_ts, reply_broadcast)) or ("C1", "1.0"))
    channel, ts = signals_blocks._post_chunked("Report", [{"type": "header"}], [])
    assert len(calls) == 1
    assert calls[0][1] == [{"type": "header"}]


def test_first_chunk_failure_still_attempts_remaining_chunks_and_reports_no_ts(monkeypatch):
    """A chunk-1 failure must not abort delivery of the rest, and must not be
    mistaken for a fully-successful post (Opus review, 2026-07-26)."""
    calls = []
    def fake_post(text, blocks=None, thread_ts=None, reply_broadcast=False):
        calls.append((text, blocks, thread_ts, reply_broadcast))
        if len(calls) == 1:
            return (None, None)  # simulate a failed first post
        return (f"C{len(calls)}", f"{len(calls)}.0")
    monkeypatch.setattr(signals_blocks, '_post_message', fake_post)

    units = [[{"type": "section"}] for _ in range(60)]  # forces >1 chunk
    channel, ts = signals_blocks._post_chunked("Report", [{"type": "header"}], units, max_blocks=10)

    assert len(calls) > 1  # later chunks still attempted despite chunk-1 failure
    assert ts is None  # never claim full delivery when any chunk failed
    assert channel is None


def test_middle_chunk_failure_reports_no_ts_even_though_first_succeeded(monkeypatch):
    """A chunk-2+ failure must not be invisible to the caller -- previously
    `morning_report_delivery` would log 'sent' even if a later chunk (e.g. all
    the Buy Candidates rows) silently failed to post (Opus review, 2026-07-26)."""
    calls = []
    def fake_post(text, blocks=None, thread_ts=None, reply_broadcast=False):
        calls.append((text, blocks, thread_ts, reply_broadcast))
        if len(calls) == 1:
            return ("C1", "1.0")
        return (None, None)  # later chunk fails
    monkeypatch.setattr(signals_blocks, '_post_message', fake_post)

    units = [[{"type": "section"}] for _ in range(60)]
    channel, ts = signals_blocks._post_chunked("Report", [{"type": "header"}], units, max_blocks=10)

    assert len(calls) > 1
    assert channel == "C1"
    assert ts is None  # partial delivery must not read as a clean "sent"
