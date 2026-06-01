from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _publish_payload(self, channel: str, message: Any) -> str:
        return json.dumps({'channel': channel, 'data': message}, default=str)

    async def publish(self, channel: str, message: Any) -> None:
        payload = self._publish_payload(channel, message)
        for queue in list(self._subscribers.get(channel, set())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass
        for queue in list(self._subscribers.get('*', set())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def publish_sync(self, channel: str, message: Any) -> None:
        """Schedule publish on the running event loop. Safe to call from sync code (handlers)."""
        payload = self._publish_payload(channel, message)
        loop = self._loop
        if loop is None or loop.is_closed():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._loop = loop
        for queue in list(self._subscribers.get(channel, set())):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, payload)
            except Exception:
                pass
        for queue in list(self._subscribers.get('*', set())):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, payload)
            except Exception:
                pass

    async def subscribe(self, channel: str = '*', maxsize: int = 1000) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        async with self._lock:
            self._subscribers[channel].add(queue)
        return queue

    def unsubscribe(self, channel: str, queue: asyncio.Queue) -> None:
        if channel in self._subscribers:
            self._subscribers[channel].discard(queue)


bus = EventBus()
