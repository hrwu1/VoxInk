"""FastAPI application for the private, localhost-only VoxInk service."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import subprocess
import tempfile
import threading
import time
import wave
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from clients.python_capture.vad import AudioChunk, EnergyVadChunker, VadConfig
from voxink_core.artifacts import discover_transcripts, read_transcript, read_transcript_text
from voxink_core.models import SessionConfig, Source, TranscriptEvent, TranscriptionOptions
from voxink_core.local_models import (
    LocalModelPackage,
    discover_local_models,
    inspect_model_package,
)
from voxink_core.persistence.audio import AudioRetentionWriter
from voxink_core.providers.base import TranscriptionProvider
from voxink_core.providers.qwen import QwenLocalProvider
from voxink_core.session_ids import new_session_id
from voxink_core.sessions import (
    InvalidSessionTransition,
    Session,
    SessionManager,
    SessionState,
)
from voxink_core.transcripts import OverlapDeduplicator
from voxink_core.vocabulary import MAX_FILE_BYTES, Vocabulary, VocabularyFile


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Report falling behind real time only after this much audio, so one slow chunk
# during model warm-up does not look like sustained lag.
_LAG_WINDOW_MS = 5_000.0
PROTOCOL_VERSION = 1
# Cap on the live caption feed the UI polls; it only renders the recent tail.
UI_EVENT_BUFFER = 500
SERVICE_VERSION = "0.2.0"
# Custom terms live beside the transcripts by default, where a user editing
# their own words is already looking.
VOCABULARY_FILENAME = "vocabulary.txt"


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    model_path: Path = PROJECT_ROOT / "models" / "Qwen3-ASR-1.7B"
    models_root: Path = PROJECT_ROOT / "models"
    output_root: Path = Path.home() / "Documents" / "VoxInk"
    device: str = "auto"
    max_new_tokens: int = 512
    auth_secret: str | None = None
    ui_bootstrap_secret: str | None = None
    native_capture_path: Path | None = None
    # None means the default beside the transcripts; the file need not exist.
    vocabulary_path: Path | None = None


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    sources: list[Source] = Field(default_factory=lambda: [Source.MICROPHONE])
    applications: list[str] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    languages: list[str] = Field(default_factory=lambda: ["en", "zh"])
    audio_saved: bool = False
    save_separate_audio: bool = True
    save_combined_audio: bool = False


class SessionView(BaseModel):
    session_id: str
    state: str
    sources: list[str]
    applications: list[str]


class NativeCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[Source]
    applications: list[str] = Field(default_factory=list)
    retain_audio: bool = False


class ModelSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str


class VocabularyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str


ProviderFactory = Callable[[Path], TranscriptionProvider]


class ServiceRuntime:
    def __init__(
        self,
        settings: ServiceSettings,
        *,
        provider: TranscriptionProvider | None = None,
        provider_factory: ProviderFactory | None = None,
        sessions: SessionManager | None = None,
    ) -> None:
        self.settings = settings
        self.provider_factory = provider_factory or (
            lambda path: QwenLocalProvider(
                path,
                device=settings.device,
                max_new_tokens=settings.max_new_tokens,
            )
        )
        self.provider = provider or self.provider_factory(settings.model_path)
        self.models = {
            package.model_id: package
            for package in discover_local_models(settings.models_root)
            if package.provider == self.provider.name
        }
        self.active_model_id = settings.model_path.expanduser().resolve().name
        active_inspection = inspect_model_package(settings.model_path)
        if (
            active_inspection.package is not None
            and not active_inspection.failures
            and active_inspection.package.provider == self.provider.name
        ):
            self.models.setdefault(
                active_inspection.package.model_id, active_inspection.package
            )
        self.model_switching = False
        self.vocabulary = VocabularyFile(
            settings.vocabulary_path or settings.output_root / VOCABULARY_FILENAME
        )
        self.sessions = sessions or SessionManager(settings.output_root)
        self.audio_writers: dict[str, AudioRetentionWriter] = {}
        self.active_streams: dict[str, set[str]] = {}
        self.paused_streams: dict[str, set[str]] = {}
        self.dedup = OverlapDeduplicator()
        self.stream_ends: dict[tuple[str, str], int] = {}
        self.ui_bootstrap_secret = settings.ui_bootstrap_secret
        # Bounded: only the session the UI is watching is recorded.
        self.ui_events: dict[str, deque[dict[str, Any]]] = {}
        self.ui_events_total: dict[str, int] = {}
        self.native_process: subprocess.Popen[str] | None = None
        self.native_session_id: str | None = None
        self.native_capture_state = "ready"
        self.native_error: str | None = None
        self.native_stderr: deque[str] = deque(maxlen=200)
        self.native_stderr_reader: threading.Thread | None = None


@dataclass
class _StreamContext:
    """Per-connection state for one source stream."""

    session_id: str
    start: dict[str, Any]
    stream_id: str
    source: Source
    chunker: EnergyVadChunker
    sample_rate: int
    anchor_ms: float = 0.0
    window_audio_ms: float = 0.0
    window_compute_ms: float = 0.0

    def anchor_to(self, header: dict[str, Any]) -> None:
        """Align the VAD sample clock with the capture time a client reports.

        Re-anchoring on every frame keeps chunk timestamps on the session
        timeline even when a stream joins late, pauses, or drops frames.
        """
        start_ms = header.get("start_ms")
        if start_ms is not None:
            self.anchor_ms = float(start_ms) - self.chunker.next_sample_ms

    def chunk_bounds(self, chunk: AudioChunk) -> tuple[int, int]:
        start = max(0.0, self.anchor_ms + chunk.start_ms(self.sample_rate))
        end = max(start, self.anchor_ms + chunk.end_ms(self.sample_rate))
        return round(start), round(end)

    def record_work(self, audio_ms: float, compute_ms: float) -> float | None:
        """Accumulate a window of work, returning its real-time factor when lagging."""
        self.window_audio_ms += audio_ms
        self.window_compute_ms += compute_ms
        if self.window_audio_ms < _LAG_WINDOW_MS:
            return None
        factor = self.window_compute_ms / self.window_audio_ms
        self.window_audio_ms = 0.0
        self.window_compute_ms = 0.0
        return factor if factor > 1.0 else None


def create_app(
    settings: ServiceSettings | None = None,
    *,
    provider: TranscriptionProvider | None = None,
    provider_factory: ProviderFactory | None = None,
    sessions: SessionManager | None = None,
) -> FastAPI:
    runtime = ServiceRuntime(
        settings or ServiceSettings(),
        provider=provider,
        provider_factory=provider_factory,
        sessions=sessions,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        _shutdown(runtime)

    app = FastAPI(title="VoxInk Local Service", version=SERVICE_VERSION, lifespan=lifespan)
    app.state.runtime = runtime

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        public_ui = request.url.path == "/ui" or request.url.path.startswith("/ui/")
        if (
            public_ui
            or request.url.path in {"/health", "/v1/ui/bootstrap"}
            or _authorized(request.headers.get("authorization"), runtime.settings.auth_secret)
        ):
            return await call_next(request)
        return JSONResponse(status_code=401, content={"detail": "invalid local service credential"})

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "privacy_mode": "local-only",
            "bind_policy": "loopback-only",
            "provider": runtime.provider.name,
            "model": runtime.active_model_id,
            "model_loaded": runtime.provider.is_loaded,
            "service_version": SERVICE_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": {"partial_results": False, "streaming_audio": True},
            "ui_available": (PROJECT_ROOT / "frontend" / "static-dist" / "index.html").is_file(),
        }

    @app.post("/v1/ui/bootstrap")
    async def bootstrap_ui(request: Request) -> dict[str, str]:
        supplied = request.headers.get("x-voxink-bootstrap")
        expected = runtime.ui_bootstrap_secret
        if not supplied or not expected or not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="invalid or expired UI bootstrap")
        if runtime.settings.auth_secret is None:
            raise HTTPException(status_code=503, detail="authenticated helper is unavailable")
        runtime.ui_bootstrap_secret = None
        return {"token": runtime.settings.auth_secret}

    @app.get("/v1/providers")
    async def providers() -> dict[str, Any]:
        return {
            "providers": [
                {
                    "id": runtime.provider.name,
                    "kind": "local",
                    "enabled": True,
                    "loaded": runtime.provider.is_loaded,
                    "model": runtime.active_model_id,
                }
            ]
        }

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {
            "models": [
                _model_view(package, active=package.model_id == runtime.active_model_id)
                for package in runtime.models.values()
            ],
            "active_model": runtime.active_model_id,
        }

    @app.post("/v1/models/active")
    async def select_model(request: ModelSelectionRequest) -> dict[str, Any]:
        package = runtime.models.get(request.model)
        if package is None:
            raise HTTPException(status_code=404, detail="local model is not installed")
        if request.model == runtime.active_model_id:
            return _model_view(package, active=True)
        if runtime.model_switching or _model_in_use(runtime):
            raise HTTPException(
                status_code=409,
                detail="stop the active recording before changing models",
            )
        runtime.model_switching = True
        try:
            inspection = await asyncio.to_thread(
                inspect_model_package, package.path, verify_checksums=True
            )
            if inspection.failures:
                raise HTTPException(status_code=409, detail=inspection.failures[0])
            if _model_in_use(runtime):
                raise HTTPException(
                    status_code=409,
                    detail="a recording started while the model was being verified",
                )
            replacement = runtime.provider_factory(package.path)
            await runtime.provider.unload()
            runtime.provider = replacement
            runtime.active_model_id = package.model_id
            return _model_view(package, active=True)
        finally:
            runtime.model_switching = False

    @app.get("/v1/vocabulary")
    async def read_vocabulary() -> dict[str, Any]:
        return _vocabulary_view(runtime)

    @app.put("/v1/vocabulary")
    async def write_vocabulary(request: VocabularyUpdateRequest) -> dict[str, Any]:
        try:
            await asyncio.to_thread(runtime.vocabulary.write, request.content)
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"cannot write the vocabulary file: {exc}"
            ) from exc
        return _vocabulary_view(runtime)

    @app.get("/v1/ui/capture/capabilities")
    async def capture_capabilities() -> dict[str, Any]:
        native = runtime.settings.native_capture_path
        available = bool(native and native.is_file() and os.access(native, os.X_OK))
        return {
            "native_capture_available": available,
            "modes": ["microphone"] + (
                ["system", "applications", "microphone_system", "microphone_applications"]
                if available
                else []
            ),
        }

    @app.get("/v1/ui/capture/applications")
    async def capture_applications() -> dict[str, Any]:
        executable = _native_capture_executable(runtime)
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                [str(executable), "--list-apps"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(status_code=503, detail=f"cannot list applications: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "native capture helper failed"
            raise HTTPException(status_code=503, detail=detail)
        applications = []
        seen_bundle_ids: set[str] = set()
        for line in completed.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or not parts[0] or not parts[1] or parts[0] in seen_bundle_ids:
                continue
            seen_bundle_ids.add(parts[0])
            applications.append({"bundle_id": parts[0], "name": parts[1]})
        return {"applications": applications}

    @app.post("/v1/ui/capture/start")
    async def start_native_capture(request: Request, options: NativeCaptureRequest) -> dict[str, Any]:
        executable = _native_capture_executable(runtime)
        if runtime.native_process is not None and runtime.native_process.poll() is None:
            raise HTTPException(status_code=409, detail="a native capture session is already running")
        sources = tuple(dict.fromkeys(options.sources))
        applications = tuple(dict.fromkeys(item.strip() for item in options.applications if item.strip()))
        if not sources:
            raise HTTPException(status_code=422, detail="capture needs at least one source")
        if any(source not in {Source.MICROPHONE, Source.SYSTEM} for source in sources):
            raise HTTPException(status_code=422, detail="unsupported native capture source")
        if applications and Source.SYSTEM not in sources:
            raise HTTPException(
                status_code=422, detail="selected applications require the system source"
            )
        if options.applications and not applications:
            raise HTTPException(status_code=422, detail="selected-application capture needs an application")
        session_id = new_session_id()
        server = request.scope.get("server")
        port = request.url.port or (int(server[1]) if server else None)
        if port is None:
            raise HTTPException(status_code=503, detail="local helper port is unavailable")
        endpoint = f"ws://127.0.0.1:{port}/v1/sessions/{session_id}/stream"
        command = [
            str(executable), "--endpoint", endpoint, "--session-id", session_id, "--no-captions"
        ]
        if options.retain_audio:
            command.append("--retain-audio")
        if Source.MICROPHONE in sources:
            command.append("--mic")
        if Source.SYSTEM in sources:
            # Selected applications narrow the system source; without them the
            # system source means every application.
            if applications:
                for bundle_id in applications:
                    command.extend(("--app", bundle_id))
            else:
                command.append("--system-all")
        try:
            process = subprocess.Popen(
                command,
                env=os.environ.copy(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise HTTPException(status_code=503, detail=f"cannot start native capture: {exc}") from exc
        runtime.native_process = process
        runtime.native_session_id = session_id
        runtime.native_capture_state = "recording"
        runtime.native_error = None
        runtime.native_stderr.clear()
        _start_native_stderr_reader(runtime, process)
        # Only one native session is watched at a time; drop the last one's feed.
        runtime.ui_events.clear()
        runtime.ui_events_total.clear()
        runtime.ui_events[session_id] = deque(maxlen=UI_EVENT_BUFFER)
        runtime.ui_events_total[session_id] = 0
        return _native_capture_view(runtime, after=0)

    @app.get("/v1/ui/capture/status")
    async def native_capture_status(after: int = 0) -> dict[str, Any]:
        return _native_capture_view(runtime, after=max(0, after))

    @app.post("/v1/ui/capture/{action}")
    async def control_native_capture(action: str) -> dict[str, Any]:
        process = runtime.native_process
        if process is None or process.poll() is not None:
            runtime.native_capture_state = "ready"
            raise HTTPException(status_code=409, detail="no native capture session is running")
        if action == "pause":
            process.send_signal(signal.SIGUSR1)
            runtime.native_capture_state = "paused"
        elif action == "resume":
            process.send_signal(signal.SIGUSR2)
            runtime.native_capture_state = "recording"
        elif action == "stop":
            runtime.native_capture_state = "stopping"
            await asyncio.to_thread(_stop_native_capture, runtime)
        else:
            raise HTTPException(status_code=404, detail="unknown native capture action")
        return _native_capture_view(runtime, after=0)

    @app.post("/v1/sessions", response_model=SessionView, status_code=201)
    async def create_session(request: SessionCreateRequest) -> SessionView:
        session_id = request.session_id or new_session_id()
        if runtime.model_switching:
            raise HTTPException(status_code=409, detail="the local model is changing")
        if request.provider is not None and request.provider != runtime.provider.name:
            raise HTTPException(status_code=409, detail="requested provider is not active")
        if request.model is not None and request.model != runtime.active_model_id:
            raise HTTPException(status_code=409, detail="requested model is not active")
        try:
            session = runtime.sessions.create(
                SessionConfig(
                    session_id=session_id,
                    sources=tuple(request.sources),
                    applications=tuple(request.applications),
                    provider=runtime.provider.name,
                    model=runtime.active_model_id,
                    languages=tuple(request.languages),
                    audio_saved=request.audio_saved,
                    save_separate_audio=request.save_separate_audio,
                    save_combined_audio=request.save_combined_audio,
                )
            )
        except (ValueError, FileExistsError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _session_view(session)

    @app.get("/v1/devices")
    async def devices() -> dict[str, Any]:
        """List microphone devices the local capture clients can open."""
        return {"devices": await asyncio.to_thread(_input_devices)}

    @app.get("/v1/transcripts")
    async def transcripts() -> dict[str, Any]:
        """List transcript artifacts under the configured output root."""
        found = await asyncio.to_thread(discover_transcripts, runtime.settings.output_root)
        return {
            "output_root": str(runtime.settings.output_root),
            "transcripts": [artifacts.to_dict() for artifacts in found],
        }

    @app.get("/v1/transcripts/{session_id}")
    async def transcript(session_id: str, part: int | None = None) -> dict[str, Any]:
        artifacts = await asyncio.to_thread(
            read_transcript, runtime.settings.output_root, session_id
        )
        if artifacts is None:
            raise HTTPException(status_code=404, detail="no transcript for that session")
        try:
            text = await asyncio.to_thread(
                read_transcript_text, runtime.settings.output_root, session_id, part=part
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"cannot read the transcript: {exc}"
            ) from exc
        return {**artifacts.to_dict(), "part": part, "text": text}

    @app.get("/v1/sessions")
    async def list_sessions() -> dict[str, Any]:
        """List sessions this helper is holding open.

        Completed and interrupted sessions from earlier runs appear here only
        once the durable session registry replaces this in-memory view; until
        then they are discoverable through their transcript artifacts.
        """
        return {
            "sessions": [
                _session_view(session).model_dump() for session in runtime.sessions.list()
            ],
            "durable_registry": False,
        }

    @app.get("/v1/sessions/{session_id}", response_model=SessionView)
    async def get_session(session_id: str) -> SessionView:
        return _session_view(_get_session(runtime, session_id))

    @app.post("/v1/sessions/{session_id}/{action}", response_model=SessionView)
    async def control_session(session_id: str, action: str) -> SessionView:
        if action not in {"start", "pause", "resume", "stop"}:
            raise HTTPException(status_code=404, detail="unknown session action")
        try:
            getattr(runtime.sessions, action)(session_id)
            session = runtime.sessions.get(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        except (InvalidSessionTransition, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if action == "stop":
            runtime.active_streams.pop(session_id, None)
            runtime.paused_streams.pop(session_id, None)
            _finalize_audio(runtime, session)
        return _session_view(session)

    @app.post("/v1/transcriptions")
    async def transcribe_file(
        file: UploadFile = File(...),
        session_id: str | None = Form(default=None),
        source: Source = Form(default=Source.MICROPHONE),
        stream_id: str | None = Form(default=None),
        language: str | None = Form(default=None),
        start_ms: int = Form(default=0),
        end_ms: int = Form(default=0),
    ) -> dict[str, Any]:
        session = _ensure_started_session(runtime, session_id, source)
        suffix = Path(file.filename or "audio.wav").suffix or ".wav"
        payload = await file.read()
        if not payload:
            raise HTTPException(status_code=400, detail="audio file is empty")
        vocabulary = runtime.vocabulary.current()
        with tempfile.NamedTemporaryFile(suffix=suffix) as temporary:
            temporary.write(payload)
            temporary.flush()
            event = await runtime.provider.transcribe_file(
                temporary.name,
                TranscriptionOptions(
                    session_id=session.config.session_id,
                    segment_id=_segment_id(),
                    stream_id=stream_id or f"{source.value}-1",
                    source=source,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    language=language,
                    context=vocabulary.context,
                ),
            )
        event = _correct(vocabulary, event)
        runtime.sessions.append(event)
        if session_id is None:
            # A caller that named no session wanted one file transcribed, not an
            # open session. Closing it here writes the completion marker and
            # releases the transcript handle instead of leaking both until exit.
            runtime.sessions.stop(session.config.session_id)
            _finalize_audio(runtime, session)
        return {"event": event.to_dict()}

    @app.websocket("/v1/sessions/{session_id}/stream")
    async def stream_audio(websocket: WebSocket, session_id: str) -> None:
        websocket_protocol = _websocket_auth_protocol(
            websocket.headers.get("sec-websocket-protocol"), runtime.settings.auth_secret
        )
        if not _authorized(websocket.headers.get("authorization"), runtime.settings.auth_secret) and websocket_protocol is None:
            await websocket.close(code=4401, reason="invalid local service credential")
            return
        await websocket.accept(subprotocol=websocket_protocol)
        pending_header: dict[str, Any] | None = None
        context: _StreamContext | None = None
        try:
            while True:
                message = await websocket.receive()
                if message.get("text") is not None:
                    control = _parse_control(message["text"])
                    message_type = control.get("type")
                    if message_type == "stream.start":
                        context = _open_stream(runtime, session_id, control)
                        await websocket.send_json(
                            {
                                "type": "stream.ready",
                                "schema_version": PROTOCOL_VERSION,
                                "session_id": session_id,
                            }
                        )
                    elif message_type in {"audio.frame", "audio.chunk"}:
                        if context is None:
                            raise ValueError("stream.start must be sent before audio")
                        pending_header = control
                    elif message_type == "stream.pause":
                        if context is not None:
                            await _send_chunks(
                                websocket, runtime, context, context.chunker.flush("pause")
                            )
                            context.chunker.reset()
                            _pause_stream(runtime, session_id, context.stream_id)
                            await websocket.send_json(
                                _stream_state(runtime, session_id, "stream.paused")
                            )
                    elif message_type == "stream.resume":
                        if context is not None:
                            # Only a fallback: clients that stamp every frame with
                            # start_ms re-anchor on their next frame instead.
                            context.anchor_ms = float(control.get("at_ms", context.anchor_ms))
                            _resume_stream(runtime, session_id, context.stream_id)
                            await websocket.send_json(
                                _stream_state(runtime, session_id, "stream.resumed")
                            )
                    elif message_type == "stream.stop":
                        if context is not None:
                            await _send_chunks(
                                websocket, runtime, context, context.chunker.flush("stop")
                            )
                            _release_stream(runtime, session_id, context.stream_id)
                            runtime.dedup.reset(context.session_id, context.stream_id)
                            runtime.stream_ends.pop((context.session_id, context.stream_id), None)
                            context = None
                        await websocket.send_json({"type": "stream.stopped", "session_id": session_id})
                        await websocket.close(code=1000)
                        return
                    else:
                        raise ValueError(f"unsupported message type: {message_type!r}")
                elif message.get("bytes") is not None:
                    if pending_header is None or context is None:
                        raise ValueError("binary audio must follow an audio.frame or audio.chunk header")
                    pcm = message["bytes"]
                    expected = pending_header.get("byte_length")
                    if expected is not None and len(pcm) != expected:
                        raise ValueError(f"expected {expected} audio bytes, received {len(pcm)}")
                    if _is_paused(runtime, session_id):
                        # A paused session captures nothing. Dropping the buffered
                        # audio keeps pre-pause speech out of the next utterance.
                        context.chunker.reset()
                        await websocket.send_json(
                            {
                                "type": "audio.ignored",
                                "sequence": pending_header.get("sequence"),
                                "reason": "session_paused",
                            }
                        )
                        pending_header = None
                        continue
                    _record_audio(runtime, session_id, context.start, pending_header, pcm)
                    if pending_header["type"] == "audio.frame":
                        context.anchor_to(pending_header)
                        published = await _send_chunks(
                            websocket, runtime, context, context.chunker.feed(pcm)
                        )
                        if not published:
                            await websocket.send_json(
                                {
                                    "type": "audio.accepted",
                                    "sequence": pending_header.get("sequence"),
                                }
                            )
                    else:
                        event, elapsed_ms = await _timed_transcription(
                            runtime, session_id, context.start, pending_header, pcm
                        )
                        await _publish_event(websocket, runtime, event)
                        await _report_lag(
                            websocket, context, event.end_ms - event.start_ms, elapsed_ms
                        )
                    pending_header = None
                elif message.get("type") == "websocket.disconnect":
                    return
        except WebSocketDisconnect:
            return
        except HTTPException as exc:
            # `_ensure_started_session` is shared with the HTTP routes. On an
            # accepted WebSocket its HTTPException would make Starlette try to
            # send an HTTP response, dropping the connection with no diagnosis.
            await websocket.send_json(
                {"type": "error", "code": "session_unavailable", "message": str(exc.detail)}
            )
            await websocket.close(code=1008)
        except (InvalidSessionTransition, KeyError, TypeError, ValueError) as exc:
            await websocket.send_json({"type": "error", "code": "invalid_message", "message": str(exc)})
            await websocket.close(code=1003)
        finally:
            if context is not None:
                _release_stream(runtime, session_id, context.stream_id)
                runtime.dedup.reset(context.session_id, context.stream_id)
                runtime.stream_ends.pop((context.session_id, context.stream_id), None)

    ui_root = PROJECT_ROOT / "frontend" / "static-dist"
    if (ui_root / "index.html").is_file():
        app.mount("/ui", StaticFiles(directory=ui_root, html=True), name="local-ui")

    return app


def _authorized(header: str | None, secret: str | None) -> bool:
    """Compare bearer credentials without leaking timing or accepting empty secrets."""
    if secret is None:
        return True
    prefix = "Bearer "
    if not header or not header.startswith(prefix):
        return False
    supplied = header[len(prefix) :]
    return bool(supplied) and secrets.compare_digest(supplied, secret)


def _input_devices() -> list[dict[str, Any]]:
    """Report input devices, or an empty list where the audio backend is absent.

    `sounddevice` is only needed by the Python capture client, so a helper
    running native capture alone must still answer this instead of failing.
    """
    try:
        import sounddevice
    except (ImportError, OSError):
        return []
    try:
        devices = sounddevice.query_devices()
        default_input = sounddevice.default.device[0]
    except (OSError, ValueError, TypeError):
        return []
    return [
        {
            "index": index,
            "name": device.get("name", ""),
            "channels": device.get("max_input_channels", 0),
            "sample_rate": device.get("default_samplerate"),
            "default": index == default_input,
        }
        for index, device in enumerate(devices)
        if device.get("max_input_channels", 0) > 0
    ]


def _model_view(package: LocalModelPackage, *, active: bool) -> dict[str, Any]:
    return {
        "id": package.model_id,
        "display_name": package.display_name,
        "profile": package.profile,
        "description": package.description,
        "provider": package.provider,
        "size_bytes": package.size_bytes,
        "active": active,
    }


def _vocabulary_view(runtime: ServiceRuntime) -> dict[str, Any]:
    """Report the file's raw text alongside what the service parses from it."""
    vocabulary = runtime.vocabulary.current()
    return {
        "path": str(runtime.vocabulary.path or ""),
        "content": runtime.vocabulary.text(),
        "terms": list(vocabulary.terms),
        "corrections": [
            {"misheard": misheard, "correct": correct}
            for misheard, correct in vocabulary.corrections
        ],
        # The context is capped, so a long list can bias fewer terms than it
        # names. The editor says so rather than letting the extras fail silently.
        "biased_terms": len(vocabulary.biased_terms),
        "max_bytes": MAX_FILE_BYTES,
    }


