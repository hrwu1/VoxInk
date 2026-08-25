"""FastAPI application for the private, localhost-only VoxInk service."""

from __future__ import annotations

import json
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket
from fastapi.websockets import WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from apps.python_capture.vad import EnergyVadChunker, VadConfig
from voxink.models import SessionConfig, Source, TranscriptionOptions
from voxink.persistence.audio import AudioRetentionWriter
from voxink.providers.base import TranscriptionProvider
from voxink.providers.qwen import QwenLocalProvider
from voxink.sessions import InvalidSessionTransition, SessionManager, SessionState


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    model_path: Path = PROJECT_ROOT / "Qwen3-ASR-1.7B"
    output_root: Path = Path.home() / "Documents" / "VoxInk"
    device: str = "auto"
    max_new_tokens: int = 512


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    sources: list[Source] = Field(default_factory=lambda: [Source.MICROPHONE])
    applications: list[str] = Field(default_factory=list)
    provider: str = "qwen-local"
    model: str = "Qwen3-ASR-1.7B"
    languages: list[str] = Field(default_factory=lambda: ["en", "zh"])
    audio_saved: bool = False
    save_separate_audio: bool = True
    save_combined_audio: bool = False


class SessionView(BaseModel):
    session_id: str
    state: str
    sources: list[str]
    applications: list[str]


class ServiceRuntime:
    def __init__(
        self,
        settings: ServiceSettings,
        *,
        provider: TranscriptionProvider | None = None,
        sessions: SessionManager | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider or QwenLocalProvider(
            settings.model_path,
            device=settings.device,
            max_new_tokens=settings.max_new_tokens,
        )
        self.sessions = sessions or SessionManager(settings.output_root)
        self.audio_writers: dict[str, AudioRetentionWriter] = {}


def create_app(
    settings: ServiceSettings | None = None,
    *,
    provider: TranscriptionProvider | None = None,
    sessions: SessionManager | None = None,
) -> FastAPI:
    runtime = ServiceRuntime(settings or ServiceSettings(), provider=provider, sessions=sessions)
    app = FastAPI(title="VoxInk Local Service", version="0.1.0")
    app.state.runtime = runtime

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "privacy_mode": "local-only",
            "bind_policy": "loopback-only",
            "provider": runtime.provider.name,
            "model_loaded": runtime.provider.is_loaded,
        }

    @app.get("/v1/providers")
    async def providers() -> dict[str, Any]:
        return {
            "providers": [
                {
                    "id": runtime.provider.name,
                    "kind": "local",
                    "enabled": True,
                    "loaded": runtime.provider.is_loaded,
                }
            ]
        }

    @app.post("/v1/sessions", response_model=SessionView, status_code=201)
    async def create_session(request: SessionCreateRequest) -> SessionView:
        session_id = request.session_id or _new_session_id()
        try:
            session = runtime.sessions.create(
                SessionConfig(
                    session_id=session_id,
                    sources=tuple(request.sources),
                    applications=tuple(request.applications),
                    provider=request.provider,
                    model=request.model,
                    languages=tuple(request.languages),
                    audio_saved=request.audio_saved,
                    save_separate_audio=request.save_separate_audio,
                    save_combined_audio=request.save_combined_audio,
                )
            )
        except (ValueError, FileExistsError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _session_view(session)

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
                ),
            )
        runtime.sessions.append(event)
        return {"event": event.to_dict()}

    @app.websocket("/v1/sessions/{session_id}/stream")
    async def stream_audio(websocket: WebSocket, session_id: str) -> None:
        await websocket.accept()
        pending_header: dict[str, Any] | None = None
        start_message: dict[str, Any] | None = None
        chunker: EnergyVadChunker | None = None
        frame_offset_ms = 0
        try:
            while True:
                message = await websocket.receive()
                if message.get("text") is not None:
                    control = _parse_control(message["text"])
                    message_type = control.get("type")
                    if message_type == "stream.start":
                        start_message = control
                        source = Source(control.get("source", "microphone"))
                        chunker = _stream_chunker(control)
                        declared_sources = tuple(
                            Source(item)
                            for item in control.get("session_sources", [source.value])
                        )
                        _ensure_started_session(
                            runtime, session_id, source, sources=declared_sources
                        )
                        await websocket.send_json(
                            {"type": "stream.ready", "schema_version": 1, "session_id": session_id}
                        )
                    elif message_type in {"audio.frame", "audio.chunk"}:
                        if start_message is None:
                            raise ValueError("stream.start must be sent before audio")
                        pending_header = control
                    elif message_type == "stream.pause":
                        if chunker is not None and start_message is not None:
                            await _send_chunks(
                                websocket,
                                runtime,
                                session_id,
                                start_message,
                                chunker.flush("pause"),
                                frame_offset_ms,
                            )
                            chunker.reset()
                        _control_if_possible(runtime, session_id, "pause")
                    elif message_type == "stream.resume":
                        frame_offset_ms = int(control.get("at_ms", frame_offset_ms))
                        _control_if_possible(runtime, session_id, "resume")
                    elif message_type == "stream.stop":
                        if chunker is not None and start_message is not None:
                            await _send_chunks(
                                websocket,
                                runtime,
                                session_id,
                                start_message,
                                chunker.flush("stop"),
                                frame_offset_ms,
                            )
                        _control_if_possible(runtime, session_id, "stop")
                        _finalize_audio(runtime, runtime.sessions.get(session_id))
                        await websocket.send_json({"type": "stream.stopped", "session_id": session_id})
                        await websocket.close(code=1000)
                        return
                    else:
                        raise ValueError(f"unsupported message type: {message_type!r}")
                elif message.get("bytes") is not None:
                    if pending_header is None or start_message is None:
                        raise ValueError("binary audio must follow an audio.frame or audio.chunk header")
                    pcm = message["bytes"]
                    expected = pending_header.get("byte_length")
                    if expected is not None and len(pcm) != expected:
                        raise ValueError(f"expected {expected} audio bytes, received {len(pcm)}")
                    _record_audio(runtime, session_id, start_message, pending_header, pcm)
                    if pending_header["type"] == "audio.frame":
                        if chunker is None:
                            raise ValueError("audio frame received without stream VAD configuration")
                        chunks = chunker.feed(pcm)
                        await _send_chunks(
                            websocket,
                            runtime,
                            session_id,
                            start_message,
                            chunks,
                            frame_offset_ms,
                        )
                        if not chunks:
                            await websocket.send_json(
                                {
                                    "type": "audio.accepted",
                                    "sequence": pending_header.get("sequence"),
                                }
                            )
                    else:
                        event = await _transcribe_pcm_chunk(
                            runtime, session_id, start_message, pending_header, pcm
                        )
                        runtime.sessions.append(event)
                        await websocket.send_json(
                            {"type": "transcript.final", "event": event.to_dict()}
                        )
                    pending_header = None
                elif message.get("type") == "websocket.disconnect":
                    return
        except WebSocketDisconnect:
            return
        except (KeyError, TypeError, ValueError) as exc:
            await websocket.send_json({"type": "error", "code": "invalid_message", "message": str(exc)})
            await websocket.close(code=1003)

    return app


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


