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
import re
import threading
import math
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
import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from clients.python_capture.vad import AudioChunk, EnergyVadChunker, VadConfig
from voxink_core.artifacts import discover_transcripts, read_transcript, read_transcript_text
from voxink_core.audit import AUDIT_FILENAME, AuditLog
from voxink_core.diarization.jobs import DiarizationJobManager
from voxink_core.echo import EchoDetector
from voxink_core.grants import (
    GRANT_SCOPES,
    GRANTS_FILENAME,
    TOKEN_PREFIX,
    Grant,
    GrantError,
    GrantStore,
)
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
from voxink_core.diarization.artifacts import read_document, rename_speaker
from voxink_core.diarization.models import DiarizationError
from voxink_core.diarization.live import LiveSpeakerIdentifier
from voxink_core.speakers import SPEAKERS_FILENAME, SpeakerRegistry, SpeakerRegistryError
from voxink_core.sessions import (
    InvalidSessionTransition,
    Session,
    SessionManager,
    SessionState,
)
from voxink_core.transcripts import OverlapDeduplicator
from voxink_core.translation import QwenLocalTranslator, TranslationProvider, other_language
from voxink_core.vocabulary import MAX_FILE_BYTES, Vocabulary, VocabularyFile
from voxink_dev.diart_runtime import DEFAULT_RUNTIME
from services.transcription.diart_bridge import DiartBridge, LiveTurn, availability as diart_availability


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


# A capture-helper status line: `code: message`.
_NOTICE_LINE = re.compile(r"^[a-z][a-z0-9_]*: ")


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
    # How long a session stays open after its last stream disconnects without
    # a stream.stop, so a helper whose socket dropped can rejoin. A recording
    # must not be finalized, and diarized, over a transient socket failure.
    stream_grace_seconds: float = 15.0
    # Local instruct model used to translate finalized segments. Optional:
    # absent, translation is reported unavailable and never requested.
    translation_model_path: Path | None = PROJECT_ROOT / "models" / "Qwen3-0.6B"
    # Owner-only state: agent grants, the audit trail, and remembered voices.
    state_root: Path = Path.home() / "Library" / "Application Support" / "VoxInk"
    # Cosine similarity a session speaker must reach to be called a known voice.
    speaker_threshold: float = 0.62
    # Name a live caption after a remembered voice once a stream has carried a
    # few seconds of it. Needs the diarization model and an enrolled voice.
    live_speaker_names: bool = True
    # Disposable, offline Diart environment; activated only by the UI checkbox.
    live_diarization_runtime: Path = DEFAULT_RUNTIME


class GrantIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = "agent"
    scopes: list[str]
    applications: list[str] = Field(default_factory=list)
    max_duration_s: int | None = None
    retain_audio: bool = False
    expires_in_s: int | None = None
    single_use: bool = False


class GrantRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client: str = "agent"
    reason: str = ""
    scopes: list[str]
    applications: list[str] = Field(default_factory=list)
    max_duration_s: int | None = None
    retain_audio: bool = False


class GrantApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    expires_in_s: int | None = None
    single_use: bool = False
    max_duration_s: int | None = None


class GrantClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_secret: str


class SpeakerNameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Empty clears the name.
    name: str = ""
    #: Store this session's voice embedding under the name for later sessions.
    remember: bool = False


class KnownSpeakerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


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
    live_diarization: bool = False
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
        # Voices the user asked to remember. Opt-in per voice, local only.
        self.speakers = SpeakerRegistry(
            settings.state_root / SPEAKERS_FILENAME, threshold=settings.speaker_threshold
        )
        self.diarization = DiarizationJobManager(
            model_path=settings.diarization_model_path,
            device=settings.diarization_device,
            aligner_path=settings.diarization_aligner_path,
            enabled=settings.diarization_enabled,
            registry=self.speakers,
        )
        self.live_speakers = (
            LiveSpeakerIdentifier(
                self.speakers,
                model_path=settings.diarization_model_path,
                device=settings.diarization_device,
            )
            if settings.live_speaker_names and settings.diarization_enabled
            else None
        )
        self.translator = translator or _default_translator(settings)
        # Serializes translation across streams; the model is not re-entrant.
        self.translation_lock = threading.Lock()
        self.translation_error: str | None = None
        # Why the last partial caption failed, if it did; a diagnostic only.
        self.partial_error: str | None = None
        self.echo = EchoDetector()
        # Session id to stream id to (level, monotonic time) of its last frame,
        # so the UI can show that audio is arriving before any caption does.
        self.levels: dict[str, dict[str, tuple[Source, float, float]]] = {}
        self.audio_writers: dict[str, AudioRetentionWriter] = {}
        # Stream id to the connection that owns it. A reconnecting helper reuses
        # its stream id, and only the connection that registered it last may
        # release it; the registry, grace timers, and session stop share a lock
        # because the timers expire on their own thread.
        self.active_streams: dict[str, dict[str, object]] = {}
        self.paused_streams: dict[str, set[str]] = {}
        self.stream_grace_timers: dict[str, threading.Timer] = {}
        self.stream_lock = threading.Lock()
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
        self.native_starting = False
        self.native_start_task: asyncio.Task | None = None
        self.live_diarization_session_id: str | None = None
        self.live_diarization_state = "disabled"
        self.live_diarization_error: str | None = None
        self.live_diarizers: dict[str, DiartBridge] = {}
        self.native_error: str | None = None
        # The helper's latest status line while it runs, for the UI to show.
        self.native_notice: str | None = None
        self.native_stderr: deque[str] = deque(maxlen=200)
        self.native_stderr_reader: threading.Thread | None = None
        self.shutting_down = False
        # Sessions the UI asked to diarize or translate. The capture helper
        # builds the session from its own stream message and knows nothing
        # about either, so the request is remembered here and applied when
        # that session is created, rather than widening the native protocol.
        self.pending_diarization: set[str] = set()
        self.translate_sessions: set[str] = set()
        self.grants = GrantStore(settings.state_root / GRANTS_FILENAME)
        self.audit = AuditLog(settings.state_root / AUDIT_FILENAME)
        # The grant that started the running capture, and the timer that ends
        # it when the grant's maximum duration is reached.
        self.capture_grant: Grant | None = None
        self.capture_deadline: asyncio.Task[None] | None = None

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
    live_diarizer: DiartBridge | None = None
    live_last_speakers: tuple[str, ...] | None = None
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
        path = request.url.path
        public_ui = path == "/ui" or path.startswith("/ui/")
        # Identify the caller before deciding whether this path needs one, so a
        # public route such as `/v1/grants/self` can still describe a credential
        # that was presented instead of reporting every caller as anonymous.
        header = request.headers.get("authorization")
        if _authorized(header, runtime.settings.auth_secret):
            request.state.principal = _Principal.owner()
        else:
            grant = runtime.grants.authenticate(_bearer(header)) if _bearer(header) else None
            request.state.principal = (
                _Principal.for_grant(grant) if grant is not None else _Principal.anonymous()
            )
        if public_ui or path in _PUBLIC_PATHS or _is_consent_path(request.method, path):
            return await call_next(request)
        if request.state.principal.kind != "anonymous":
            return await call_next(request)
        # An agent that holds nothing is not stuck: the consent flow is open to
        # it, so name the way out rather than refusing in prose (§14.5).
        return JSONResponse(
            status_code=401,
            content={
                "detail": {
                    "code": "permission_denied",
                    "message": (
                        "no valid local credential: this call needs the owner's approval"
                        if header
                        else "no local credential was presented"
                    ),
                    "remediation": (
                        "Ask the owner for a grant: POST /v1/grants/requests with the scopes you "
                        "need, have the owner approve it in VoxInk, then POST "
                        "/v1/grants/requests/{request_id}/claim. Both are open to callers holding "
                        "no credential."
                    ),
                    "consent": {
                        "request": "POST /v1/grants/requests",
                        "claim": "POST /v1/grants/requests/{request_id}/claim",
                        "scopes": "GET /v1/grants/scopes",
                        "self": "GET /v1/grants/self",
                    },
                }
            },
        )

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
                "grants": True,
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
    async def select_model(http: Request, request: ModelSelectionRequest) -> dict[str, Any]:
        principal = _require(runtime, http, "models.switch")
        package = runtime.models.get(request.model)
        if package is None:
            raise HTTPException(status_code=404, detail="local model is not installed")
        runtime.audit.record(
            "models.select", client=principal.name, result="requested",
            request_id=http.headers.get("x-voxink-request-id"), scope="models.switch", model=request.model,
        )
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
    async def write_vocabulary(http: Request, request: VocabularyUpdateRequest) -> dict[str, Any]:
        principal = _require(runtime, http, "vocabulary.write")
        runtime.audit.record(
            "vocabulary.replace", client=principal.name, result="ok",
            request_id=http.headers.get("x-voxink-request-id"), scope="vocabulary.write",
            bytes=len(request.content.encode("utf-8")),
        )
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
            "live_speaker_names": runtime.live_speakers is not None,
            "live_diarization": _live_diarization_view(runtime),
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
        principal: _Principal = request.state.principal
        request_id = request.headers.get("x-voxink-request-id")
        executable = _native_capture_executable(runtime)
        if runtime.native_starting:
            raise HTTPException(status_code=409, detail="native capture is already starting")
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
        # What this capture needs permission for, in the language of grants.
        needed = []
        if Source.MICROPHONE in sources:
            needed.append("capture.microphone")
        if Source.SYSTEM in sources:
            needed.append("capture.applications" if applications else "capture.system")
        if options.retain_audio:
            needed.append("audio.retain")
        grant = principal.grant
        if grant is not None:
            missing = [scope for scope in needed if not grant.allows(scope)]
            if missing:
                runtime.audit.record(
                    "capture.start", client=principal.name, result="approval_required",
                    request_id=request_id, scope=",".join(missing), sources=[s.value for s in sources],
                )
                raise _approval_required(runtime, principal, missing, applications, options)
            if grant.applications and (
                not applications or any(app not in grant.applications for app in applications)
            ):
                runtime.audit.record(
                    "capture.start", client=principal.name, result="permission_denied",
                    request_id=request_id, scope="capture.applications", applications=list(applications),
                )
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "permission_denied",
                        "message": "this grant may only capture: " + ", ".join(grant.applications),
                    },
                )
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
        if options.live_diarization:
            if Source.MICROPHONE not in sources:
                raise HTTPException(status_code=422, detail="live speakers requires microphone capture")
            if principal.kind != "owner":
                raise HTTPException(status_code=403, detail="experimental live speakers is owner-only")
            info = diart_availability(runtime.settings.live_diarization_runtime)
            if not info["available"]:
                raise HTTPException(status_code=409, detail=f"Live speakers unavailable: {info['error']}")
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
        runtime.live_diarization_session_id = session_id if options.live_diarization else None
        runtime.live_diarization_error = None
        runtime.live_diarization_state = "starting" if options.live_diarization else "disabled"
        if options.live_diarization:
            bridge = DiartBridge(
                runtime.settings.live_diarization_runtime, device=runtime.settings.diarization_device,
                session_id=session_id, stream_id="microphone-1",
            )
            runtime.live_diarizers[session_id] = bridge
            runtime.native_starting = True
            runtime.native_start_task = asyncio.current_task()
            try:
                await bridge.start()
                runtime.live_diarization_state = "ready"
            except asyncio.CancelledError as exc:
                _close_live_diarization(runtime, session_id)
                raise HTTPException(status_code=409, detail="Live speakers startup was cancelled") from exc
            except Exception as exc:
                _close_live_diarization(runtime, session_id, error=str(exc))
                raise HTTPException(status_code=503, detail=f"Live speakers failed to start: {exc}") from exc
            except BaseException:
                _close_live_diarization(runtime, session_id, error="Live speakers startup was cancelled")
                raise
            finally:
                runtime.native_starting = False
                runtime.native_start_task = None
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
            _close_live_diarization(runtime, session_id, error=str(exc))
            raise HTTPException(status_code=503, detail=f"cannot start native capture: {exc}") from exc
        runtime.native_process = process
        runtime.native_session_id = session_id
        runtime.capture_grant = grant
        if grant is not None:
            if grant.single_use:
                runtime.grants.consume(grant.id)
            if grant.max_duration_s:
                _cancel_deadline(runtime)
                runtime.capture_deadline = asyncio.create_task(
                    _enforce_deadline(runtime, session_id, grant.max_duration_s, principal.name)
                )
        runtime.audit.record(
            "capture.start", client=principal.name, result="ok", request_id=request_id,
            session_id=session_id, scope=",".join(needed), sources=[s.value for s in sources],
            applications=list(applications), retain_audio=options.retain_audio,
            translate=options.translate, max_duration_s=grant.max_duration_s if grant else None,
        )
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
        runtime.native_notice = None
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
    async def mark_native_capture(http: Request, body: MarkRequest | None = None) -> dict[str, Any]:
        """Drop a timestamped marker into the transcript being written."""
        principal = _require_any(runtime, http, _CAPTURE_SCOPES)
        process = runtime.native_process
        session_id = runtime.native_session_id
        if process is None or process.poll() is not None or session_id is None:
            raise HTTPException(status_code=409, detail="no native capture session is running")
        label = (body or MarkRequest()).label
        runtime.audit.record(
            "capture.mark", client=principal.name, result="ok",
            request_id=http.headers.get("x-voxink-request-id"), session_id=session_id,
        )
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
    async def control_native_capture(http: Request, action: str) -> dict[str, Any]:
        if action not in {"pause", "resume", "stop"}:
            raise HTTPException(status_code=404, detail="unknown native capture action")
        principal = _require_any(runtime, http, _CAPTURE_SCOPES)
        if action == "stop" and runtime.native_starting:
            task = runtime.native_start_task
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, HTTPException):
                    pass
            runtime.native_capture_state = "ready"
            return _native_capture_view(runtime, after=0)
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
        else:
            runtime.native_capture_state = "stopping"
            _cancel_deadline(runtime)
            await asyncio.to_thread(_stop_native_capture, runtime)
        runtime.audit.record(
            f"capture.{action}", client=principal.name, result="ok",
            request_id=http.headers.get("x-voxink-request-id"), session_id=runtime.native_session_id,
        )
        return _native_capture_view(runtime, after=0)

    # -- agent grants (§14.5) ---------------------------------------------------

    @app.get("/v1/grants/scopes")
    async def grant_scopes() -> dict[str, Any]:
        return {"scopes": list(GRANT_SCOPES)}

    @app.get("/v1/grants/self")
    async def grant_self(http: Request) -> dict[str, Any]:
        """What the caller may actually do, as opposed to what a grant can carry.

        A caller cannot learn its own standing from `/v1/grants/scopes`, which
        is only the catalog; reading the catalog as one's own permissions is
        the mistake this route exists to prevent. Public, because describing a
        credential to the caller that presented it discloses nothing further.
        """
        principal = _principal(http)
        grant = principal.grant
        return {
            "credential": "owner" if principal.kind == "owner" else ("grant" if grant else "none"),
            # The owner credential authorizes every scope; anonymous, none.
            "scopes": list(GRANT_SCOPES) if principal.kind == "owner" else (list(grant.scopes) if grant else []),
            "grant_id": grant.id if grant else None,
            "expires_at": grant.expires_at if grant else None,
            "applications": list(grant.applications) if grant else [],
            "retain_audio": bool(grant.retain_audio) if grant else False,
            "requestable_scopes": list(GRANT_SCOPES),
        }

    @app.get("/v1/grants")
    async def list_grants(http: Request) -> dict[str, Any]:
        _require_owner(http)
        return {"grants": [grant.to_dict() for grant in runtime.grants.list()]}

    @app.post("/v1/grants", status_code=201)
    async def issue_grant(http: Request, body: GrantIssueRequest) -> dict[str, Any]:
        """Issue a grant directly. Owner only: the CLI cannot mint its own."""
        _require_owner(http)
        try:
            grant, token = runtime.grants.issue(
                label=body.label,
                scopes=body.scopes,
                applications=body.applications,
                max_duration_s=body.max_duration_s,
                retain_audio=body.retain_audio,
                expires_in_s=body.expires_in_s,
                single_use=body.single_use,
            )
        except GrantError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.audit.record(
            "grants.issue", client="owner", result="ok", scope=",".join(grant.scopes),
            grant_id=grant.id, label=grant.label, max_duration_s=grant.max_duration_s,
        )
        # The token is returned exactly once; only its hash is stored.
        return {"grant": grant.to_dict(), "token": token}

    @app.delete("/v1/grants/{grant_id}")
    async def revoke_grant(http: Request, grant_id: str) -> dict[str, Any]:
        _require_owner(http)
        grant = runtime.grants.revoke(grant_id)
        if grant is None:
            raise HTTPException(status_code=404, detail="unknown grant")
        if runtime.capture_grant is not None and runtime.capture_grant.id == grant_id:
            # A revoked grant's recording stops with it.
            _cancel_deadline(runtime)
            await asyncio.to_thread(_stop_native_capture, runtime)
        runtime.audit.record("grants.revoke", client="owner", result="ok", grant_id=grant_id)
        return {"grant": grant.to_dict()}

    @app.get("/v1/grants/requests")
    async def list_grant_requests(http: Request) -> dict[str, Any]:
        _require_owner(http)
        return {"requests": [item.to_dict() for item in runtime.grants.pending()]}

    @app.post("/v1/grants/requests", status_code=201)
    async def create_grant_request(body: GrantRequestBody) -> dict[str, Any]:
        """An agent asks for access. Anyone on loopback may ask; only the owner answers."""
        try:
            pending, claim_secret = runtime.grants.request(
                client=body.client,
                reason=body.reason,
                scopes=body.scopes,
                applications=body.applications,
                max_duration_s=body.max_duration_s,
                retain_audio=body.retain_audio,
            )
        except GrantError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.audit.record(
            "grants.request", client=f"request:{pending.client}", result="pending",
            request_id=pending.id, scope=",".join(pending.scopes),
        )
        return {"request": pending.to_dict(), "claim_secret": claim_secret}

    @app.post("/v1/grants/requests/{request_id}/approve")
    async def approve_grant_request(
        http: Request, request_id: str, body: GrantApproveRequest | None = None
    ) -> dict[str, Any]:
        _require_owner(http)
        options = body or GrantApproveRequest()
        try:
            grant = runtime.grants.approve(
                request_id,
                label=options.label,
                expires_in_s=options.expires_in_s,
                single_use=options.single_use,
                max_duration_s=options.max_duration_s,
            )
        except GrantError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        runtime.audit.record(
            "grants.approve", client="owner", result="ok", request_id=request_id,
            scope=",".join(grant.scopes), grant_id=grant.id, max_duration_s=grant.max_duration_s,
        )
        return {"grant": grant.to_dict()}

    @app.post("/v1/grants/requests/{request_id}/deny")
    async def deny_grant_request(http: Request, request_id: str) -> dict[str, Any]:
        _require_owner(http)
        try:
            pending = runtime.grants.deny(request_id)
        except GrantError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        runtime.audit.record("grants.deny", client="owner", result="ok", request_id=request_id)
        return {"request": pending.to_dict()}

    @app.post("/v1/grants/requests/{request_id}/claim")
    async def claim_grant_request(request_id: str, body: GrantClaimRequest) -> dict[str, Any]:
        """Hand the approved token to the holder of the claim secret, once."""
        try:
            pending, token = runtime.grants.claim(request_id, body.claim_secret)
        except GrantError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if token is not None:
            runtime.audit.record(
                "grants.claim", client=f"request:{pending.client}", result="ok",
                request_id=request_id, grant_id=pending.grant_id,
            )
        return {"request": pending.to_dict(), "token": token}

    @app.get("/v1/audit")
    async def read_audit(http: Request, limit: int = 100) -> dict[str, Any]:
        _require_owner(http)
        return {"entries": runtime.audit.tail(max(1, min(limit, 1000)))}

    @app.post("/v1/ui/actions/reveal")
    async def reveal_session(http: Request, body: RevealRequest | None = None) -> dict[str, Any]:
        """Open a session's folder in Finder, or the output root with no session."""
        _require_owner(http)
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

    # -- speakers who keep their names ------------------------------------------

    @app.get("/v1/speakers")
    async def known_speakers(http: Request) -> dict[str, Any]:
        _require_owner(http)
        return {
            "speakers": [item.to_dict() for item in runtime.speakers.list()],
            "threshold": runtime.speakers.threshold,
            "path": str(runtime.speakers.path),
        }

    @app.post("/v1/speakers/{registry_id}")
    async def rename_known_speaker(http: Request, registry_id: str, body: KnownSpeakerRequest) -> dict[str, Any]:
        _require_owner(http)
        try:
            known = runtime.speakers.rename(registry_id, body.name)
        except SpeakerRegistryError as exc:
            raise HTTPException(status_code=404 if "unknown" in str(exc) else 422, detail=str(exc)) from exc
        runtime.audit.record("speakers.rename", client="owner", result="ok", registry_id=registry_id)
        return {"speaker": known.to_dict()}

    @app.delete("/v1/speakers/{registry_id}")
    async def forget_known_speaker(http: Request, registry_id: str) -> dict[str, Any]:
        _require_owner(http)
        try:
            known = runtime.speakers.forget(registry_id)
        except SpeakerRegistryError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        runtime.audit.record("speakers.forget", client="owner", result="ok", registry_id=registry_id)
        return {"speaker": known.to_dict()}

    @app.post("/v1/sessions/{session_id}/speakers/{speaker_id}")
    async def name_session_speaker(
        http: Request, session_id: str, speaker_id: str, body: SpeakerNameRequest
    ) -> dict[str, Any]:
        """Name one anonymous speaker of a finished session, and maybe remember the voice."""
        _require_owner(http)
        directory = _session_directory(runtime, session_id)
        document = await asyncio.to_thread(read_document, directory)
        if document is None:
            raise HTTPException(status_code=404, detail="this session has no speaker artifacts yet")
        job = runtime.diarization.get(session_id)
        if job is not None and job.state.value == "processing":
            raise HTTPException(status_code=409, detail="speakers are still being identified")
        name = " ".join(body.name.split())
        registry_id: str | None = None
        similarity: float | None = None
        if name and body.remember:
            embedding = ((document.get("speakers") or {}).get(speaker_id) or {}).get("embedding")
            if not embedding:
                raise HTTPException(
                    status_code=409,
                    detail="this speaker has no voice embedding to remember; name it without remembering",
                )
            try:
                known = await asyncio.to_thread(
                    runtime.speakers.enroll,
                    name,
                    embedding,
                    model_id=str((document.get("producer") or {}).get("model_id", "")),
                )
            except SpeakerRegistryError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            registry_id = known.id
            similarity = 1.0
        try:
            updated = await asyncio.to_thread(
                rename_speaker, directory, speaker_id, name or None,
                registry_id=registry_id, similarity=similarity,
            )
        except DiarizationError as exc:
            raise HTTPException(status_code=404 if "unknown speaker" in exc.message else 409, detail=exc.message) from exc
        runtime.audit.record(
            "speakers.name", client="owner", result="ok", session_id=session_id,
            speaker_id=speaker_id, remembered=bool(registry_id),
        )
        return {
            "session_id": session_id,
            "speaker_id": speaker_id,
            "name": name or None,
            "registry_id": registry_id,
            "speakers": {
                identifier: {"name": (info or {}).get("name"), "registry_id": (info or {}).get("registry_id")}
                for identifier, info in (updated.get("speakers") or {}).items()
            },
        }

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
    async def transcripts(http: Request) -> dict[str, Any]:
        """List transcript artifacts under the configured output root."""
        _require(runtime, http, "transcripts.read")
        found = await asyncio.to_thread(discover_transcripts, runtime.settings.output_root)
        return {
            "output_root": str(runtime.settings.output_root),
            "transcripts": [artifacts.to_dict() for artifacts in found],
        }

    @app.get("/v1/transcripts/{session_id}")
    async def transcript(http: Request, session_id: str, part: int | None = None) -> dict[str, Any]:
        _require(runtime, http, "transcripts.read")
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
    async def list_sessions(http: Request) -> dict[str, Any]:
        """List sessions this helper is holding open.

        Completed and interrupted sessions from earlier runs appear here only
        once the durable session registry replaces this in-memory view; until
        then they are discoverable through their transcript artifacts.
        """
        _require(runtime, http, "sessions.read")
        return {
            "sessions": [
                _session_view(session).model_dump() for session in runtime.sessions.list()
            ],
            "durable_registry": False,
        }

    @app.get("/v1/sessions/{session_id}", response_model=SessionView)
    async def get_session(http: Request, session_id: str) -> SessionView:
        _require(runtime, http, "sessions.read")
        return _session_view(_get_session(runtime, session_id))

    @app.post("/v1/sessions/{session_id}/{action}", response_model=SessionView)
    async def control_session(http: Request, session_id: str, action: str) -> SessionView:
        if action not in {"start", "pause", "resume", "stop"}:
            raise HTTPException(status_code=404, detail="unknown session action")
        _require_owner(http)
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
            with runtime.stream_lock:
                _cancel_stream_grace(runtime, session_id)
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
        if event.text.strip():
            # Silence, or a line that only echoed the vocabulary, has no block
            # worth a timestamp in the transcript.
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
                        if context is not None:
                            raise ValueError("stream.start was already received")
                        context = _open_stream(runtime, session_id, control)
                        await websocket.send_json(
                            {
                                "type": "stream.ready",
                                "schema_version": PROTOCOL_VERSION,
                                "session_id": session_id,
                                "partial_results": runtime.settings.partial_captions and context.live_diarizer is None,
                            }
                        )
                    elif message_type in {"audio.frame", "audio.chunk"}:
                        if context is None:
                            raise ValueError("stream.start must be sent before audio")
                        pending_header = control
                    elif message_type == "stream.pause":
                        if context is not None:
                            await _flush_stream_audio(websocket, runtime, context, "pause")
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
                            await _flush_stream_audio(websocket, runtime, context, "stop")
                            if context.live_diarizer is not None:
                                _close_live_diarization(runtime, session_id)
                            context.cancel_partial()
                            await _drain_background(websocket, runtime, context)
                            if _release_stream(runtime, session_id, context, graceful=False):
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
                    _note_level(runtime, context, pcm)
                    if context.live_diarizer is not None:
                        if "start_ms" not in pending_header:
                            raise ValueError("live speakers requires source timestamps on every frame")
                        turns = await context.live_diarizer.feed(
                            pcm, round(float(pending_header["start_ms"])), sequence=pending_header.get("sequence"),
                        )
                        await _send_diart_turns(websocket, runtime, context, turns)
                        await websocket.send_json({"type": "audio.accepted", "sequence": pending_header.get("sequence")})
                    elif pending_header["type"] == "audio.frame":
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
        except (RuntimeError, OSError, TimeoutError) as exc:
            if context is None or context.live_diarizer is None:
                raise
            _close_live_diarization(runtime, session_id, error=str(exc))
            await websocket.send_json({"type": "error", "code": "live_diarization_failed", "message": str(exc)})
            await websocket.close(code=1011)
        except HTTPException as exc:
            _close_unclaimed_live_diarization(runtime, session_id, str(exc.detail))
            # `_ensure_started_session` is shared with the HTTP routes. On an
            # accepted WebSocket its HTTPException would make Starlette try to
            # send an HTTP response, dropping the connection with no diagnosis.
            await websocket.send_json(
                {"type": "error", "code": "session_unavailable", "message": str(exc.detail)}
            )
            await websocket.close(code=1008)
        except (InvalidSessionTransition, KeyError, TypeError, ValueError) as exc:
            _close_unclaimed_live_diarization(runtime, session_id, str(exc))
            if context is not None and context.live_diarizer is not None:
                _close_live_diarization(runtime, session_id, error=str(exc))
            await websocket.send_json({"type": "error", "code": "invalid_message", "message": str(exc)})
            await websocket.close(code=1003)
        finally:
            if context is not None:
                if context.live_diarizer is not None:
                    _close_live_diarization(runtime, session_id, error="Live speaker stream disconnected; start a new recording to retry")
                context.cancel_partial()
                # The connection went away without a stream.stop. That is not
                # a Stop: the session is held for a grace period so the helper
                # can rejoin, and the deduplicator state stays with the stream
                # only if this connection still owned it.
                if _release_stream(runtime, session_id, context, graceful=True):
                    runtime.dedup.reset(context.session_id, context.stream_id)
                    runtime.stream_ends.pop((context.session_id, context.stream_id), None)

    ui_root = PROJECT_ROOT / "frontend" / "static-dist"
    if (ui_root / "index.html").is_file():
        app.mount("/ui", StaticFiles(directory=ui_root, html=True), name="local-ui")

    return app