def _model_in_use(runtime: ServiceRuntime) -> bool:
    process = runtime.native_process
    if process is not None and process.poll() is None:
        return True
    return any(
        session.state in {SessionState.CREATED, SessionState.RUNNING, SessionState.PAUSED}
        for session in runtime.sessions.list()
    )


def _websocket_auth_protocol(header: str | None, secret: str | None) -> str | None:
    """Authenticate browser WebSockets without putting secrets in their URL."""
    if secret is None:
        return None
    prefix = "voxink.auth."
    for requested in (item.strip() for item in (header or "").split(",")):
        if requested.startswith(prefix) and secrets.compare_digest(requested[len(prefix) :], secret):
            return requested
    return None


def _open_stream(
    runtime: ServiceRuntime, session_id: str, control: dict[str, Any]
) -> _StreamContext:
    """Register one source stream and start (or join) its session."""
    source = Source(control.get("source", "microphone"))
    declared_sources = tuple(
        Source(item) for item in control.get("session_sources", [source.value])
    )
    _ensure_started_session(
        runtime,
        session_id,
        source,
        sources=declared_sources,
        applications=_stream_applications(control),
        retention=_stream_retention(control),
    )
    stream_id = str(control["stream_id"])
    runtime.active_streams.setdefault(session_id, set()).add(stream_id)
    runtime.paused_streams.setdefault(session_id, set()).discard(stream_id)
    runtime.dedup.reset(session_id, stream_id)
    runtime.stream_ends.pop((session_id, stream_id), None)
    return _StreamContext(
        session_id=session_id,
        start=control,
        stream_id=stream_id,
        source=source,
        chunker=_stream_chunker(control),
        sample_rate=int(control.get("audio", {}).get("sample_rate_hz", 16000)),
    )


