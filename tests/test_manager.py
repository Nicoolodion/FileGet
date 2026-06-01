from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from plex_get.manager import Manager


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


@pytest.fixture
def mgr(monkeypatch):
    # Don't touch the real DB; just exercise the pause/resume surface.
    m = Manager()
    return m


def test_manager_starts_unpaused(mgr):
    assert mgr.is_paused() is False


def test_manager_pause_and_resume(mgr):
    mgr.pause()
    assert mgr.is_paused() is True
    mgr.resume()
    assert mgr.is_paused() is False


def test_manager_pause_releases_dispatch_waiter(mgr):
    async def runner():
        mgr.pause()
        # The dispatch loop awaits _paused.wait(); resuming should unblock it.
        mgr.resume()
        # Now schedule a quick test of wait: it should return immediately.
        await asyncio.wait_for(mgr._paused.wait(), timeout=0.5)
        return True

    assert _run(runner()) is True
