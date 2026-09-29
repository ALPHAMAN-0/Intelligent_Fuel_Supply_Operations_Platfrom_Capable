"""SSE consumer for GET /v1/stream (guide §6).

Events are hints only: every event just wakes the sync loop, which re-reads REST. The listener reconnects with
backoff (there is no replay) and treats a long silence while the simulation runs as a silent drop (the simulator
silently drops subscribers that fall more than 200 events behind). A 15 s silence is a normal keepalive gap.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable

import httpx

from fuelops.shared.logging import get_logger
from fuelops.shared.metrics import SSE_CONNECTED, SSE_RECONNECTS

log = get_logger("sse")

EventHandler = Callable[[str, dict], Awaitable[None] | None]


def parse_sse_lines(lines: list[str]) -> tuple[str | None, str | None]:
    """Parse one SSE message block into (event name, data). Comment lines (': ...') are ignored."""
    name, data_parts = None, []
    for line in lines:
        if not line or line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            name = value
        elif field == "data":
            data_parts.append(value)
    return name, ("\n".join(data_parts) if data_parts else None)


class SSEListener:
    def __init__(
        self,
        base_url: str,
        on_event: EventHandler,
        *,
        is_running: Callable[[], bool] = lambda: False,
        expected_tick_interval_s: Callable[[], float] = lambda: 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
        max_backoff_s: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.on_event = on_event
        self.is_running = is_running
        self.expected_tick_interval_s = expected_tick_interval_s
        self.transport = transport
        self.max_backoff_s = max_backoff_s
        self.connected = False
        self.reconnects = 0
        self.last_event_at = 0.0
        self.last_tick_event_at = 0.0
        self.last_error: str | None = None
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def _silence_limit(self) -> float:
        return max(3 * self.expected_tick_interval_s(), 20.0)

    async def _dispatch(self, name: str, data: str | None) -> None:
        payload: dict = {}
        if data:
            try:
                payload = json.loads(data)
            except ValueError:
                payload = {"raw": data}
        self.last_event_at = time.monotonic()
        if name == "simulation.tick":
            self.last_tick_event_at = self.last_event_at
        res = self.on_event(name, payload)
        if asyncio.iscoroutine(res):
            await res

    async def _consume(self, client: httpx.AsyncClient) -> None:
        async with client.stream("GET", "/v1/stream", headers={"Accept": "text/event-stream"}) as resp:
            if resp.status_code != 200:
                await resp.aread()
                raise RuntimeError(f"stream returned HTTP {resp.status_code}")
            self._set_connected(True)
            self.last_tick_event_at = time.monotonic()
            await self._dispatch("stream.connected", None)
            block: list[str] = []
            lines = resp.aiter_lines()
            while not self._stop.is_set():
                try:
                    line = await asyncio.wait_for(lines.__anext__(), timeout=self._silence_limit())
                except StopAsyncIteration:
                    raise RuntimeError("stream closed by server") from None
                except TimeoutError:
                    raise RuntimeError("stream silent too long; assuming dropped") from None
                if line == "":
                    if block:
                        name, data = parse_sse_lines(block)
                        block = []
                        if name:
                            await self._dispatch(name, data)
                    continue
                block.append(line)
                if self.is_running() and time.monotonic() - self.last_tick_event_at > self._silence_limit():
                    raise RuntimeError("no simulation.tick while RUNNING; assuming silent drop")

    def _set_connected(self, value: bool) -> None:
        self.connected = value
        SSE_CONNECTED.set(1 if value else 0)

    async def run(self) -> None:
        backoff = 0.5
        timeout = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=self.transport) as client:
            while not self._stop.is_set():
                try:
                    await self._consume(client)
                    backoff = 0.5
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - any failure means reconnect
                    self.last_error = str(exc)
                    if self.connected:
                        log.warning("sse.disconnected", error=str(exc))
                    self._set_connected(False)
                    await self._dispatch_safe("stream.disconnected", {"error": str(exc)})
                self.reconnects += 1
                SSE_RECONNECTS.inc()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(self.max_backoff_s, backoff * 2)
        self._set_connected(False)

    async def _dispatch_safe(self, name: str, payload: dict) -> None:
        try:
            res = self.on_event(name, payload)
            if asyncio.iscoroutine(res):
                await res
        except Exception:  # noqa: BLE001
            log.exception("sse.handler_failed", event=name)
