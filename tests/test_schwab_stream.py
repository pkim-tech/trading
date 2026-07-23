"""Tests for schwab_stream.py's reconnect/backoff loop (Part 3, branch C).
Doesn't touch a real websocket -- monkeypatches asyncio.run to simulate
repeated connection failures and asserts the loop retries with increasing
(capped) delay, posts a Slack warning, and never crashes or gives up."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import schwab_stream


class _StopLoop(BaseException):
    """Raised to bound the otherwise-infinite retry loop -- BaseException (not
    Exception) so it isn't swallowed by run_stream_forever's `except Exception`
    even when raised from inside the try block (fake_asyncio_run)."""


def test_reconnect_backoff_increases_and_caps(monkeypatch):
    delays = []
    posted = []

    def fake_asyncio_run(coro):
        coro.close()  # avoid an "unawaited coroutine" warning
        raise ConnectionError("boom")

    def fake_sleep(delay):
        delays.append(delay)
        if len(delays) >= len(schwab_stream._BACKOFF_STEPS) + 2:
            raise _StopLoop()

    monkeypatch.setattr(schwab_stream.asyncio, 'run', fake_asyncio_run)
    monkeypatch.setattr(schwab_stream.time, 'sleep', fake_sleep)
    monkeypatch.setattr(schwab_stream, '_post_message', lambda msg: posted.append(msg))
    monkeypatch.setattr(schwab_stream, '_last_alert_at', 0.0)

    with pytest.raises(_StopLoop):
        schwab_stream.run_stream_forever()

    assert delays[:len(schwab_stream._BACKOFF_STEPS)] == schwab_stream._BACKOFF_STEPS
    # capped -- stays at the last step once past the list
    assert delays[len(schwab_stream._BACKOFF_STEPS)] == schwab_stream._BACKOFF_STEPS[-1]
    assert all("disconnected" in m for m in posted)
    # cooldown-gated (2026-07-23): a persistent disconnect no longer alerts on
    # every retry (a fast real-clock loop like this one never clears the 15min
    # cooldown), just the first
    assert len(posted) == 1


def test_reconnect_resets_backoff_after_a_clean_run(monkeypatch):
    calls = {"n": 0}
    delays = []

    def fake_asyncio_run(coro):
        coro.close()
        calls["n"] += 1
        if calls["n"] in (1, 2):
            raise ConnectionError("boom")
        if calls["n"] == 3:
            return None  # a "clean" run (returns normally) resets backoff_idx
        raise _StopLoop()

    def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(schwab_stream.asyncio, 'run', fake_asyncio_run)
    monkeypatch.setattr(schwab_stream.time, 'sleep', fake_sleep)
    monkeypatch.setattr(schwab_stream, '_post_message', lambda msg: None)

    with pytest.raises(_StopLoop):
        schwab_stream.run_stream_forever()

    # first two failures: backoff steps 0, 1 -- then a clean run resets the
    # index, so the next (4th) call's failure starts back at step 0
    assert delays == [
        schwab_stream._BACKOFF_STEPS[0],
        schwab_stream._BACKOFF_STEPS[1],
    ]
