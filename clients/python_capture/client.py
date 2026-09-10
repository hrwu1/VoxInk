"""Async microphone-to-WebSocket capture client.

The client sends raw frames and leaves utterance formation to the helper, whose
energy VAD (`voxink_core.vad`) chunks every stream the same way whichever
backend captured it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import sys
import threading
import time
from typing import Any, Callable

from . import __version__


class MissingDependency(RuntimeError):
    """Raised with an actionable message for an optional runtime dependency."""


def require_sounddevice() -> Any:
    try:
        import sounddevice  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MissingDependency(
            "Microphone capture requires 'sounddevice'. Install capture extras "
            "with: python -m pip install -r requirements-capture.txt"
        ) from exc
    return sounddevice


def require_websockets() -> Any:
    try:
        import websockets  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MissingDependency(
            "WebSocket streaming requires 'websockets'. Install capture extras "
            "with: python -m pip install -r requirements-capture.txt"
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
    language: str | None = None
    auth_token: str | None = None
    retain_audio: bool = False

    def __post_init__(self) -> None:
        if self.language is not None and self.language not in {"en", "zh"}:
            raise ValueError("language must be 'en', 'zh', or omitted for automatic detection")
        if self.source != "microphone":
            raise ValueError("the Python capture backend supports the microphone only")
        if self.sample_rate_hz <= 0 or self.frame_duration_ms <= 0:
            raise ValueError("sample rate and frame duration must be positive")


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    pcm: bytes
    captured_at_ns: int


EventHandler = Callable[[dict[str, Any]], None]


def list_input_devices() -> list[tuple[str, str, bool]]:
    """`(uid, name, is_default)` for every input device `sounddevice` can open.

    `sounddevice` has no stable device identifiers, so the index stands in for
    the UID; it is what `--mic-device` accepts back.
    """
    sounddevice = require_sounddevice()
    devices = sounddevice.query_devices()
    default_input = sounddevice.default.device[0]
    return [
        (str(index), device.get("name", ""), index == default_input)
        for index, device in enumerate(devices)
        if device.get("max_input_channels", 0) > 0
    ]


class MicrophoneCaptureClient:
    def __init__(self, config: CaptureConfig, on_event: EventHandler | None = None) -> None:
        self.config = config
        self.on_event = on_event or _print_event
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
        self._closing = False
        self.dropped_frames = 0

    @property
    def running(self) -> bool:
        return self._websocket is not None

    @property
    def paused(self) -> bool:
        return self.running and not self._accept_audio.is_set()

    async def start(self) -> None:
        if self.running:
            return
        sounddevice = require_sounddevice()
        websockets = require_websockets()
        self._loop = asyncio.get_running_loop()
        self._started_ns = time.monotonic_ns()
        headers = None
        if self.config.auth_token:
            headers = {"Authorization": f"Bearer {self.config.auth_token}"}
        self._closing = False
        self._websocket = await websockets.connect(
            self.config.websocket_url, additional_headers=headers
        )
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
            self._sender_task.cancel()
            await _ignore_cancel(self._sender_task)
        await self._send_json(
            {"type": "stream.stop", "stream_id": self.config.stream_id, "at_ms": self._elapsed_ms()}
        )
        # The helper may drop the socket without a close frame once the
        # stream has stopped; the receiver must not report that as an error.
        self._closing = True
        await self._websocket.close()
        if self._receiver_task is not None:
            await _ignore_cancel(self._receiver_task)
        self._websocket = None

    def _audio_callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        del frames, time_info
        if status:
            _status("audio_warning", str(status))
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
                await self._send_frame(captured)
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

    async def _receive_events(self) -> None:
        try:
            async for message in self._websocket:
                if isinstance(message, bytes):
                    _status("protocol_warning", f"unexpected binary message ({len(message)} bytes)")
                    continue
                try:
                    event = json.loads(message)
                except json.JSONDecodeError:
                    _status("protocol_warning", f"unparseable message: {message[:80]}")
                    continue
                if isinstance(event, dict):
                    self.on_event(event)
        except Exception as exc:
            if self.running and not self._closing:
                _status("stream_error", str(exc))

    def _start_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {
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
            "stream_mode": "frames",
            # Chunking shape is the helper's default; only the audio format is ours.
            "vad": {},
            "retention": {
                "audio": self.config.retain_audio,
                "separate": True,
                "combined": False,
            },
        }
        if self.config.language is not None:
            # Omitted entirely when unset so the service keeps automatic detection.
            message["language"] = self.config.language
        return message

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


def _status(code: str, message: str) -> None:
    """One `code: message` line on stderr, the form the helper reads as a notice."""
    print(f"{code}: {message}", file=sys.stderr, flush=True)


def _print_event(event: dict[str, Any]) -> None:
    """Finals as `[MIC] text`, matching the Swift backend's stdout."""
    if event.get("type") == "transcript.final":
        label = str(event.get("source", "microphone")).upper()
        print(f"[{label}] {event.get('text', '')}", flush=True)
    elif event.get("type") == "error":
        _status(str(event.get("code", "error")), str(event.get("message", "")))


async def _ignore_cancel(task: asyncio.Task[Any]) -> None:
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