def _pause_stream(runtime: ServiceRuntime, session_id: str, stream_id: str) -> None:
    """Pause the shared session only once every active stream has paused."""
    paused = runtime.paused_streams.setdefault(session_id, set())
    paused.add(stream_id)
    active = runtime.active_streams.get(session_id, set())
    if active and not active - paused:
        _control_if_possible(runtime, session_id, "pause")


def _resume_stream(runtime: ServiceRuntime, session_id: str, stream_id: str) -> None:
    """Resume the shared session as soon as any one stream resumes."""
    paused = runtime.paused_streams.get(session_id)
    if paused is not None:
        paused.discard(stream_id)
    _control_if_possible(runtime, session_id, "resume")


def _stream_state(runtime: ServiceRuntime, session_id: str, message_type: str) -> dict[str, Any]:
    """Acknowledge a control message with the resulting shared session state."""
    try:
        state = runtime.sessions.get(session_id).state.value
    except KeyError:
        state = SessionState.STOPPED.value
    return {"type": message_type, "session_id": session_id, "session_state": state}


def _is_paused(runtime: ServiceRuntime, session_id: str) -> bool:
    try:
        return runtime.sessions.get(session_id).state is SessionState.PAUSED
    except KeyError:
        return False


def _correct(vocabulary: Vocabulary, event: TranscriptEvent) -> TranscriptEvent:
    """Apply the user's correction rules to one event's text."""
    if not vocabulary.corrections or not event.text:
        return event
    corrected = vocabulary.apply(event.text)
    return event if corrected == event.text else replace(event, text=corrected)