def _authorized(header: str | None, secret: str | None) -> bool:
    """Compare bearer credentials without leaking timing or accepting empty secrets."""
    if secret is None:
        # No credential configured (direct debugging): everything is the owner,
        # except a caller presenting a grant token, who is held to its scopes.
        supplied = _bearer(header)
        return not (supplied and supplied.startswith(TOKEN_PREFIX))
    supplied = _bearer(header)
    return bool(supplied) and secrets.compare_digest(supplied, secret)


def _bearer(header: str | None) -> str | None:
    prefix = "Bearer "
    if not header or not header.startswith(prefix):
        return None
    return header[len(prefix) :] or None


# Paths anyone on loopback may call: readiness, the UI bootstrap, and the
# consent flow an agent uses before it has any credential at all.
_PUBLIC_PATHS = frozenset(
    {"/health", "/v1/ui/bootstrap", "/v1/ui/capture/capabilities", "/v1/grants/scopes", "/v1/grants/self"}
)
_CAPTURE_SCOPES = ("capture.microphone", "capture.system", "capture.applications")


def _is_consent_path(method: str, path: str) -> bool:
    if method != "POST":
        return False
    if path == "/v1/grants/requests":
        return True
    return path.startswith("/v1/grants/requests/") and path.endswith("/claim")


