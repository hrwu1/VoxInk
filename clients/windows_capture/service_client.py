"""The WebSocket client that delivers frames to the local service.

The counterpart of `LocalServiceClient.swift`. One localhost WebSocket is
opened per source stream. Each audio frame is sent as an `audio.frame` JSON
header immediately followed by raw PCM binary, matching the service's
schema-version-1 streaming contract. A frame is delivered now, kept for a
socket that is coming back, or written to the spool. It is never dropped.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .backlog import AudioBacklog, AudioSpool, StreamClock
from .models import (
    AudioFrame,
    AudioSource,
    CaptureError,
    CaptureStatusEvent,
    ReconnectDecision,
    StatusLevel,
    StreamReconnectPolicy,
)

TranscriptHandler = Callable[[dict[str, Any]], None]
TranslationHandler = Callable[[dict[str, Any]], None]
StatusHandler = Callable[[CaptureStatusEvent], None]


class MissingDependency(RuntimeError):
    """Raised with an actionable message for an optional runtime dependency."""


def require_websockets() -> Any:
    try:
        import websockets  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MissingDependency(
            "WebSocket streaming requires 'websockets'. Install capture extras "
            "with: python -m pip install -r requirements-capture.txt"
        ) from exc
    return websockets


class LocalEndpointPolicy:
    @staticmethod
    def validate(url: str) -> None:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme.lower() not in {"ws", "wss"} or host not in {"127.0.0.1", "localhost", "::1"}:
            raise CaptureError.invalid_service_url(url)


def default_spool_directory(session_id: str) -> Path:
    """Where undeliverable audio goes when the helper named no directory."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "VoxInk" / "spool" / session_id


# Chunking shape only. The level at which audio counts as speech is derived by
# the service from the audio itself: this backend applies no gain, so it cannot
# know what level its sources will deliver.
VAD_DESCRIPTION = {
    "silence_duration_ms": 700,
    "min_speech_duration_ms": 200,
    "max_chunk_duration_ms": 15_000,
    "pre_roll_ms": 200,
    "overlap_ms": 200,
}


