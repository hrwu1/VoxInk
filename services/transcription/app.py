"""FastAPI application for the private, localhost-only VoxInk service."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
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
from voxink_core.diarization.jobs import DiarizationJobManager
from voxink_core.echo import EchoDetector
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
from voxink_core.translation import QwenLocalTranslator, TranslationProvider, other_language
from voxink_core.vocabulary import MAX_FILE_BYTES, Vocabulary, VocabularyFile


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Report falling behind real time only after this much audio, so one slow chunk
# during model warm-up does not look like sustained lag.
_LAG_WINDOW_MS = 5_000.0
PROTOCOL_VERSION = 1
# Cap on the live caption feed the UI polls; it only renders the recent tail.
UI_EVENT_BUFFER = 500
SERVICE_VERSION = "0.3.0"
# Custom terms live beside the transcripts by default, where a user editing
# their own words is already looking.
VOCABULARY_FILENAME = "vocabulary.txt"
# A partial is only worth showing once there is this much of an utterance.
_MIN_PARTIAL_MS = 600
# Segment ids a stream remembers as finalized, so a late partial for one of
# them is dropped instead of overwriting the final caption.
_FINALIZED_MEMORY = 64
SYSTEM_AUDIO_BACKENDS = ("auto", "tap", "screencapturekit")


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
    diarization_model_path: Path = (
        PROJECT_ROOT / "models" / "pyannote-speaker-diarization-community-1"
    )
    # "auto" prefers Apple MPS, which matched CPU turn-for-turn on both the
    # single- and three-speaker benchmarks at roughly nine times the speed, and
    # falls back to CPU where it is unavailable.
    diarization_device: str = "auto"
    diarization_enabled: bool = True
    # The forced aligner that lets a transcript block be split where the
    # speaker changed. Optional: absent, blocks are labeled whole.
    diarization_aligner_path: Path | None = (
        PROJECT_ROOT / "models" / "Qwen3-ForcedAligner-0.6B"
    )
    # Live partial captions: re-transcribe the open utterance at most this
    # often. The interval stretches itself when inference is slower than it.
    partial_captions: bool = True
    partial_interval_ms: int = 1000
    # Drop a microphone chunk that is the room's echo of a system channel.
    echo_suppression: bool = True
    # Local instruct model used to translate finalized segments. Optional:
    # absent, translation is reported unavailable and never requested.
    translation_model_path: Path | None = PROJECT_ROOT / "models" / "Qwen3-0.6B"


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
    diarize: bool = False


class DiarizationRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Re-run over a session that already has artifacts, for example after the
    #: transcript changed and the existing ones went stale.
    regenerate: bool = False


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
    diarize: bool = False
    # Core Audio device UID from /v1/devices; None keeps the system default.
    microphone_device: str | None = None
    # Manual language override ("en" or "zh"); None lets the model detect it.
    language: str | None = None
    # Translate each finalized segment into the other language, on device.
    translate: bool = False
    backend: str | None = None


class MarkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = ""


class RevealRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None


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
        translator: TranslationProvider | None = None,
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
        self.diarization = DiarizationJobManager(
            model_path=settings.diarization_model_path,
            device=settings.diarization_device,
            aligner_path=settings.diarization_aligner_path,
            enabled=settings.diarization_enabled,
        )
        self.translator = translator or _default_translator(settings)
        # Serializes translation across streams; the model is not re-entrant.
        self.translation_lock = threading.Lock()
        self.translation_error: str | None = None
        # Why the last partial caption failed, if it did; a diagnostic only.
        self.partial_error: str | None = None
        self.echo = EchoDetector()
        self.audio_writers: dict[str, AudioRetentionWriter] = {}
        self.active_streams: dict[str, set[str]] = {}
        self.paused_streams: dict[str, set[str]] = {}
        self.dedup = OverlapDeduplicator()
        self.stream_ends: dict[tuple[str, str], int] = {}
        # Monotonic clock reading when each session started running, so a
        # marker can be placed on the session timeline without an audio frame.
        self.session_started_at: dict[str, float] = {}
        self.last_session_id: str | None = None
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
        self.shutting_down = False
        # Sessions the UI asked to diarize or translate. The capture helper
        # builds the session from its own stream message and knows nothing
        # about either, so the request is remembered here and applied when
        # that session is created, rather than widening the native protocol.
        self.pending_diarization: set[str] = set()
        self.translate_sessions: set[str] = set()

    @property
    def translation_available(self) -> bool:
        return self.translator is not None

    def translate_text(self, text: str, source_language: str, target_language: str) -> str:
        """Translate one segment on the calling thread; serialized per model."""
        translator = self.translator
        if translator is None:
            raise RuntimeError("translation is unavailable")
        with self.translation_lock:
            return translator.translate(text, source_language, target_language)


def _default_translator(settings: ServiceSettings) -> TranslationProvider | None:
    path = settings.translation_model_path
    if path is None or not (Path(path).expanduser() / "config.json").is_file():
        return None
    return QwenLocalTranslator(path, device=settings.device)


@dataclass
class _StreamContext:
    """Per-connection state for one source stream."""

    session_id: str
    start: dict[str, Any]
    stream_id: str
    source: Source
    chunker: EnergyVadChunker
    sample_rate: int
    channel: str | None = None
    channel_name: str | None = None
    anchor_ms: float = 0.0
    window_audio_ms: float = 0.0
    window_compute_ms: float = 0.0
    # Partial captions for the utterance in progress.
    partial_segment_id: str | None = None
    partial_task: asyncio.Task[None] | None = None
    last_partial_at: float = 0.0
    partial_interval_s: float = 1.0
    finalized_partials: deque[str] = field(default_factory=lambda: deque(maxlen=_FINALIZED_MEMORY))
    # Messages produced off the receive loop and sent by it, so nothing writes
    # to the socket concurrently.
    outbox: list[dict[str, Any]] = field(default_factory=list)
    background: set[asyncio.Task[None]] = field(default_factory=set)
    echo_suppressed: int = 0

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

    def sample_bounds(self, start_sample: int, end_sample: int) -> tuple[int, int]:
        start = max(0.0, self.anchor_ms + start_sample * 1000.0 / self.sample_rate)
        end = max(start, self.anchor_ms + end_sample * 1000.0 / self.sample_rate)
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

    def keep(self, task: asyncio.Task[None]) -> None:
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    def cancel_partial(self) -> None:
        """Forget the open utterance's partial; its final is on its way or gone."""
        if self.partial_segment_id is not None:
            self.finalized_partials.append(self.partial_segment_id)
        self.partial_segment_id = None
        task = self.partial_task
        self.partial_task = None
        if task is not None and not task.done():
            task.cancel()