@dataclass(frozen=True, slots=True)
class _Principal:
    """Who is calling: the owner (UI or developer credential), a grant, or nobody."""

    kind: str
    grant: Grant | None = None

    @classmethod
    def owner(cls) -> "_Principal":
        return cls("owner")

    @classmethod
    def anonymous(cls) -> "_Principal":
        return cls("anonymous")

    @classmethod
    def for_grant(cls, grant: Grant) -> "_Principal":
        return cls("grant", grant)

    @property
    def name(self) -> str:
        if self.grant is not None:
            return f"grant:{self.grant.id}"
        return self.kind


def _principal(request: Request) -> _Principal:
    return getattr(request.state, "principal", None) or _Principal.anonymous()


def _require_owner(request: Request) -> _Principal:
    principal = _principal(request)
    if principal.kind != "owner":
        raise HTTPException(
            status_code=403,
            detail={"code": "permission_denied", "message": "only the application owner may do this"},
        )
    return principal


def _require(runtime: ServiceRuntime, request: Request, scope: str) -> _Principal:
    """Let the owner through; hold a grant to its scopes."""
    principal = _principal(request)
    if principal.kind == "owner":
        return principal
    if principal.grant is not None and principal.grant.allows(scope):
        return principal
    raise _approval_required(runtime, principal, [scope])


