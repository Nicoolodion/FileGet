from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def publish(self, channel: str, message: Any) -> None:
        payload = json.dumps({"channel": channel, "data": message}, default=str)
        for queue in list(self._subscribers.get(channel, set())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass
        for queue in list(self._subscribers.get("*", set())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    async def subscribe(self, channel: str = "*", maxsize: int = 1000) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        async with self._lock:
            self._subscribers[channel].add(queue)
        return queue

    def unsubscribe(self, channel: str, queue: asyncio.Queue) -> None:
        if channel in self._subscribers:
            self._subscribers[channel].discard(queue)


bus = EventBus()