async def _publish_event(
    websocket: WebSocket, runtime: ServiceRuntime, event: TranscriptEvent
) -> bool:
    """Correct, deduplicate, persist, and publish one finalized transcript event.

    Corrections run before deduplication so the tail the deduplicator remembers
    is the same text the transcript, the UI, and the next chunk's comparison all
    see; correcting afterwards would leave those four out of step.
    """
    event = _correct(runtime.vocabulary.current(), event)
    key = (event.session_id, event.stream_id)
    previous_end = runtime.stream_ends.get(key)
    if previous_end is None or event.start_ms >= previous_end:
        # Only chunks that overlap in time can have re-transcribed the same
        # audio. Forgetting the tail otherwise keeps a genuinely repeated
        # phrase ("yes. yes.") out of the deduplicator's reach.
        runtime.dedup.reset(*key)
    event = runtime.dedup.deduplicate(event)
    if not event.text.strip():
        # The chunk repeated its predecessor's overlap and carried nothing new.
        return False
    try:
        runtime.sessions.append(event)
    except (InvalidSessionTransition, KeyError, ValueError) as exc:
        # Never drop the connection over session state: report and keep capturing.
        await websocket.send_json(
            {"type": "error", "code": "transcript_not_stored", "message": str(exc)}
        )
        return True
    runtime.stream_ends[key] = max(previous_end or 0, event.end_ms)
    watched = runtime.ui_events.get(event.session_id)
    if watched is not None:
        # Sessions the UI never polls would otherwise accumulate forever.
        watched.append(event.to_dict())
        runtime.ui_events_total[event.session_id] += 1
    await websocket.send_json({"type": "transcript.final", "event": event.to_dict()})
    return True