def _require_any(runtime: ServiceRuntime, request: Request, scopes: tuple[str, ...]) -> _Principal:
    principal = _principal(request)
    if principal.kind == "owner":
        return principal
    if principal.grant is not None and any(principal.grant.allows(scope) for scope in scopes):
        return principal
    raise _approval_required(runtime, principal, list(scopes))


def _approval_required(
    runtime: ServiceRuntime,
    principal: _Principal,
    missing: list[str],
    applications: tuple[str, ...] = (),
    options: NativeCaptureRequest | None = None,
) -> HTTPException:
    """Build the §14.5 refusal: a request the owner can approve, never a bypass.

    The pending request is created on the caller's behalf so the UI can show
    it right away; the claim secret goes back only to this caller.
    """
    client = principal.grant.label if principal.grant is not None else "agent"
    detail: dict[str, Any] = {
        "code": "approval_required",
        "message": "this needs the owner's approval: " + ", ".join(missing),
        "missing_scopes": missing,
    }
    try:
        pending, claim_secret = runtime.grants.request(
            client=client,
            reason="requested automatically by a call that lacked the scope",
            scopes=missing,
            applications=applications,
            max_duration_s=None,
            retain_audio=bool(options and options.retain_audio),
        )
    except GrantError as exc:
        detail["message"] += f" (no request was filed: {exc})"
    else:
        detail["request_id"] = pending.id
        detail["claim_secret"] = claim_secret
    return HTTPException(status_code=403, detail=detail)