def create_app(
    settings: ServiceSettings | None = None,
    *,
    provider: TranscriptionProvider | None = None,
    provider_factory: ProviderFactory | None = None,
    sessions: SessionManager | None = None,
    translator: TranslationProvider | None = None,
) -> FastAPI:
    runtime = ServiceRuntime(
        settings or ServiceSettings(),
        provider=provider,
        provider_factory=provider_factory,
        sessions=sessions,
        translator=translator,
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
            "capabilities": {
                "partial_results": runtime.settings.partial_captions,
                "streaming_audio": True,
                "translation": runtime.translation_available,
                "echo_suppression": runtime.settings.echo_suppression,
                "channels": True,
            },
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

    @app.get("/v1/translation")
    async def translation_capability() -> dict[str, Any]:
        translator = runtime.translator
        return {
            "available": translator is not None,
            "provider": translator.name if translator else None,
            "model_path": str(runtime.settings.translation_model_path or ""),
            "loaded": bool(translator and translator.is_loaded),
            "languages": ["en", "zh"],
            "error": runtime.translation_error,
        }

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
            "backends": list(SYSTEM_AUDIO_BACKENDS),
            "partial_captions": runtime.settings.partial_captions,
            "echo_suppression": runtime.settings.echo_suppression,
            "translation_available": runtime.translation_available,
            "languages": ["auto", "en", "zh"],
        }

    @app.get("/v1/ui/capture/applications")
    async def capture_applications() -> dict[str, Any]:
        executable = _native_capture_executable(runtime)
        lines = await _run_native_listing(executable, "--list-apps")
        applications = []
        seen_bundle_ids: set[str] = set()
        for line in lines:
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
        if options.backend is not None and options.backend not in SYSTEM_AUDIO_BACKENDS:
            raise HTTPException(status_code=422, detail="unknown system audio backend")
        language = (options.language or "").strip().lower() or None
        if language is not None and language not in {"en", "zh"}:
            raise HTTPException(status_code=422, detail="language must be en or zh")
        if options.translate and not runtime.translation_available:
            raise HTTPException(
                status_code=409,
                detail="translation is unavailable; the local translation package is not installed",
            )
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
        if options.microphone_device and options.microphone_device.strip():
            command.extend(("--mic-device", options.microphone_device.strip()))
        if language is not None:
            command.extend(("--language", language))
        if options.backend is not None:
            command.extend(("--backend", options.backend))
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
        runtime.pending_diarization.discard(session_id)
        runtime.translate_sessions.discard(session_id)
        if options.diarize and options.retain_audio and Source.MICROPHONE in sources:
            # Silently ignored without retained microphone audio: the UI already
            # prevents that combination, and refusing the whole capture over an
            # optional post-processing step would be a poor trade.
            runtime.pending_diarization.add(session_id)
        if options.translate:
            runtime.translate_sessions.add(session_id)
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

    @app.post("/v1/ui/capture/mark")
    async def mark_native_capture(body: MarkRequest | None = None) -> dict[str, Any]:
        """Drop a timestamped marker into the transcript being written."""
        process = runtime.native_process
        session_id = runtime.native_session_id
        if process is None or process.poll() is not None or session_id is None:
            raise HTTPException(status_code=409, detail="no native capture session is running")
        label = (body or MarkRequest()).label
        at_ms = _session_elapsed_ms(runtime, session_id)
        try:
            await asyncio.to_thread(runtime.sessions.mark, session_id, at_ms, label)
        except KeyError as exc:
            # The helper has not opened its first stream yet, so there is no
            # transcript to mark. Asking again a moment later works.
            raise HTTPException(status_code=409, detail="the transcript is not open yet") from exc
        except InvalidSessionTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        entry = {
            "type": "mark",
            "session_id": session_id,
            "segment_id": f"mark-{uuid4().hex[:12]}",
            "source": None,
            "start_ms": at_ms,
            "end_ms": at_ms,
            "text": " ".join(label.split()),
            "is_final": True,
        }
        _record_ui_event(runtime, session_id, entry)
        return {"session_id": session_id, "at_ms": at_ms, "label": entry["text"]}

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

    @app.post("/v1/ui/actions/reveal")
    async def reveal_session(body: RevealRequest | None = None) -> dict[str, Any]:
        """Open a session's folder in Finder, or the output root with no session."""
        session_id = (body or RevealRequest()).session_id or runtime.last_session_id
        directory = (
            _session_directory(runtime, session_id) if session_id else runtime.settings.output_root
        )
        if not directory.is_dir():
            raise HTTPException(status_code=404, detail="that session has no folder yet")
        if sys.platform != "darwin":
            raise HTTPException(status_code=501, detail="revealing folders needs macOS")
        try:
            await asyncio.to_thread(subprocess.run, ["open", str(directory)], check=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(status_code=500, detail=f"cannot open the folder: {exc}") from exc
        return {"session_id": session_id, "directory": str(directory)}

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
                    diarize=request.diarize,
                )
            )
        except (ValueError, FileExistsError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _session_view(session)

    @app.get("/v1/diarization")
    async def diarization_capability() -> dict[str, Any]:
        """Whether speaker diarization can run on this installation."""
        return runtime.diarization.capability()

    @app.get("/v1/sessions/{session_id}/diarization")
    async def diarization_status(session_id: str) -> dict[str, Any]:
        directory = _session_directory(runtime, session_id)
        return await asyncio.to_thread(
            runtime.diarization.describe, directory, session_id
        )

    @app.post("/v1/sessions/{session_id}/diarization", status_code=202)
    async def start_diarization(
        session_id: str, body: DiarizationRequestBody | None = None
    ) -> dict[str, Any]:
        """Request diarization for a session that has already stopped.

        Accepted, not completed: the job runs in the background and its state
        is polled from the companion GET.
        """
        directory = _session_directory(runtime, session_id)
        if not directory.is_dir():
            raise HTTPException(status_code=404, detail="session not found")
        try:
            session = runtime.sessions.get(session_id)
        except KeyError:
            session = None
        if session is not None and session.state is not SessionState.STOPPED:
            raise HTTPException(
                status_code=409,
                detail="diarization runs after the session stops",
            )
        job = runtime.diarization.request(
            directory,
            session_id,
            regenerate=(body or DiarizationRequestBody()).regenerate,
        )
        return job.to_dict()

    @app.get("/v1/devices")
    async def devices() -> dict[str, Any]:
        """List microphone devices the local capture clients can open."""
        native = runtime.settings.native_capture_path
        if native is not None and native.is_file() and os.access(native, os.X_OK):
            try:
                lines = await _run_native_listing(native, "--list-devices")
            except HTTPException:
                lines = []
            found = []
            for line in lines:
                parts = line.split("\t")
                if len(parts) < 2 or not parts[0]:
                    continue
                found.append(
                    {
                        "uid": parts[0],
                        "name": parts[1],
                        "default": len(parts) > 2 and parts[2] == "default",
                    }
                )
            if found:
                return {"devices": found, "source": "native"}
        return {"devices": await asyncio.to_thread(_input_devices), "source": "python"}

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
        if action == "start":
            runtime.session_started_at[session_id] = time.monotonic()
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
                if context is not None:
                    # Partials and translations computed while this loop was
                    # waiting leave now, on the loop's own turn at the socket.
                    await _flush_outbox(websocket, runtime, context)
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
                                "partial_results": runtime.settings.partial_captions,
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
                            context.cancel_partial()
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
                            context.cancel_partial()
                            await _drain_background(websocket, runtime, context)
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
                        context.cancel_partial()
                        await websocket.send_json(
                            {
                                "type": "audio.ignored",
                                "sequence": pending_header.get("sequence"),
                                "reason": "session_paused",
                            }
                        )
                        pending_header = None
                        continue
                    _record_audio(runtime, session_id, context, pending_header, pcm)
                    if pending_header["type"] == "audio.frame":
                        context.anchor_to(pending_header)
                        if context.source is Source.SYSTEM and runtime.settings.echo_suppression:
                            runtime.echo.observe(
                                session_id,
                                context.stream_id,
                                round(float(pending_header.get("start_ms", context.anchor_ms + context.chunker.next_sample_ms))),
                                pcm,
                            )
                        published = await _send_chunks(
                            websocket, runtime, context, context.chunker.feed(pcm)
                        )
                        if not published:
                            _maybe_schedule_partial(runtime, context)
                            await websocket.send_json(
                                {
                                    "type": "audio.accepted",
                                    "sequence": pending_header.get("sequence"),
                                }
                            )
                    else:
                        event, elapsed_ms = await _timed_transcription(
                            runtime, session_id, context, pending_header, pcm
                        )
                        published = await _publish_event(websocket, runtime, event)
                        if published is not None:
                            _maybe_schedule_translation(runtime, context, published)
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
                context.cancel_partial()
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


async def _run_native_listing(executable: Path, flag: str) -> list[str]:
    """Run one of the helper's listing modes and return its stdout lines."""
    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            [str(executable), flag],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=503, detail=f"cannot run the capture helper: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "native capture helper failed"
        raise HTTPException(status_code=503, detail=detail)
    return completed.stdout.splitlines()


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
            "uid": None,
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
    channel, channel_name = _stream_channel(control)
    return _StreamContext(
        session_id=session_id,
        start=control,
        stream_id=stream_id,
        source=source,
        chunker=_stream_chunker(control),
        sample_rate=int(control.get("audio", {}).get("sample_rate_hz", 16000)),
        channel=channel if source is Source.SYSTEM else None,
        channel_name=channel_name if source is Source.SYSTEM else None,
        partial_interval_s=runtime.settings.partial_interval_ms / 1000.0,
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


def _record_ui_event(runtime: ServiceRuntime, session_id: str, entry: dict[str, Any]) -> None:
    """Append to the UI feed, if this session is the one the UI is watching.

    Sessions the UI never polls would otherwise accumulate forever.
    """
    watched = runtime.ui_events.get(session_id)
    if watched is None:
        return
    watched.append(entry)
    runtime.ui_events_total[session_id] += 1


async def _publish_event(
    websocket: WebSocket, runtime: ServiceRuntime, event: TranscriptEvent
) -> TranscriptEvent | None:
    """Correct, deduplicate, persist, and publish one finalized transcript event.

    Corrections run before deduplication so the tail the deduplicator remembers
    is the same text the transcript, the UI, and the next chunk's comparison all
    see; correcting afterwards would leave those four out of step.

    Returns the event as published, or None when nothing was published.
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
        return None
    try:
        runtime.sessions.append(event)
    except (InvalidSessionTransition, KeyError, ValueError) as exc:
        # Never drop the connection over session state: report and keep capturing.
        await websocket.send_json(
            {"type": "error", "code": "transcript_not_stored", "message": str(exc)}
        )
        return event
    runtime.stream_ends[key] = max(previous_end or 0, event.end_ms)
    _record_ui_event(runtime, event.session_id, {**event.to_dict(), "type": "transcript"})
    await websocket.send_json({"type": "transcript.final", "event": event.to_dict()})
    return event


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


# ---------------------------------------------------------------------------
# Partial captions
# ---------------------------------------------------------------------------


def _maybe_schedule_partial(runtime: ServiceRuntime, context: _StreamContext) -> None:
    """Re-transcribe the open utterance if it is time to, without blocking.

    One partial at a time per stream, never more often than the interval, and
    the interval grows to fit inference that turns out slower than it: on a
    CPU that takes three seconds per pass, asking every second would queue
    work the final then has to wait behind.
    """
    if not runtime.settings.partial_captions:
        return
    task = context.partial_task
    if task is not None and not task.done():
        return
    now = time.monotonic()
    if now - context.last_partial_at < context.partial_interval_s:
        return
    active = context.chunker.active_audio()
    if active is None:
        return
    pcm, start_sample, end_sample = active
    if len(pcm) < context.sample_rate * 2 * _MIN_PARTIAL_MS // 1000:
        return
    start_ms, end_ms = context.sample_bounds(start_sample, end_sample)
    if (
        context.source is Source.MICROPHONE
        and runtime.settings.echo_suppression
        and runtime.echo.has_reference(context.session_id)
        and runtime.echo.judge(context.session_id, start_ms, end_ms, pcm).echo
    ):
        # What is being heard is the speakers. The final will be dropped, so
        # do not flash its text on screen first.
        return
    if context.partial_segment_id is None:
        context.partial_segment_id = _segment_id()
    context.last_partial_at = now
    context.partial_task = asyncio.create_task(
        _run_partial(runtime, context, pcm, start_ms, end_ms, context.partial_segment_id)
    )


async def _run_partial(
    runtime: ServiceRuntime,
    context: _StreamContext,
    pcm: bytes,
    start_ms: int,
    end_ms: int,
    segment_id: str,
) -> None:
    began = time.monotonic()
    try:
        event = await _transcribe_pcm_chunk(
            runtime,
            context.session_id,
            context,
            {"type": "audio.chunk", "start_ms": start_ms, "end_ms": end_ms},
            pcm,
            segment_id=segment_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a partial is disposable; the final still comes
        runtime.partial_error = f"{type(exc).__name__}: {exc}"[:200]
        return
    runtime.partial_error = None
    elapsed = time.monotonic() - began
    base = runtime.settings.partial_interval_ms / 1000.0
    context.partial_interval_s = max(base, elapsed * 1.5)
    if segment_id in context.finalized_partials or context.partial_segment_id != segment_id:
        return
    event = _correct(runtime.vocabulary.current(), replace(event, is_final=False))
    if not event.text.strip():
        return
    payload = event.to_dict()
    _record_ui_event(runtime, context.session_id, {**payload, "type": "transcript"})
    context.outbox.append({"type": "transcript.partial", "event": payload})


async def _flush_outbox(websocket: WebSocket, runtime: ServiceRuntime, context: _StreamContext) -> None:
    if not context.outbox:
        return
    pending, context.outbox = context.outbox, []
    for message in pending:
        event = message.get("event") or {}
        if (
            message.get("type") == "transcript.partial"
            and event.get("segment_id") in context.finalized_partials
        ):
            # Its final already went out; a stale partial must not replace it.
            continue
        await websocket.send_json(message)


async def _drain_background(websocket: WebSocket, runtime: ServiceRuntime, context: _StreamContext) -> None:
    """Let in-flight translations finish before the socket closes.

    Bounded: a translation that has not returned in a few seconds is written
    to the transcript when it does, but the client stops waiting for it.
    """
    tasks = [task for task in context.background if not task.done()]
    if tasks:
        await asyncio.wait(tasks, timeout=5.0)
    await _flush_outbox(websocket, runtime, context)


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def _maybe_schedule_translation(
    runtime: ServiceRuntime, context: _StreamContext, event: TranscriptEvent
) -> None:
    if event.session_id not in runtime.translate_sessions or runtime.translator is None:
        return
    if not event.text.strip():
        return
    context.keep(asyncio.create_task(_run_translation(runtime, context, event)))


async def _run_translation(
    runtime: ServiceRuntime, context: _StreamContext, event: TranscriptEvent
) -> None:
    source, target = other_language(event.language, event.text)
    try:
        text = await asyncio.to_thread(runtime.translate_text, event.text, source, target)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - translation is optional; the transcript is not
        runtime.translation_error = f"{type(exc).__name__}: {exc}"[:200]
        return
    if not text.strip():
        return
    runtime.translation_error = None
    try:
        await asyncio.to_thread(runtime.sessions.translate, event, target, text)
    except (InvalidSessionTransition, KeyError, ValueError, OSError):
        # The session closed in the meantime; the caption still gets it.
        pass
    entry = {
        "type": "translation",
        "session_id": event.session_id,
        "segment_id": event.segment_id,
        "source": event.source.value,
        "start_ms": event.start_ms,
        "end_ms": event.end_ms,
        "language": target,
        "text": text,
        "is_final": True,
    }
    _record_ui_event(runtime, event.session_id, entry)
    context.outbox.append(
        {
            "type": "transcript.translation",
            "event": {"segment_id": event.segment_id, "text": text, "language": target},
        }
    )


def _shutdown(runtime: ServiceRuntime) -> None:
    """Close every open session so no transcript part is left unterminated."""
    runtime.shutting_down = True
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
    # Give any job already running a moment to finish and clear its scratch
    # directory, rather than abandoning it at process exit.
    runtime.diarization.shutdown()
    translator = runtime.translator
    if translator is not None and translator.is_loaded:
        try:
            translator.unload()
        except Exception:  # noqa: BLE001 - exiting anyway
            pass


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
        "last_session": _last_session_view(runtime),
        "translation": {
            "requested": bool(session_id and session_id in runtime.translate_sessions),
            "available": runtime.translation_available,
            "error": runtime.translation_error,
        },
    }


def _last_session_view(runtime: ServiceRuntime) -> dict[str, Any] | None:
    session_id = runtime.last_session_id
    if session_id is None:
        return None
    directory = _session_directory(runtime, session_id)
    return {
        "session_id": session_id,
        "directory": str(directory),
        "transcript_parts": sorted(
            path.name for path in directory.glob("transcript-part-[0-9][0-9][0-9].md")
        ) if directory.is_dir() else [],
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


def _session_elapsed_ms(runtime: ServiceRuntime, session_id: str) -> int:
    """Where "now" falls on the session timeline, for events without audio."""
    started = runtime.session_started_at.get(session_id)
    if started is None:
        return 0
    return max(0, round((time.monotonic() - started) * 1000.0))


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
    context: _StreamContext,
    header: dict[str, Any],
    pcm: bytes,
) -> None:
    session = runtime.sessions.get(session_id)
    if not session.config.audio_saved:
        return
    start = context.start
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
        channel=context.channel,
    )


def _finalize_audio(runtime: ServiceRuntime, session: Session) -> None:
    session_id = session.config.session_id
    runtime.last_session_id = session_id
    runtime.session_started_at.pop(session_id, None)
    runtime.echo.forget(session_id)
    writer = runtime.audio_writers.pop(session_id, None)
    if writer is not None:
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
                for pattern in (f"{source.value}-part-*.wav", f"{source.value}-*-part-*.wav"):
                    for path in writer.session_dir.glob(pattern):
                        path.unlink()
    # Dispatch even when no writer was ever created. A session that asked for
    # diarization and captured no audio should say so as a typed failure, not
    # silently report that diarization was never requested.
    _dispatch_diarization(runtime, session)


def _dispatch_diarization(runtime: ServiceRuntime, session: Session) -> None:
    """Start diarization now that the session is finished on disk.

    This is called from `_finalize_audio`, which every Stop path goes through,
    and only after the transcript writer has been closed and the audio writer
    has committed its WAV headers. That ordering is the trigger condition: a
    job dispatched earlier would diarize a partial recording.

    Dispatch returns immediately, so Stop never waits for a model, and any
    failure lands in the job rather than in the caller's response.
    """
    if not session.config.diarize or runtime.shutting_down:
        # A job started as the process exits would be killed mid-inference and
        # leave its scratch directory behind. The transcript and audio are
        # already safe, so diarization can be requested again later.
        return
    try:
        runtime.diarization.request(
            _session_directory(runtime, session.config.session_id),
            session.config.session_id,
        )
    except Exception:  # noqa: BLE001 - Stop must succeed even if dispatch cannot
        pass


def _diarization_requested(
    runtime: ServiceRuntime,
    session_id: str,
    sources: tuple[Source, ...],
    retention: dict[str, bool],
) -> bool:
    """Whether this session should diarize, given what it is actually keeping.

    The flag is dropped rather than honoured when the retention the capture
    client negotiated cannot support it. `SessionConfig` would otherwise reject
    the whole session, which would fail a recording over an optional extra.
    """
    if session_id not in runtime.pending_diarization:
        return False
    return bool(
        retention.get("audio_saved")
        and retention.get("save_separate_audio", True)
        and Source.MICROPHONE in sources
    )


def _session_directory(runtime: ServiceRuntime, session_id: str) -> Path:
    try:
        config = runtime.sessions.get(session_id).config
    except KeyError:
        return runtime.settings.output_root / session_id
    return config.output_directory or runtime.settings.output_root / session_id


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
        # The final takes over the id its partials were shown under, so the
        # caption is replaced in place rather than duplicated beneath itself.
        segment_id = context.partial_segment_id or _segment_id()
        context.cancel_partial()
        context.finalized_partials.append(segment_id)
        if (
            context.source is Source.MICROPHONE
            and runtime.settings.echo_suppression
            and runtime.echo.has_reference(context.session_id)
        ):
            verdict = await asyncio.to_thread(
                runtime.echo.judge, context.session_id, chunk_start, chunk_end, chunk.pcm
            )
            if verdict.echo:
                context.echo_suppressed += 1
                await websocket.send_json(
                    {
                        "type": "audio.echo_suppressed",
                        "stream_id": context.stream_id,
                        "segment_id": segment_id,
                        "start_ms": chunk_start,
                        "end_ms": chunk_end,
                        **verdict.to_dict(),
                    }
                )
                published += 1
                continue
        header = {
            "type": "audio.chunk",
            "start_ms": chunk_start,
            "end_ms": chunk_end,
            "finalization_reason": chunk.reason,
        }
        event, elapsed_ms = await _timed_transcription(
            runtime, context.session_id, context, header, chunk.pcm, segment_id=segment_id
        )
        result = await _publish_event(websocket, runtime, event)
        if result is not None:
            published += 1
            _maybe_schedule_translation(runtime, context, result)
        await _report_lag(websocket, context, chunk_end - chunk_start, elapsed_ms)
    return published


async def _timed_transcription(
    runtime: ServiceRuntime,
    session_id: str,
    context: _StreamContext,
    header: dict[str, Any],
    pcm: bytes,
    *,
    segment_id: str | None = None,
) -> tuple[TranscriptEvent, float]:
    began = time.monotonic()
    event = await _transcribe_pcm_chunk(
        runtime, session_id, context, header, pcm, segment_id=segment_id
    )
    return event, (time.monotonic() - began) * 1000.0


async def _transcribe_pcm_chunk(
    runtime: ServiceRuntime,
    session_id: str,
    context: _StreamContext,
    header: dict[str, Any],
    pcm: bytes,
    *,
    segment_id: str | None = None,
):
    start = context.start
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
        event = await runtime.provider.transcribe_file(
            temporary.name,
            TranscriptionOptions(
                session_id=session_id,
                segment_id=segment_id or _segment_id(),
                stream_id=str(start.get("stream_id", f"{source.value}-1")),
                source=source,
                start_ms=start_ms,
                end_ms=end_ms,
                language=_stream_language(start),
                context=runtime.vocabulary.current().context,
            ),
        )
    if context.channel and (event.channel != context.channel or event.channel_name != context.channel_name):
        event = replace(event, channel=context.channel, channel_name=context.channel_name)
    return event


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


def _stream_channel(start: dict[str, Any]) -> tuple[str | None, str | None]:
    """Read the per-application channel a system stream isolates, if any."""
    channel = start.get("channel")
    name = start.get("channel_name")
    if channel is not None and (not isinstance(channel, str) or not channel.strip()):
        raise ValueError("channel must be a non-empty string")
    if name is not None and not isinstance(name, str):
        raise ValueError("channel_name must be a string")
    channel = channel.strip() if isinstance(channel, str) else None
    name = " ".join(name.split()) if isinstance(name, str) and name.strip() else None
    return channel, (name if channel else None)


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
    for name, field_name in names.items():
        if name in requested:
            if not isinstance(requested[name], bool):
                raise ValueError(f"retention.{name} must be true or false")
            values[field_name] = requested[name]
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
        settings = dict(retention or {})
        session = runtime.sessions.create(
            SessionConfig(
                session_id=session_id,
                sources=sources or (source,),
                applications=applications,
                provider=runtime.provider.name,
                model=runtime.active_model_id,
                diarize=_diarization_requested(runtime, session_id, sources or (source,), settings),
                **settings,
            )
        )
    if source not in session.config.sources:
        raise HTTPException(
            status_code=409,
            detail=f"source {source.value} is not enabled for this session",
        )
    if session.state is SessionState.CREATED:
        runtime.sessions.start(session_id)
        runtime.session_started_at[session_id] = time.monotonic()
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
