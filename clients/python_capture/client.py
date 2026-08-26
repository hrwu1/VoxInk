"""Async microphone-to-WebSocket capture client."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import threading
import time
from typing import Any

from . import __version__
from .vad import AudioChunk, EnergyVadChunker, VadConfig


class MissingDependency(RuntimeError):
    """Raised with an actionable message for an optional runtime dependency."""


def require_sounddevice() -> Any:
    try:
        import sounddevice  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MissingDependency(
            "Microphone capture requires 'sounddevice'. Install capture extras "
            "with: python -m pip install sounddevice websockets"
        ) from exc
    return sounddevice


def require_websockets() -> Any:
    try:
        import websockets  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MissingDependency(
            "WebSocket streaming requires 'websockets'. Install capture extras "
            "with: python -m pip install sounddevice websockets"
        ) from exc
    return websockets


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    websocket_url: str
    session_id: str
    stream_id: str = "microphone-1"
    source: str = "microphone"
    sample_rate_hz: int = 16_000
    frame_duration_ms: int = 20
    device: str | int | None = None
    queue_frames: int = 500
    stream_mode: str = "vad-chunks"
    vad: VadConfig = VadConfig()

    def __post_init__(self) -> None:
        if self.source != "microphone":
            raise ValueError("the Python capture client currently supports microphone only")
        if self.stream_mode not in {"frames", "vad-chunks"}:
            raise ValueError("stream_mode must be 'frames' or 'vad-chunks'")
        if self.sample_rate_hz != self.vad.sample_rate_hz:
            raise ValueError("capture and VAD sample rates must match")
        if self.frame_duration_ms != self.vad.frame_duration_ms:
            raise ValueError("capture and VAD frame durations must match")


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    pcm: bytes
    captured_at_ns: int


class MicrophoneCaptureClient:
    def __init__(self, config: CaptureConfig) -> None:
        self.config = config
        self._queue: asyncio.Queue[CapturedFrame] = asyncio.Queue(
            maxsize=config.queue_frames
        )
        self._accept_audio = threading.Event()
        self._stream: Any = None
        self._websocket: Any = None
        self._sender_task: asyncio.Task[None] | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started_ns: int | None = None
        self._sequence = 0
        self._chunker = EnergyVadChunker(config.vad)
        self.dropped_frames = 0

    @property
    def running(self) -> bool:
        return self._websocket is not None

    async def start(self) -> None:
        if self.running:
            return
        sounddevice = require_sounddevice()
        websockets = require_websockets()
        self._loop = asyncio.get_running_loop()
        self._started_ns = time.monotonic_ns()
        self._websocket = await websockets.connect(self.config.websocket_url)
        await self._send_json(self._start_message())

        samples = self.config.sample_rate_hz * self.config.frame_duration_ms // 1000
        self._accept_audio.set()
        self._stream = sounddevice.RawInputStream(
            samplerate=self.config.sample_rate_hz,
            blocksize=samples,
            device=self.config.device,
            channels=1,
            dtype="int16",
            callback=self._audio_callback,
        )
        self._stream.start()
        self._sender_task = asyncio.create_task(self._send_audio(), name="audio-sender")
        self._receiver_task = asyncio.create_task(
            self._receive_events(), name="transcript-receiver"
        )

    async def pause(self) -> None:
        if not self.running or not self._accept_audio.is_set():
            return
        self._accept_audio.clear()
        self._drain_queue()
        await self._send_pending_chunks("pause")
        self._chunker.reset()
        await self._send_json(
            {"type": "stream.pause", "stream_id": self.config.stream_id, "at_ms": self._elapsed_ms()}
        )

    async def resume(self) -> None:
        if not self.running or self._accept_audio.is_set():
            return
        await self._send_json(
            {"type": "stream.resume", "stream_id": self.config.stream_id, "at_ms": self._elapsed_ms()}
        )
        self._accept_audio.set()

    async def stop(self) -> None:
        if not self.running:
            return
        self._accept_audio.clear()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._sender_task is not None:
            await self._queue.join()
        await self._send_pending_chunks("stop")
        if self._sender_task is not None:
            self._sender_task.cancel()
            await _ignore_cancel(self._sender_task)
        await self._send_json(
            {"type": "stream.stop", "stream_id": self.config.stream_id, "at_ms": self._elapsed_ms()}
        )
        await self._websocket.close()
        if self._receiver_task is not None:
            await _ignore_cancel(self._receiver_task)
        self._websocket = None

    def _audio_callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        del frames, time_info
        if status:
            print(f"audio warning: {status}")
        if not self._accept_audio.is_set() or self._loop is None:
            return
        frame = CapturedFrame(bytes(indata), time.monotonic_ns())
        self._loop.call_soon_threadsafe(self._enqueue_frame, frame)

    def _enqueue_frame(self, frame: CapturedFrame) -> None:
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.dropped_frames += 1

    async def _send_audio(self) -> None:
        while True:
            captured = await self._queue.get()
            try:
                if self.config.stream_mode == "frames":
                    await self._send_frame(captured)
                else:
                    for chunk in self._chunker.feed(captured.pcm):
                        await self._send_chunk(chunk, captured.captured_at_ns)
            finally:
                self._queue.task_done()

    async def _send_frame(self, captured: CapturedFrame) -> None:
        sample_count = len(captured.pcm) // 2
        duration_ms = sample_count * 1000.0 / self.config.sample_rate_hz
        start_ms = max(0.0, self._capture_ms(captured.captured_at_ns) - duration_ms)
        header = {
            "type": "audio.frame",
            "sequence": self._next_sequence(),
            "stream_id": self.config.stream_id,
            "start_ms": start_ms,
            "duration_ms": duration_ms,
            "captured_at_ns": captured.captured_at_ns,
            "byte_length": len(captured.pcm),
        }
        await self._send_json(header)
        await self._websocket.send(captured.pcm)

    async def _send_chunk(self, chunk: AudioChunk, captured_at_ns: int) -> None:
        rate = self.config.sample_rate_hz
        duration_ms = (chunk.end_sample - chunk.start_sample) * 1000.0 / rate
        end_ms = self._capture_ms(captured_at_ns)
        header = {
            "type": "audio.chunk",
            "sequence": self._next_sequence(),
            "stream_id": self.config.stream_id,
            "start_ms": max(0.0, end_ms - duration_ms),
            "end_ms": end_ms,
            "speech_ms": chunk.speech_samples * 1000.0 / rate,
            "captured_at_ns": captured_at_ns,
            "byte_length": len(chunk.pcm),
            "finalization_reason": chunk.reason,
        }
        await self._send_json(header)
        await self._websocket.send(chunk.pcm)

    async def _send_pending_chunks(self, reason: str) -> None:
        if self.config.stream_mode != "vad-chunks" or not self.running:
            return
        for chunk in self._chunker.flush(reason):
            await self._send_chunk(chunk, time.monotonic_ns())

    async def _receive_events(self) -> None:
        try:
            async for message in self._websocket:
                if isinstance(message, bytes):
                    print(f"server sent unexpected binary message ({len(message)} bytes)")
                    continue
                try:
                    event = json.loads(message)
                    print(json.dumps(event, ensure_ascii=False))
                except json.JSONDecodeError:
                    print(message)
        except Exception as exc:
            if self.running:
                print(f"WebSocket receiver stopped: {exc}")

    def _start_message(self) -> dict[str, Any]:
        return {
            "type": "stream.start",
            "schema_version": 1,
            "session_id": self.config.session_id,
            "stream_id": self.config.stream_id,
            "source": self.config.source,
            "audio": {
                "encoding": "pcm_s16le",
                "sample_rate_hz": self.config.sample_rate_hz,
                "channels": 1,
                "frame_duration_ms": self.config.frame_duration_ms,
            },
            "device": {"id": self.config.device, "name": None},
            "client": {"name": "voxink-python-capture", "version": __version__},
            "stream_mode": self.config.stream_mode,
            "vad": asdict(self.config.vad),
        }

    async def _send_json(self, message: dict[str, Any]) -> None:
        await self._websocket.send(json.dumps(message, separators=(",", ":")))

    def _next_sequence(self) -> int:
        sequence = self._sequence
        self._sequence += 1
        return sequence

    def _elapsed_ms(self) -> float:
        if self._started_ns is None:
            return 0.0
        return (time.monotonic_ns() - self._started_ns) / 1_000_000.0

    def _capture_ms(self, captured_at_ns: int) -> float:
        if self._started_ns is None:
            return 0.0
        return max(0.0, (captured_at_ns - self._started_ns) / 1_000_000.0)

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                return


async def _ignore_cancel(task: asyncio.Task[Any]) -> None:
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