def _cancel_deadline(runtime: ServiceRuntime) -> None:
    task = runtime.capture_deadline
    runtime.capture_deadline = None
    if task is not None and not task.done():
        task.cancel()


async def _enforce_deadline(runtime: ServiceRuntime, session_id: str, seconds: int, client: str) -> None:
    """Stop a grant-started capture when its maximum duration runs out."""
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        return
    if runtime.native_session_id != session_id:
        return
    process = runtime.native_process
    if process is None or process.poll() is not None:
        return
    runtime.native_capture_state = "stopping"
    await asyncio.to_thread(_stop_native_capture, runtime)
    runtime.audit.record(
        "capture.stop", client="deadline", result="ok", session_id=session_id,
        max_duration_s=seconds, started_by=client,
    )


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
    if runtime.native_starting:
        return True
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
    """Register one source stream and start (or join) its session.

    Under the stream lock end to end: a session whose last stream dropped is
    waiting on a grace timer, and that timer must not fire between the session
    check and this stream's registration.
    """
    source = Source(control.get("source", "microphone"))
    declared_sources = tuple(
        Source(item) for item in control.get("session_sources", [source.value])
    )
    stream_id = str(control["stream_id"])
    channel, channel_name = _stream_channel(control)
    context = _StreamContext(
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
    if source is Source.MICROPHONE and runtime.live_diarization_session_id == session_id:
        audio = control.get("audio", {})
        if (context.sample_rate != 16000 or int(audio.get("channels", 1)) != 1
                or audio.get("encoding", "pcm_s16le") != "pcm_s16le"):
            raise ValueError("live speakers requires mono 16 kHz pcm_s16le audio")
        bridge = runtime.live_diarizers.get(session_id)
        if bridge is None or runtime.live_diarization_state == "failed":
            raise ValueError("live speakers is unavailable for this session; start a new recording")
        if any(getattr(active, "live_diarizer", None) is bridge
               for active in runtime.active_streams.get(session_id, {}).values()):
            raise ValueError("live speakers already has a microphone stream")
        context.live_diarizer = bridge
    with runtime.stream_lock:
        _ensure_started_session(
            runtime,
            session_id,
            source,
            sources=declared_sources,
            applications=_stream_applications(control),
            retention=_stream_retention(control),
        )
        _cancel_stream_grace(runtime, session_id)
        runtime.active_streams.setdefault(session_id, {})[stream_id] = context
        runtime.paused_streams.setdefault(session_id, set()).discard(stream_id)
        runtime.dedup.reset(session_id, stream_id)
        runtime.stream_ends.pop((session_id, stream_id), None)
        if context.live_diarizer is not None:
            runtime.live_diarization_state = "running"
    return context


def _pause_stream(runtime: ServiceRuntime, session_id: str, stream_id: str) -> None:
    """Pause the shared session only once every active stream has paused."""
    paused = runtime.paused_streams.setdefault(session_id, set())
    paused.add(stream_id)
    active = set(runtime.active_streams.get(session_id, {}))
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
    """Apply the user's correction rules to one event's text.

    A line that only reads the biasing terms back is the model transcribing
    its own context on a chunk without speech. Blanking it here lets every
    caller treat it the way it already treats the model's own silence output.
    """
    if not event.text:
        return event
    if vocabulary.is_echo(event.text):
        return replace(event, text="")
    if not vocabulary.corrections:
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
    websocket: WebSocket, runtime: ServiceRuntime, event: TranscriptEvent,
    *, speaker_metadata: dict[str, Any] | None = None,
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
        # The chunk repeated its predecessor's overlap, or only echoed the
        # vocabulary, and carried nothing new.
        return None
    _require_open_session(runtime, event.session_id)
    try:
        runtime.sessions.append(event)
    except (InvalidSessionTransition, KeyError, ValueError) as exc:
        # A paused or otherwise unwritable session is reported without dropping
        # the connection; a stopped one was refused above.
        await websocket.send_json(
            {"type": "error", "code": "transcript_not_stored", "message": str(exc)}
        )
        return event
    runtime.stream_ends[key] = max(previous_end or 0, event.end_ms)
    # Experimental attribution belongs to the live view. The append-only
    # transcript above keeps its text without provisional speaker claims.
    display = {**event.to_dict(), **(speaker_metadata or {})}
    _record_ui_event(runtime, event.session_id, {**display, "type": "transcript"})
    await websocket.send_json({"type": "transcript.final", "event": display})
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
    with runtime.stream_lock:
        for session_id in list(runtime.stream_grace_timers):
            _cancel_stream_grace(runtime, session_id)
    try:
        _stop_native_capture(runtime)
    except Exception:
        # Finalizing the sessions below is the whole point of this function, so a
        # failed helper stop must never abort it.
        runtime.native_capture_state = "failed"
    for session_id in list(runtime.live_diarizers):
        _close_live_diarization(runtime, session_id)
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
        if not runtime.active_streams.get(runtime.native_session_id or ""):
            _close_live_diarization(runtime, runtime.native_session_id or "")
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
        "notice": runtime.native_notice,
        "levels": _levels_view(runtime, session_id),
        "retention": _retention_view(runtime, session_id),
        "last_session": _last_session_view(runtime),
        "live_diarization": _live_diarization_view(runtime),
        "translation": {
            "requested": bool(session_id and session_id in runtime.translate_sessions),
            "available": runtime.translation_available,
            "error": runtime.translation_error,
        },
    }