async def _report_lag(
    websocket: WebSocket, context: _StreamContext, audio_ms: float, compute_ms: float
) -> None:
    factor = context.record_work(audio_ms, compute_ms)
    if factor is None:
        return
    await websocket.send_json(
        {
            "type": "stream.lag",
            "stream_id": context.stream_id,
            "real_time_factor": round(factor, 2),
            "message": "Transcription is slower than capture.",
        }
    )


def _shutdown(runtime: ServiceRuntime) -> None:
    """Close every open session so no transcript part is left unterminated."""
    try:
        _stop_native_capture(runtime)
    except Exception:
        # Finalizing the sessions below is the whole point of this function, so a
        # failed helper stop must never abort it.
        runtime.native_capture_state = "failed"
    for session in runtime.sessions.list():
        if session.state not in {SessionState.RUNNING, SessionState.PAUSED}:
            continue
        try:
            runtime.sessions.stop(session.config.session_id)
        except (InvalidSessionTransition, KeyError):
            continue
        _finalize_audio(runtime, session)


def _native_capture_executable(runtime: ServiceRuntime) -> Path:
    executable = runtime.settings.native_capture_path
    if executable is None or not executable.is_file() or not os.access(executable, os.X_OK):
        raise HTTPException(
            status_code=503,
            detail="native capture is unavailable; run ./scripts/build-macos-capture and restart VoxInk",
        )
    return executable


