"""Ordered, bounded delivery of captured audio frames to the service client.

The counterpart of `AudioFrameQueue.swift`. An audio callback runs on the
driver's thread, must never block, and must never allocate without bound, yet
the service's wire contract depends on frames arriving in capture order: every
JSON header is paired with the binary message that follows it. Frames are
therefore handed to a bounded deque that exactly one asyncio task drains
sequentially. Order is preserved, and a service that falls behind real time
costs bounded memory plus counted, reported drops instead of an unbounded
pile of concurrent sends.
"""

from __future__ import annotations

import asyncio
from collections import deque
import threading
import time
from typing import Protocol

from .models import AudioFrame, AudioSource, CaptureStatusEvent, StatusLevel


class AudioFrameSink(Protocol):
    async def connect(self, session_id: str, sources: list[AudioSource]) -> None: ...
    async def send(self, frame: AudioFrame) -> None: ...
    def send_status(self, event: CaptureStatusEvent) -> None: ...
    async def pause(self, session_id: str) -> None: ...
    async def resume(self, session_id: str) -> None: ...
    async def disconnect(self, session_id: str, reason: str) -> None: ...


class AudioFrameQueue:
    # Roughly ten seconds of 20 ms frames.
    DEFAULT_CAPACITY = 500

    def __init__(
        self,
        sink: AudioFrameSink,
        session_id: str,
        source: AudioSource,
        capacity: int = DEFAULT_CAPACITY,
        drop_report_interval_ns: int = 1_000_000_000,
    ) -> None:
        self._sink = sink
        self._session_id = session_id
        self._source = source
        self._capacity = max(1, capacity)
        self._drop_report_interval_ns = drop_report_interval_ns
        self._lock = threading.Lock()
        self._frames: deque[AudioFrame] = deque()
        self._open = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._consumer: asyncio.Task[None] | None = None
        self._dropped = 0
        self._reported = 0
        self._last_report_ns = 0

    @property
    def dropped_frame_count(self) -> int:
        """Total frames discarded because the service could not keep up."""
        with self._lock:
            return self._dropped

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._open

    def start(self) -> None:
        """Open the pipeline. Must be called on the event loop thread. Safe to
        call again after `finish_and_drain()` to resume."""
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        with self._lock:
            if self._open:
                return
            self._open = True
            self._loop = loop
            self._wake = wake
        self._consumer = loop.create_task(self._consume(wake), name=f"frame-queue-{self._source.value}")

    def submit(self, frame: AudioFrame) -> None:
        """Enqueue one frame. Non-blocking, callable from an audio callback thread.

        Frames submitted while the pipeline is closed, before start or while
        paused, are discarded at the source rather than buffered, because
        replaying stale audio after a resume would corrupt the session timeline.
        """
        with self._lock:
            if not self._open or self._loop is None or self._wake is None:
                return
            if len(self._frames) >= self._capacity:
                # Evict the oldest queued frame, keeping latency low and
                # leaving the gap behind the listener rather than ahead.
                self._frames.popleft()
                self._dropped += 1
            self._frames.append(frame)
            loop, wake = self._loop, self._wake
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            # The loop is closed; the pipeline is going away with it.
            pass

    async def finish_and_drain(self) -> None:
        """Close the pipeline and wait until every buffered frame has been sent."""
        with self._lock:
            if not self._open:
                consumer = None
            else:
                self._open = False
                consumer = self._consumer
                self._consumer = None
                wake = self._wake
                self._wake = None
                self._loop = None
        if consumer is None:
            return
        if wake is not None:
            wake.set()
        await consumer

    async def _consume(self, wake: asyncio.Event) -> None:
        while True:
            with self._lock:
                frame = self._frames.popleft() if self._frames else None
                closing = not self._open
            if frame is None:
                if closing:
                    break
                wake.clear()
                await wake.wait()
                continue
            try:
                await self._sink.send(frame)
            except Exception:  # noqa: BLE001 - delivery failures are the sink's to report
                pass
            self._report_drops(force=False)
        self._report_drops(force=True)

    def _report_drops(self, force: bool) -> None:
        """Report accumulated drops at most once per interval so a sustained
        overload produces a readable warning rather than a flood of them."""
        now = time.monotonic_ns()
        with self._lock:
            pending = self._dropped - self._reported
            elapsed = now - self._last_report_ns if now >= self._last_report_ns else 0
            due = force or self._last_report_ns == 0 or elapsed >= self._drop_report_interval_ns
            if pending <= 0 or not due:
                return
            self._reported = self._dropped
            self._last_report_ns = now
            total = self._dropped
        self._sink.send_status(CaptureStatusEvent(
            session_id=self._session_id,
            source=self._source,
            level=StatusLevel.WARNING,
            code="audio_frames_dropped",
            message=(
                f"Dropped {pending} {self._source.value} frames because transcription "
                f"fell behind real time ({total} total)."
            ),
        ))
