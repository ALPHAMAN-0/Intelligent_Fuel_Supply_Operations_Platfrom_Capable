"""Minimal async-safe circuit breaker: CLOSED -> OPEN after N consecutive failures -> HALF_OPEN probe after cooldown."""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum


class BreakerState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


STATE_VALUE = {BreakerState.CLOSED: 0, BreakerState.HALF_OPEN: 1, BreakerState.OPEN: 2}


class BreakerOpenError(Exception):
    def __init__(self, name: str):
        super().__init__(f"circuit breaker '{name}' is open")
        self.name = name


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        reset_timeout_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        on_change: Callable[[str, BreakerState], None] | None = None,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s
        self._clock = clock
        self._on_change = on_change
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False
        self._probe_started_at = 0.0

    @property
    def state(self) -> BreakerState:
        if self._state is BreakerState.OPEN and self._clock() - self._opened_at >= self.reset_timeout_s:
            self._set(BreakerState.HALF_OPEN)
        return self._state

    @property
    def failures(self) -> int:
        return self._failures

    def _set(self, new: BreakerState) -> None:
        if new is not self._state:
            self._state = new
            if new is BreakerState.OPEN:
                self._opened_at = self._clock()
            if new is not BreakerState.HALF_OPEN:
                self._probe_in_flight = False
            if self._on_change:
                self._on_change(self.name, new)

    def allow(self) -> bool:
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is not BreakerState.HALF_OPEN:
            return False
        # One probe at a time; a probe that never reported back (cancelled task, lost bookkeeping) is abandoned after a
        # full cooldown so the breaker can never wedge in HALF_OPEN.
        if self._probe_in_flight and self._clock() - self._probe_started_at < self.reset_timeout_s:
            return False
        self._probe_in_flight = True
        self._probe_started_at = self._clock()
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._set(BreakerState.CLOSED)

    def record_failure(self) -> None:
        self._failures += 1
        if self._state is BreakerState.HALF_OPEN or self._failures >= self.failure_threshold:
            self._set(BreakerState.OPEN)
            self._opened_at = self._clock()
        self._probe_in_flight = False

    def snapshot(self) -> dict:
        return {"name": self.name, "state": self.state.value, "failures": self._failures}