class WebSocketAudioFrameSink:
    CLIENT_NAME = "voxink-windows-capture"
    # About eight minutes of 16 kHz mono audio held in memory before the
    # oldest of it is written to the spool file.
    DEFAULT_BACKLOG_CAPACITY_BYTES = 16 * 1024 * 1024
    # How long a stop waits for the service to acknowledge `stream.stop`. The
    # service finalizes its last VAD chunk first, so closing the socket early
    # loses the final caption.
    STOP_ACK_TIMEOUT_SECONDS = 2.0
    # A send or open that takes longer than this is a failed transport: the
    # frame is kept and the socket reopened, rather than a pause or stop
    # waiting on a wedged service.
    TRANSPORT_TIMEOUT_SECONDS = 2.0

    def __init__(
        self,
        endpoint: str,
        auth_token: str | None = None,
        applications: list[str] | None = None,
        retain_audio: bool = False,
        language: str | None = None,
        spool_directory: Path | None = None,
        backlog_capacity_bytes: int = DEFAULT_BACKLOG_CAPACITY_BYTES,
        transcript_handler: TranscriptHandler | None = None,
        translation_handler: TranslationHandler | None = None,
        status_handler: StatusHandler | None = None,
        websockets: Any = None,
    ) -> None:
        LocalEndpointPolicy.validate(endpoint)
        self._endpoint = endpoint
        self._auth_token = auth_token
        self._applications = list(applications or [])
        self._retain_audio = retain_audio
        self._language = language
        self._spool_directory = spool_directory
        self._backlog_capacity_bytes = backlog_capacity_bytes
        self._transcript_handler = transcript_handler
        self._translation_handler = translation_handler
        self._status_handler = status_handler
        self._websockets = websockets
        self._session_id: str | None = None
        self._session_sources: list[AudioSource] = []
        self._session_started_ns = 0
        self._connected = False
        self._sockets: dict[str, Any] = {}
        self._receivers: dict[str, asyncio.Task[None]] = {}
        self._stream_locks: dict[str, asyncio.Lock] = {}
        self._reconnects = StreamReconnectPolicy()
        self._stopped: dict[str, asyncio.Event] = {}
        self._clock = StreamClock()
        self._backlogs: dict[str, AudioBacklog] = {}
        self._spools: dict[str, AudioSpool] = {}
        self._spool_failures: set[str] = set()

    # MARK: - AudioFrameSink

    async def connect(self, session_id: str, sources: list[AudioSource]) -> None:
        self._session_id = session_id
        self._session_sources = list(sources)
        self._session_started_ns = time.monotonic_ns()
        self._connected = True
        if self._websockets is None:
            self._websockets = require_websockets()

    async def send(self, frame: AudioFrame) -> None:
        # Serializing each header/binary pair per stream is required because
        # the service allows exactly one outstanding audio header on a
        # connection, and the backlog goes first so the session timeline
        # stays in capture order.
        lock = self._stream_locks.setdefault(frame.stream_id, asyncio.Lock())
        async with lock:
            await self._send_pair(frame)

    def send_status(self, event: CaptureStatusEvent) -> None:
        """Capture diagnostics are local events, not service protocol messages."""
        if self._status_handler is not None:
            self._status_handler(event)

    async def pause(self, session_id: str) -> None:
        """Pause every open stream. Callers must stop feeding frames first so
        the control message lands after the audio it follows."""
        await self._drain_pending_sends()
        at_ms = self._elapsed_ms()
        for stream_id, socket in list(self._sockets.items()):
            await self._send_control(socket, {"type": "stream.pause", "stream_id": stream_id, "at_ms": at_ms})

    async def resume(self, session_id: str) -> None:
        at_ms = self._elapsed_ms()
        for stream_id, socket in list(self._sockets.items()):
            await self._send_control(socket, {"type": "stream.resume", "stream_id": stream_id, "at_ms": at_ms})

    async def disconnect(self, session_id: str, reason: str) -> None:
        # Let queued audio finish rather than cancelling it: the tail of a
        # session is exactly the part a user is still waiting to read.
        await self._drain_pending_sends()
        await self._settle_backlogs()
        self._close_spools()
        at_ms = self._elapsed_ms()
        for stream_id, socket in list(self._sockets.items()):
            await self._send_control(socket, {"type": "stream.stop", "stream_id": stream_id, "at_ms": at_ms})
            await self._await_stop_acknowledgement(stream_id)
        sockets = list(self._sockets.values())
        self._sockets.clear()
        for socket in sockets:
            await self._close_socket(socket)
        for receiver in list(self._receivers.values()):
            receiver.cancel()
        for receiver in list(self._receivers.values()):
            try:
                await receiver
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._receivers.clear()
        self._stopped.clear()
        self._connected = False
        self._session_id = None
        self._session_sources = []

    # MARK: - Delivery

    async def _send_pair(self, frame: AudioFrame) -> None:
        socket = await self._socket_if_available(frame)
        if socket is None:
            self._keep(frame)
            return
        backlog = self._backlogs.get(frame.stream_id)
        pending = backlog.drain() if backlog is not None else []
        pending.append(frame)
        for index, queued in enumerate(pending):
            try:
                await self._deliver(queued, socket)
            except Exception:  # noqa: BLE001 - every transport failure means "keep it"
                self._drop_socket(socket, frame.stream_id)
                for remaining in pending[index:]:
                    self._keep(remaining)
                return

    async def _deliver(self, frame: AudioFrame, socket: Any) -> None:
        duration_ms = frame.duration_ms
        elapsed_ns = frame.timestamp_ns - self._session_started_ns if frame.timestamp_ns >= self._session_started_ns else 0
        captured_ms = elapsed_ns / 1_000_000.0
        start_ms = self._clock.start_ms(frame.stream_id, captured_ms, duration_ms)
        header = {
            "type": "audio.frame",
            "sequence": frame.sequence,
            "stream_id": frame.stream_id,
            "start_ms": start_ms,
            "duration_ms": duration_ms,
            "captured_at_ns": frame.timestamp_ns,
            "byte_length": len(frame.pcm_s16le),
        }
        await self._send_raw(socket, json.dumps(header, separators=(",", ":")))
        await self._send_raw(socket, frame.pcm_s16le)
        self._clock.advance(frame.stream_id, start_ms + duration_ms)

    async def _send_raw(self, socket: Any, message: str | bytes) -> None:
        await asyncio.wait_for(socket.send(message), self.TRANSPORT_TIMEOUT_SECONDS)

    async def _send_control(self, socket: Any, message: dict[str, Any]) -> None:
        try:
            await self._send_raw(socket, json.dumps(message, separators=(",", ":")))
        except Exception:  # noqa: BLE001 - a control that cannot be sent is not a lost recording
            pass

    def _drop_socket(self, socket: Any, stream_id: str) -> None:
        if self._sockets.get(stream_id) is not socket:
            return
        del self._sockets[stream_id]
        asyncio.get_running_loop().create_task(self._close_socket(socket))

    async def _close_socket(self, socket: Any) -> None:
        try:
            await asyncio.wait_for(socket.close(), self.TRANSPORT_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001
            pass

    # MARK: - Backlog and spool

    def _keep(self, frame: AudioFrame) -> None:
        """Hold a frame that could not be delivered: queued for the
        reconnect, or spooled once the queue is full or the session is gone."""
        if self._reconnects.is_dead(frame.stream_id):
            self._spool([frame], frame.stream_id)
            return
        backlog = self._backlogs.get(frame.stream_id)
        if backlog is None:
            backlog = AudioBacklog(self._backlog_capacity_bytes)
            self._backlogs[frame.stream_id] = backlog
        self._spool(backlog.append(frame), frame.stream_id)

    def _spool_backlog(self, stream_id: str) -> None:
        backlog = self._backlogs.get(stream_id)
        if backlog is None:
            return
        self._spool(backlog.drain(), stream_id)

    def _spool(self, frames: list[AudioFrame], stream_id: str) -> None:
        if not frames or stream_id in self._spool_failures:
            return
        first = frames[0]
        spool = self._spools.get(stream_id)
        if spool is None:
            try:
                spool = AudioSpool(self._resolved_spool_directory(), stream_id, first.sample_rate, first.channels)
            except OSError as exc:
                self._spool_failures.add(stream_id)
                self._status(
                    StatusLevel.ERROR, "audio_spool_failed",
                    f"Stream {stream_id} cannot reach the service and its spool file could not be "
                    f"created ({exc}); audio is being lost.",
                    source=first.source,
                )
                return
            self._spools[stream_id] = spool
            self._status(
                StatusLevel.WARNING, "audio_spooled",
                f"Stream {stream_id} cannot reach the service; its audio is being saved to "
                f"{spool.path} and is not in the live transcript.",
                source=first.source,
            )
        try:
            for frame in frames:
                spool.append(frame.pcm_s16le)
        except OSError as exc:
            self._spool_failures.add(stream_id)
            self._status(
                StatusLevel.ERROR, "audio_spool_failed",
                f"Stream {stream_id} could not write to {spool.path} ({exc}); audio is being lost.",
                source=first.source,
            )

    def _resolved_spool_directory(self) -> Path:
        if self._spool_directory is not None:
            return self._spool_directory
        return default_spool_directory(self._session_id or "session")

    async def _settle_backlogs(self) -> None:
        """Deliver what is still queued at Stop, opening a socket if the
        service is back, and spool whatever cannot go."""
        for stream_id in sorted(self._backlogs):
            backlog = self._backlogs[stream_id]
            if backlog.is_empty:
                continue
            frames = backlog.drain()
            socket = self._sockets.get(stream_id)
            if socket is None:
                socket = await self._socket_if_available(frames[0])
            if socket is None:
                self._spool(frames, stream_id)
                continue
            for index, frame in enumerate(frames):
                try:
                    await self._deliver(frame, socket)
                except Exception:  # noqa: BLE001
                    self._drop_socket(socket, stream_id)
                    self._spool(frames[index:], stream_id)
                    break
        self._backlogs.clear()

    def _close_spools(self) -> None:
        for stream_id in sorted(self._spools):
            spool = self._spools[stream_id]
            seconds = spool.duration_seconds
            try:
                spool.close()
            except OSError as exc:
                self._status(
                    StatusLevel.ERROR, "audio_spool_failed",
                    f"Spool {spool.path} could not be finalized ({exc}); its header may be stale.",
                )
                continue
            self._status(
                StatusLevel.WARNING, "audio_spooled",
                f"Stream {stream_id}: {seconds:.1f} s of audio never reached the service and were "
                f"saved to {spool.path}. Transcribe it with: voxink transcribe {spool.path}",
            )
        self._spools.clear()

    # MARK: - Sockets

    async def _socket_if_available(self, frame: AudioFrame) -> Any | None:
        """The stream's socket, opening one when the policy allows an attempt
        now. None means the frame must be kept: the stream is backing off
        from a failed attempt, the attempt just failed, or the session is gone."""
        socket = self._sockets.get(frame.stream_id)
        if socket is not None:
            return socket
        if not self._connected or self._session_id is None:
            return None
        decision = self._reconnects.socket_needed(frame.stream_id, time.monotonic())
        if decision.decision is ReconnectDecision.RECONNECT:
            self._status(
                StatusLevel.WARNING, "service_reconnecting",
                f"Reconnecting stream {frame.stream_id} to the service (attempt {decision.attempt}); "
                "audio is kept until it is back.",
                source=frame.source,
            )
        elif decision.decision is not ReconnectDecision.CONNECT:
            return None
        headers = {"Authorization": f"Bearer {self._auth_token}"} if self._auth_token else None
        try:
            socket = await asyncio.wait_for(
                self._websockets.connect(self._endpoint, additional_headers=headers),
                self.TRANSPORT_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            self._attempt_failed(frame, exc)
            return None
        try:
            await self._send_raw(socket, json.dumps(self._start_message(frame), separators=(",", ":")))
        except Exception as exc:  # noqa: BLE001
            await self._close_socket(socket)
            self._attempt_failed(frame, exc)
            return None
        self._sockets[frame.stream_id] = socket
        self._stopped[frame.stream_id] = asyncio.Event()
        self._receivers[frame.stream_id] = asyncio.get_running_loop().create_task(
            self._receive_loop(socket, frame.stream_id), name=f"receive-{frame.stream_id}"
        )
        return socket

    def _attempt_failed(self, frame: AudioFrame, error: Exception) -> None:
        delay = self._reconnects.attempt_failed(frame.stream_id, time.monotonic())
        self._status(
            StatusLevel.WARNING, "service_unreachable",
            f"Stream {frame.stream_id} could not reach the service ({error or type(error).__name__}); "
            f"trying again in {delay:.2g} s, audio is kept meanwhile.",
            source=frame.source,
        )

    def _start_message(self, frame: AudioFrame) -> dict[str, Any]:
        # The microphone stream carries no per-frame application list, but the
        # session's selection belongs in the transcript metadata no matter
        # which stream opens first.
        applications = (
            list(frame.application_identifiers)
            if frame.application_identifiers is not None
            else (self._applications or None)
        )
        message: dict[str, Any] = {
            "type": "stream.start",
            "schema_version": 1,
            "session_id": self._session_id,
            "stream_id": frame.stream_id,
            "source": frame.source.value,
            "session_sources": [source.value for source in self._session_sources],
            "audio": {
                "encoding": "pcm_s16le",
                "sample_rate_hz": frame.sample_rate,
                "channels": frame.channels,
                "frame_duration_ms": 20,
            },
            "client": {"name": self.CLIENT_NAME, "version": __version__},
            "stream_mode": "frames",
            "vad": dict(VAD_DESCRIPTION),
            "retention": {"audio": self._retain_audio, "separate": True, "combined": False},
        }
        if applications:
            message["application_bundle_identifiers"] = applications
        if frame.channel is not None:
            message["channel"] = frame.channel
            if frame.channel_name is not None:
                message["channel_name"] = frame.channel_name
        if self._language is not None:
            message["language"] = self._language
        return message

    async def _receive_loop(self, socket: Any, stream_id: str) -> None:
        try:
            async for message in socket:
                if self._sockets.get(stream_id) is not socket:
                    return
                if isinstance(message, bytes):
                    continue
                try:
                    envelope = json.loads(message)
                except json.JSONDecodeError:
                    continue
                if not isinstance(envelope, dict):
                    continue
                self._handle_envelope(envelope, stream_id)
            raise ConnectionError("the service closed the connection")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # A closed socket will never acknowledge a stop.
            self._complete_stop_acknowledgement(stream_id)
            if self._sockets.get(stream_id) is socket:
                del self._sockets[stream_id]
                # A refused stream already reported why; its close is expected.
                if not self._reconnects.is_dead(stream_id):
                    self._status(StatusLevel.WARNING, "service_connection_closed", str(exc) or type(exc).__name__)

    def _handle_envelope(self, envelope: dict[str, Any], stream_id: str) -> None:
        kind = envelope.get("type")
        event = envelope.get("event")
        if kind in {"transcript.partial", "transcript.final"}:
            if isinstance(event, dict) and self._transcript_handler is not None:
                self._transcript_handler(event)
        elif kind == "transcript.translation":
            if isinstance(event, dict) and self._translation_handler is not None:
                self._translation_handler(event)
        elif kind == "stream.stopped":
            self._complete_stop_acknowledgement(stream_id)
        elif kind == "stream.ready":
            if self._reconnects.stream_ready(stream_id):
                self._status(StatusLevel.INFO, "service_reconnected", f"Stream {stream_id} reconnected to the service.")
        elif kind == "error":
            code = str(envelope.get("code") or "service_error")
            if code == "session_unavailable":
                # The session is finalized; a reconnect would only be refused
                # again. What is queued goes to the spool now.
                self._reconnects.session_unavailable(stream_id)
                self._spool_backlog(stream_id)
            # Live speakers giving up is not a recording problem.
            level = StatusLevel.WARNING if code == "live_speakers_stopped" else StatusLevel.ERROR
            self._status(level, code, str(envelope.get("message") or "The transcription service reported an error."))

    async def _await_stop_acknowledgement(self, stream_id: str) -> None:
        """Wait for `stream.stopped`, or give up so a wedged service cannot
        block the user's Stop."""
        stopped = self._stopped.get(stream_id)
        if stopped is None or stream_id not in self._sockets:
            return
        try:
            await asyncio.wait_for(stopped.wait(), self.STOP_ACK_TIMEOUT_SECONDS)
        except TimeoutError:
            self._status(StatusLevel.WARNING, "stop_timeout", f"The service did not acknowledge stream.stop for {stream_id} in time.")

    def _complete_stop_acknowledgement(self, stream_id: str) -> None:
        stopped = self._stopped.get(stream_id)
        if stopped is not None:
            stopped.set()

    async def _drain_pending_sends(self) -> None:
        # Every in-flight pair holds its stream lock; taking each lock in turn
        # waits for it to finish.
        for lock in list(self._stream_locks.values()):
            async with lock:
                pass

    def _elapsed_ms(self) -> float:
        now = time.monotonic_ns()
        return (now - self._session_started_ns if now >= self._session_started_ns else 0) / 1_000_000.0

    def _status(self, level: StatusLevel, code: str, message: str, source: AudioSource | None = None) -> None:
        if self._status_handler is None:
            return
        self._status_handler(CaptureStatusEvent(
            session_id=self._session_id or "unknown", source=source, level=level, code=code, message=message,
        ))