def _live_diarization_view(runtime: ServiceRuntime) -> dict[str, Any]:
    info = diart_availability(runtime.settings.live_diarization_runtime)
    return {
        "available": info["available"],
        "requested": runtime.live_diarization_session_id is not None,
        "state": runtime.live_diarization_state,
        "error": runtime.live_diarization_error or info["error"],
    }


def _close_live_diarization(runtime: ServiceRuntime, session_id: str, *, error: str | None = None) -> None:
    bridge = runtime.live_diarizers.pop(session_id, None)
    if bridge is None:
        return
    bridge.close()
    if runtime.live_diarization_session_id == session_id:
        runtime.live_diarization_state = "failed" if error else "stopped"
        runtime.live_diarization_error = error


def _close_unclaimed_live_diarization(runtime: ServiceRuntime, session_id: str, error: str) -> None:
    bridge = runtime.live_diarizers.get(session_id)
    if bridge is not None and not any(
        getattr(context, "live_diarizer", None) is bridge
        for context in runtime.active_streams.get(session_id, {}).values()
    ):
        _close_live_diarization(runtime, session_id, error=error)


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
    session_id = runtime.native_session_id
    if process is None:
        runtime.native_capture_state = "ready"
        return
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            if session_id in runtime.live_diarizers:
                # A final bounded turn still needs Diart and shared Qwen. Let
                # the socket acknowledge it before terminating the helper.
                timeout = max(timeout, 90.0)
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
    if runtime.native_process is process:
        runtime.native_capture_state = "ready" if process.returncode == 0 else "failed"
        _read_native_error(runtime, process)
    _close_live_diarization(runtime, session_id or "")


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
                line = line.rstrip("\n")
                runtime.native_stderr.append(line)
                # Status lines are `code: message`; anything else is a crash
                # trace the exit path reports instead.
                if _NOTICE_LINE.match(line):
                    runtime.native_notice = line
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
    session = _require_open_session(runtime, session_id)
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
    _close_live_diarization(runtime, session_id)
    runtime.last_session_id = session_id
    runtime.session_started_at.pop(session_id, None)
    runtime.echo.forget(session_id)
    runtime.levels.pop(session_id, None)
    if runtime.live_speakers is not None:
        runtime.live_speakers.forget(session_id)
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
    runtime: ServiceRuntime,
    session_id: str,
    context: "_StreamContext | None",
    *,
    graceful: bool,
) -> bool:
    """Forget one stream; return whether this connection still owned it.

    A stream id belongs to the connection that registered it last. A helper
    whose socket dropped reopens the same id on a new connection, and the old
    handler's cleanup then runs against a stream that is no longer its own;
    releasing it would stop the session under the live connection.

    With the last stream gone, `stream.stop` finalizes the session now. A bare
    disconnect only starts a grace timer: a socket lost to a keepalive timeout
    is not a Stop, and the helper's next frame is expected to rejoin.
    """
    if context is None:
        return False
    with runtime.stream_lock:
        streams = runtime.active_streams.get(session_id)
        if streams is None or streams.get(context.stream_id) is not context:
            return False
        del streams[context.stream_id]
        paused = runtime.paused_streams.get(session_id)
        if paused is not None:
            paused.discard(context.stream_id)
        if streams:
            return True
        runtime.active_streams.pop(session_id, None)
        runtime.paused_streams.pop(session_id, None)
        _cancel_stream_grace(runtime, session_id)
        if not graceful or runtime.settings.stream_grace_seconds <= 0:
            _stop_abandoned_session(runtime, session_id)
            return True
        timer = threading.Timer(runtime.settings.stream_grace_seconds, _expire_stream_grace)
        timer.args = (runtime, session_id, timer)
        timer.daemon = True
        runtime.stream_grace_timers[session_id] = timer
        timer.start()
        return True