def _native_capture_view(runtime: ServiceRuntime, *, after: int) -> dict[str, Any]:
    process = runtime.native_process
    exit_code = process.poll() if process is not None else None
    if process is not None and exit_code is not None and runtime.native_capture_state != "ready":
        runtime.native_capture_state = "ready" if exit_code == 0 else "failed"
        _read_native_error(runtime, process)
    session_id = runtime.native_session_id
    events = list(runtime.ui_events.get(session_id or "", ()))
    total = runtime.ui_events_total.get(session_id or "", 0)
    # Cursors are absolute, so a client that falls behind a trimmed buffer skips
    # the dropped events instead of replaying the feed from the start.
    dropped = total - len(events)
    cursor = min(max(0, after - dropped), len(events))
    return {
        "state": runtime.native_capture_state,
        "session_id": session_id,
        "events": events[cursor:],
        "next_cursor": total,
        "exit_code": exit_code,
        "error": runtime.native_error,
        "retention": _retention_view(runtime, session_id),
    }


def _retention_view(runtime: ServiceRuntime, session_id: str | None) -> dict[str, Any]:
    """Report what retention has written, and whether it has had to stop.

    This rides on the capture view rather than the transcript event feed
    because that feed is captions: every entry in it is rendered as spoken
    text, and a storage notice is not something anyone said.
    """
    writer = runtime.audio_writers.get(session_id or "")
    if writer is None:
        return {"retaining": False, "retained_bytes": 0, "stopped_reason": None}
    return {
        "retaining": not writer.retention_stopped,
        "retained_bytes": writer.retained_bytes,
        "stopped_reason": writer.stopped_reason,
    }


