"""Capture lifecycle state and elapsed recording time, independent of HTTP/UI."""

from __future__ import annotations

from collections.abc import Callable
import threading
import time


class CaptureState:
    """Count accepted recording time while excluding pauses and stop cleanup."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._state = "ready"
        self._elapsed = 0.0
        self._running_since: float | None = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def elapsed_ms(self) -> int:
        with self._lock:
            elapsed = self._elapsed
            if self._running_since is not None:
                elapsed += self._clock() - self._running_since
            return max(0, round(elapsed * 1000))

    def start(self) -> None:
        with self._lock:
            self._elapsed = 0.0
            self._running_since = self._clock()
            self._state = "recording"

    def transition(self, state: str) -> None:
        if state not in {"ready", "recording", "paused", "stopping", "failed"}:
            raise ValueError(f"unknown capture state: {state}")
        with self._lock:
            if state == self._state:
                return
            now = self._clock()
            if self._running_since is not None:
                self._elapsed += now - self._running_since
            self._running_since = now if state == "recording" else None
            self._state = state