def _expire_stream_grace(
    runtime: ServiceRuntime, session_id: str, timer: threading.Timer
) -> None:
    """Finalize a session no stream rejoined within the grace period."""
    with runtime.stream_lock:
        if runtime.stream_grace_timers.get(session_id) is not timer:
            return
        del runtime.stream_grace_timers[session_id]
        if runtime.active_streams.get(session_id):
            return
        _stop_abandoned_session(runtime, session_id)


def _cancel_stream_grace(runtime: ServiceRuntime, session_id: str) -> None:
    """Callers hold the stream lock."""
    timer = runtime.stream_grace_timers.pop(session_id, None)
    if timer is not None:
        timer.cancel()


def _stop_abandoned_session(runtime: ServiceRuntime, session_id: str) -> None:
    try:
        session = runtime.sessions.get(session_id)
    except KeyError:
        return
    if session.state in {SessionState.RUNNING, SessionState.PAUSED}:
        runtime.sessions.stop(session_id)
        _finalize_audio(runtime, session)


def _require_open_session(runtime: ServiceRuntime, session_id: str):
    """The session a live stream feeds; a stopped one refuses further audio.

    Once a session has stopped, its transcript is complete and may already have
    been diarized. Audio arriving after that must not be retained into it, and
    its captions must not be shown as though they were being kept.
    """
    session = runtime.sessions.get(session_id)
    if session.state is SessionState.STOPPED:
        raise HTTPException(
            status_code=409, detail="session is stopped; audio after Stop is not recorded"
        )
    return session


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


async def _flush_stream_audio(
    websocket: WebSocket, runtime: ServiceRuntime, context: _StreamContext, reason: str,
) -> None:
    if context.live_diarizer is None:
        await _send_chunks(websocket, runtime, context, context.chunker.flush(reason))
    else:
        await _send_diart_turns(websocket, runtime, context, await context.live_diarizer.flush())


async def _send_diart_turns(
    websocket: WebSocket, runtime: ServiceRuntime, context: _StreamContext, turns: Iterable[LiveTurn],
) -> None:
    """Transcribe bounded microphone contexts through the one shared Qwen.

    Text times describe the audio Qwen actually heard. Speaker-turn bounds are
    separate display metadata; no word alignment or durable identity is implied.
    """
    for turn in turns:
        event, elapsed_ms = await _timed_transcription(
            runtime, context.session_id, context,
            {"start_ms": turn.audio_start_ms, "end_ms": turn.audio_end_ms}, turn.pcm,
        )
        if context.live_last_speakers != turn.speakers:
            # Two speakers can each say "okay" in adjacent short turns. Audio
            # padding overlaps, but that does not make the second reply a repeat.
            runtime.dedup.reset(context.session_id, context.stream_id)
        context.live_last_speakers = turn.speakers
        names = []
        for speaker in turn.speakers:
            suffix = speaker.removeprefix("microphone:").removeprefix("speaker")
            names.append(f"Speaker {int(suffix) + 1}" if suffix.isdecimal() else speaker)
        metadata = {
            "speaker": " + ".join(names) or None,
            "speakers": list(turn.speakers),
            "speaker_provisional": True,
            "speaker_overlap": len(turn.speakers) > 1,
            "turn_start_ms": turn.start_ms, "turn_end_ms": turn.end_ms,
            "audio_start_ms": turn.audio_start_ms, "audio_end_ms": turn.audio_end_ms,
        }
        result = await _publish_event(websocket, runtime, replace(event, speaker=None), speaker_metadata=metadata)
        if result is not None:
            _maybe_schedule_translation(runtime, context, result)
        await _report_lag(websocket, context, turn.end_ms - turn.start_ms, elapsed_ms)


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
    if context.live_diarizer is None and runtime.live_speakers is not None and event.text.strip():
        audio = context.start.get("audio", {})
        speaker = await asyncio.to_thread(
            runtime.live_speakers.identify,
            session_id,
            context.stream_id,
            pcm,
            int(audio.get("sample_rate_hz", 16000)),
            int(audio.get("channels", 1)),
        )
        if speaker is not None:
            event = replace(event, speaker=speaker)
    return event, (time.monotonic() - began) * 1000.0


# Frame levels below this are shown as silence; a full-scale frame is 1.0.
_LEVEL_FLOOR_DB = -60.0
# A level older than this is stale: the stream stopped, paused, or dropped.
_LEVEL_TTL_S = 2.0


def _note_level(runtime: ServiceRuntime, context: _StreamContext, pcm: bytes) -> None:
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2")
    if samples.size == 0:
        return
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) / 32768.0
    decibels = 20.0 * math.log10(rms) if rms > 0 else _LEVEL_FLOOR_DB
    level = min(1.0, max(0.0, (decibels - _LEVEL_FLOOR_DB) / -_LEVEL_FLOOR_DB))
    runtime.levels.setdefault(context.session_id, {})[context.stream_id] = (
        context.source, level, time.monotonic()
    )


def _levels_view(runtime: ServiceRuntime, session_id: str | None) -> dict[str, float]:
    """The freshest level per source, zero once its last frame is stale."""
    now = time.monotonic()
    view: dict[str, float] = {}
    for source, level, at in runtime.levels.get(session_id or "", {}).values():
        current = level if now - at <= _LEVEL_TTL_S else 0.0
        view[source.value] = max(view.get(source.value, 0.0), round(current, 3))
    return view


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