def _stop_native_capture(runtime: ServiceRuntime, timeout: float = 10.0) -> None:
    process = runtime.native_process
    if process is None:
        runtime.native_capture_state = "ready"
        return
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
    runtime.native_capture_state = "ready" if process.returncode == 0 else "failed"
    _read_native_error(runtime, process)


def _start_native_stderr_reader(
    runtime: ServiceRuntime, process: subprocess.Popen[str]
) -> None:
    """Drain the capture helper's stderr for as long as it runs.

    The Swift CLI warns about dropped frames up to once per second per source.
    Left unread those fill the pipe, and the helper then blocks inside its own
    status path while the UI still reports "recording".
    """
    stream = process.stderr
    if stream is None:
        return

    def drain() -> None:
        try:
            for line in stream:
                runtime.native_stderr.append(line.rstrip("\n"))
        except (OSError, ValueError):
            return

    reader = threading.Thread(target=drain, name="voxink-native-stderr", daemon=True)
    runtime.native_stderr_reader = reader
    reader.start()


def _read_native_error(runtime: ServiceRuntime, process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        return
    reader = runtime.native_stderr_reader
    if reader is not None:
        reader.join(timeout=1.0)
        runtime.native_stderr_reader = None
    stream = process.stderr
    if stream is not None and not stream.closed:
        # No reader ran, so the pipe still holds whatever the helper wrote.
        try:
            runtime.native_stderr.extend(stream.read().splitlines())
        except (OSError, ValueError):
            pass
        finally:
            stream.close()
    if runtime.native_error is None:
        detail = next(
            (line.strip() for line in reversed(runtime.native_stderr) if line.strip()), ""
        )
        if detail:
            runtime.native_error = detail


def _record_audio(
    runtime: ServiceRuntime,
    session_id: str,
    start: dict[str, Any],
    header: dict[str, Any],
    pcm: bytes,
) -> None:
    session = runtime.sessions.get(session_id)
    if not session.config.audio_saved:
        return
    source = Source(header.get("source", start.get("source", "microphone")))
    writer = runtime.audio_writers.get(session_id)
    if writer is None:
        audio = start.get("audio", {})
        session_dir = (
            session.config.output_directory
            or runtime.settings.output_root / session.config.session_id
        )
        writer = AudioRetentionWriter(
            session_dir,
            sample_rate=int(audio.get("sample_rate_hz", 16000)),
            channels=int(audio.get("channels", 1)),
        )
        runtime.audio_writers[session_id] = writer
    writer.append_pcm(
        source,
        pcm,
        start_ms=round(float(header.get("start_ms", 0))),
    )


def _finalize_audio(runtime: ServiceRuntime, session: Session) -> None:
    writer = runtime.audio_writers.pop(session.config.session_id, None)
    if writer is None:
        return
    writer.close()
    if session.config.save_combined_audio:
        try:
            writer.generate_combined()
        except FileExistsError:
            # A recovered or re-stopped session already has combined parts.
            # Keeping them is required: recovery must never overwrite audio.
            pass
    if not session.config.save_separate_audio:
        for source in Source:
            for path in writer.session_dir.glob(f"{source.value}-part-*.wav"):
                path.unlink()


def _release_stream(
    runtime: ServiceRuntime, session_id: str, stream_id: str | None
) -> None:
    if stream_id is None:
        return
    paused = runtime.paused_streams.get(session_id)
    if paused is not None:
        paused.discard(stream_id)
    streams = runtime.active_streams.get(session_id)
    if streams is None:
        return
    streams.discard(stream_id)
    if streams:
        return
    runtime.active_streams.pop(session_id, None)
    runtime.paused_streams.pop(session_id, None)
    try:
        session = runtime.sessions.get(session_id)
    except KeyError:
        return
    if session.state in {SessionState.RUNNING, SessionState.PAUSED}:
        runtime.sessions.stop(session_id)
        _finalize_audio(runtime, session)


def _stream_chunker(start: dict[str, Any]) -> EnergyVadChunker:
    audio = start.get("audio", {})
    requested = start.get("vad", {})
    # Chunking shape is the client's to choose: how long an utterance may run
    # and how much context to carry are policy. What counts as sound is not.
    # A client cannot know the level its microphone will deliver, and one that
    # guesses wrong makes the service discard speech as silence, so the gate is
    # derived here from the audio itself. `threshold_rms` is ignored rather
    # than rejected: older helpers still send one.
    names = {
        "silence_duration_ms",
        "min_speech_duration_ms",
        "max_chunk_duration_ms",
        "pre_roll_ms",
        "overlap_ms",
    }
    values = {name: requested[name] for name in names if name in requested}
    values["sample_rate_hz"] = int(audio.get("sample_rate_hz", 16000))
    values["frame_duration_ms"] = int(audio.get("frame_duration_ms", 20))
    return EnergyVadChunker(VadConfig(**values))


async def _send_chunks(
    websocket: WebSocket,
    runtime: ServiceRuntime,
    context: _StreamContext,
    chunks: Iterable[AudioChunk],
) -> int:
    """Transcribe finalized chunks, returning how many produced a response."""
    published = 0
    for chunk in chunks:
        chunk_start, chunk_end = context.chunk_bounds(chunk)
        header = {
            "type": "audio.chunk",
            "start_ms": chunk_start,
            "end_ms": chunk_end,
            "finalization_reason": chunk.reason,
        }
        event, elapsed_ms = await _timed_transcription(
            runtime, context.session_id, context.start, header, chunk.pcm
        )
        published += await _publish_event(websocket, runtime, event)
        await _report_lag(websocket, context, chunk_end - chunk_start, elapsed_ms)
    return published


async def _timed_transcription(
    runtime: ServiceRuntime,
    session_id: str,
    start: dict[str, Any],
    header: dict[str, Any],
    pcm: bytes,
) -> tuple[TranscriptEvent, float]:
    began = time.monotonic()
    event = await _transcribe_pcm_chunk(runtime, session_id, start, header, pcm)
    return event, (time.monotonic() - began) * 1000.0


async def _transcribe_pcm_chunk(
    runtime: ServiceRuntime,
    session_id: str,
    start: dict[str, Any],
    header: dict[str, Any],
    pcm: bytes,
):
    audio = start.get("audio", {})
    sample_rate = int(audio.get("sample_rate_hz", 16000))
    channels = int(audio.get("channels", 1))
    if audio.get("encoding", "pcm_s16le") != "pcm_s16le":
        raise ValueError("only pcm_s16le streaming audio is supported")
    source = Source(start.get("source", "microphone"))
    start_ms = int(header.get("start_ms", 0))
    end_ms = int(header.get("end_ms", start_ms + int(header.get("duration_ms", 0))))
    with tempfile.NamedTemporaryFile(suffix=".wav") as temporary:
        with wave.open(temporary.name, "wb") as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm)
        return await runtime.provider.transcribe_file(
            temporary.name,
            TranscriptionOptions(
                session_id=session_id,
                segment_id=_segment_id(),
                stream_id=str(start.get("stream_id", f"{source.value}-1")),
                source=source,
                start_ms=start_ms,
                end_ms=end_ms,
                language=_stream_language(start),
                context=runtime.vocabulary.current().context,
            ),
        )