def _finalize_audio(runtime: ServiceRuntime, session) -> None:
    writer = runtime.audio_writers.pop(session.config.session_id, None)
    if writer is None:
        return
    writer.close()
    if session.config.save_combined_audio:
        writer.generate_combined()
    if not session.config.save_separate_audio:
        for source in Source:
            for path in writer.session_dir.glob(f"{source.value}-part-*.wav"):
                path.unlink()


def _stream_chunker(start: dict[str, Any]) -> EnergyVadChunker:
    audio = start.get("audio", {})
    requested = start.get("vad", {})
    names = {
        "threshold_rms",
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
    session_id: str,
    start: dict[str, Any],
    chunks,
    offset_ms: int,
) -> None:
    sample_rate = int(start.get("audio", {}).get("sample_rate_hz", 16000))
    for chunk in chunks:
        chunk_start = offset_ms + round(chunk.start_ms(sample_rate))
        chunk_end = offset_ms + round(chunk.end_ms(sample_rate))
        header = {
            "type": "audio.chunk",
            "start_ms": chunk_start,
            "end_ms": chunk_end,
            "finalization_reason": chunk.reason,
        }
        event = await _transcribe_pcm_chunk(
            runtime, session_id, start, header, chunk.pcm
        )
        runtime.sessions.append(event)
        await websocket.send_json({"type": "transcript.final", "event": event.to_dict()})


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
                language=start.get("language"),
            ),
        )


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
):
    if session_id is None:
        session_id = _new_session_id()
    try:
        session = runtime.sessions.get(session_id)
    except KeyError:
        session = runtime.sessions.create(
            SessionConfig(session_id=session_id, sources=sources or (source,))
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
    try:
        getattr(runtime.sessions, action)(session_id)
    except (KeyError, ValueError):
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


def _new_session_id() -> str:
    from datetime import datetime

    return datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S-%f")


def _segment_id() -> str:
    return f"seg-{uuid4().hex[:12]}"
