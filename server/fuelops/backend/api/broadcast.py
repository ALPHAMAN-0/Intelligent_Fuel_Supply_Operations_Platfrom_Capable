"""Fan-out of backend events to operator consoles over SSE (/api/stream)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any


class Broadcaster:
    def __init__(self, queue_size: int = 200):
        self.queue_size = queue_size
        self._clients: set[asyncio.Queue] = set()
        self.dropped = 0
        self.published = 0

    @property
    def clients(self) -> int:
        return len(self._clients)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        self.published += 1
        for q in list(self._clients):
            try:
                q.put_nowait((event, data))
            except asyncio.QueueFull:
                self.dropped += 1  # slow client: it will resync from REST on its next refresh

    async def stream(self, keepalive_s: float = 15.0) -> AsyncIterator[str]:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        self._clients.add(q)
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=keepalive_s)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
        finally:
            self._clients.discard(q)