def _stream_applications(start: dict[str, Any]) -> tuple[str, ...]:
    """Read the selected application bundle identifiers a capture client reports.

    Every stream of a session carries the same list, so whichever stream opens
    first records them in the transcript's front matter.
    """
    applications = start.get("application_bundle_identifiers")
    if applications is None:
        return ()
    if isinstance(applications, str) or not isinstance(applications, (list, tuple)):
        raise ValueError("application_bundle_identifiers must be a list of strings")
    identifiers = []
    for item in applications:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("application bundle identifiers must be non-empty strings")
        identifiers.append(item.strip())
    return tuple(dict.fromkeys(identifiers))


def _stream_retention(start: dict[str, Any]) -> dict[str, bool]:
    """Read the optional audio-retention request from a stream.start message.

    Retention stays off unless a client asks for it, and the combination is
    validated by `SessionConfig` so an impossible request fails at stream start
    rather than silently recording nothing.
    """
    requested = start.get("retention")
    if requested is None:
        return {}
    if not isinstance(requested, dict):
        raise ValueError("retention must be an object")
    names = {"audio": "audio_saved", "separate": "save_separate_audio", "combined": "save_combined_audio"}
    unknown = set(requested) - set(names)
    if unknown:
        raise ValueError(f"unsupported retention fields: {', '.join(sorted(unknown))}")
    values = {}
    for name, field in names.items():
        if name in requested:
            if not isinstance(requested[name], bool):
                raise ValueError(f"retention.{name} must be true or false")
            values[field] = requested[name]
    return values


def _stream_language(start: dict[str, Any]) -> str | None:
    """Read the optional manual language override from a stream.start message."""
    language = start.get("language")
    if language is None:
        return None
    if not isinstance(language, str):
        raise ValueError("language must be a string such as 'en' or 'zh'")
    language = language.strip()
    return language or None


def _parse_control(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("JSON messages must be objects")
    return value


def _ensure_started_session(
    runtime: ServiceRuntime,
    session_id: str | None,
    source: Source,
    *,
    sources: tuple[Source, ...] | None = None,
    applications: tuple[str, ...] = (),
    retention: dict[str, bool] | None = None,
):
    if runtime.model_switching:
        raise HTTPException(status_code=409, detail="the local model is changing")
    if session_id is None:
        session_id = new_session_id()
    try:
        session = runtime.sessions.get(session_id)
    except KeyError:
        session = runtime.sessions.create(
            SessionConfig(
                session_id=session_id,
                sources=sources or (source,),
                applications=applications,
                provider=runtime.provider.name,
                model=runtime.active_model_id,
                **(retention or {}),
            )
        )
    if source not in session.config.sources:
        raise HTTPException(
            status_code=409,
            detail=f"source {source.value} is not enabled for this session",
        )
    if session.state is SessionState.CREATED:
        runtime.sessions.start(session_id)
        session = runtime.sessions.get(session_id)
    if session.state is not SessionState.RUNNING:
        raise HTTPException(status_code=409, detail=f"session is {session.state.value}")
    return session


def _control_if_possible(runtime: ServiceRuntime, session_id: str, action: str) -> None:
    """Apply a session control, ignoring a transition another stream already made."""
    try:
        getattr(runtime.sessions, action)(session_id)
    except (InvalidSessionTransition, KeyError, ValueError):
        return


def _get_session(runtime: ServiceRuntime, session_id: str):
    try:
        return runtime.sessions.get(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc


def _session_view(session) -> SessionView:
    return SessionView(
        session_id=session.config.session_id,
        state=session.state.value,
        sources=[source.value for source in session.config.sources],
        applications=list(session.config.applications),
    )


def _segment_id() -> str:
    return f"seg-{uuid4().hex[:12]}"
