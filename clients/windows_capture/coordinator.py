"""Start, pause, resume, and stop every configured source as one session.

The counterpart of `CaptureCoordinator.swift`. Every method runs on the event
loop, and a lock keeps a stop that lands during a pause's drain from
interleaving with it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from enum import Enum
from typing import Any, Protocol

from .frame_queue import AudioFrameSink
from .microphone import MicrophoneCapture
from .models import (
    AudioSource,
    CaptureConfiguration,
    CaptureError,
    CaptureStatusEvent,
    StatusLevel,
    SystemAudioBackend,
    SystemAudioSelection,
)
from .process_loopback import ProcessLoopbackCapture, ProcessLoopbackChannel, process_loopback_availability
from .service_client import StatusHandler, TranscriptHandler, TranslationHandler, WebSocketAudioFrameSink
from .system_audio import LoopbackCapture


class SystemAudioSourceCapture(Protocol):
    """One running system-audio source, whichever facility provides it."""

    @property
    def dropped_frame_count(self) -> int: ...
    async def start(self) -> None: ...
    async def pause(self) -> None: ...
    def resume(self) -> None: ...
    async def stop(self) -> None: ...


class CaptureState(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    # Draining the sources before the pause is complete.
    PAUSING = "pausing"
    PAUSED = "paused"


# Injection points for tests: (configuration, sink) -> source.
MicrophoneFactory = Callable[[CaptureConfiguration, AudioFrameSink], Any]
ProcessLoopbackFactory = Callable[[ProcessLoopbackChannel, CaptureConfiguration, AudioFrameSink], Any]
LoopbackFactory = Callable[[SystemAudioSelection, CaptureConfiguration, AudioFrameSink], Any]


class CaptureCoordinator:
    def __init__(
        self,
        configuration: CaptureConfiguration,
        sink: AudioFrameSink | None = None,
        transcript_handler: TranscriptHandler | None = None,
        translation_handler: TranslationHandler | None = None,
        status_handler: StatusHandler | None = None,
        microphone_factory: MicrophoneFactory | None = None,
        process_loopback_factory: ProcessLoopbackFactory | None = None,
        loopback_factory: LoopbackFactory | None = None,
        process_loopback_available: Callable[[], tuple[bool, str]] = process_loopback_availability,
    ) -> None:
        if not configuration.microphone_enabled and configuration.system_audio is None:
            raise CaptureError.no_capture_source()
        self._configuration = configuration
        self._status_handler = status_handler
        self._sink: AudioFrameSink = sink or WebSocketAudioFrameSink(
            endpoint=configuration.service_url or "",
            auth_token=configuration.auth_token,
            applications=configuration.selected_applications,
            retain_audio=configuration.retain_audio,
            language=configuration.language,
            spool_directory=configuration.spool_directory,
            transcript_handler=transcript_handler,
            translation_handler=translation_handler,
            status_handler=status_handler,
        )
        self._microphone_factory = microphone_factory or (
            lambda config, sink: MicrophoneCapture(config.session_id, config.sample_rate, sink, config.microphone_device_uid)
        )
        self._process_loopback_factory = process_loopback_factory or (
            lambda channel, config, sink: ProcessLoopbackCapture(channel, config.session_id, config.sample_rate, sink)
        )
        self._loopback_factory = loopback_factory or (
            lambda selection, config, sink: LoopbackCapture(selection, config.session_id, config.sample_rate, sink)
        )
        self._process_loopback_available = process_loopback_available
        self._microphone: Any = None
        self._system_sources: list[SystemAudioSourceCapture] = []
        self._state = CaptureState.IDLE
        # A resume that arrived while `pause()` was still draining.
        self._pending_resume = False
        # A stop that arrived while `pause()` was still draining: it owns the
        # state once the drain is done.
        self._stop_requested = False
        self._lock = asyncio.Lock()

    @property
    def current_state(self) -> CaptureState:
        return self._state

    @property
    def dropped_frame_count(self) -> int:
        """Frames discarded across all sources because transcription fell behind."""
        total = self._microphone.dropped_frame_count if self._microphone is not None else 0
        return total + sum(source.dropped_frame_count for source in self._system_sources)

    async def start(self) -> None:
        async with self._lock:
            if self._state is not CaptureState.IDLE:
                return
            configuration = self._configuration
            sources = []
            if configuration.microphone_enabled:
                sources.append(AudioSource.MICROPHONE)
            if configuration.system_audio is not None:
                sources.append(AudioSource.SYSTEM)
            await self._sink.connect(configuration.session_id, sources)
            try:
                if configuration.microphone_enabled:
                    microphone = self._microphone_factory(configuration, self._sink)
                    await microphone.start()
                    self._microphone = microphone
                if configuration.system_audio is not None:
                    self._system_sources = await self._start_system_sources(configuration.system_audio)
                self._state = CaptureState.RECORDING
            except BaseException:
                if self._microphone is not None:
                    await self._microphone.stop()
                    self._microphone = None
                for source in self._system_sources:
                    await source.stop()
                self._system_sources = []
                await self._sink.disconnect(configuration.session_id, "startup_failed")
                raise

    async def _start_system_sources(self, selection: SystemAudioSelection) -> list[SystemAudioSourceCapture]:
        """Open the system sources the configuration asks for.

        With process loopback, selected applications become one stream each so
        the transcript can say which application spoke, and all-system capture
        excludes this process. Endpoint loopback mixes everything into one
        stream and cannot narrow it, so it serves all-system capture only.
        """
        configuration = self._configuration
        backend = configuration.backend
        if backend is not SystemAudioBackend.LOOPBACK:
            available, reason = self._process_loopback_available()
            if available:
                started: list[SystemAudioSourceCapture] = []
                try:
                    for channel in self._loopback_channels(selection):
                        source = self._process_loopback_factory(channel, configuration, self._sink)
                        await source.start()
                        started.append(source)
                    return started
                except Exception as exc:  # noqa: BLE001
                    for source in started:
                        await source.stop()
                    if backend is not SystemAudioBackend.AUTO:
                        raise
                    self._status(
                        StatusLevel.WARNING, "loopback_fallback",
                        f"Process loopback unavailable ({exc}); using endpoint loopback.",
                    )
            elif backend is SystemAudioBackend.PROCESS_LOOPBACK:
                raise CaptureError.process_loopback_unavailable(reason)
            else:
                self._status(StatusLevel.WARNING, "loopback_fallback", f"Process loopback unavailable ({reason}); using endpoint loopback.")
        if not selection.is_all:
            raise CaptureError.loopback_unavailable(
                "selected-application capture needs process loopback; endpoint loopback cannot isolate applications"
            )
        capture = self._loopback_factory(selection, configuration, self._sink)
        await capture.start()
        return [capture]

    def _loopback_channels(self, selection: SystemAudioSelection) -> list[ProcessLoopbackChannel]:
        if selection.is_all:
            return [ProcessLoopbackChannel(identifier=None, name=None, stream_id="system-1")]
        names = self._configuration.application_names
        return [
            ProcessLoopbackChannel(identifier=identifier, name=names.get(identifier), stream_id=f"system-{identifier}")
            for identifier in selection.sorted_applications
        ]

    async def pause(self) -> None:
        """Stop delivering audio, then tell the service. Draining first means
        the utterance in progress is finalized instead of being cut off, and
        no audio arrives after the service has paused the session."""
        async with self._lock:
            if self._state is not CaptureState.RECORDING:
                return
            self._state = CaptureState.PAUSING
            if self._microphone is not None:
                await self._microphone.pause()
            for source in self._system_sources:
                await source.pause()
            await self._sink.pause(self._configuration.session_id)
            # A stop can land while the drain is in flight; it owns the state then.
            if self._stop_requested:
                return
            self._state = CaptureState.PAUSED
            resume_now = self._pending_resume
            self._pending_resume = False
        if resume_now:
            await self.resume()

    async def resume(self) -> None:
        if self._state is CaptureState.PAUSING:
            # The drain has not finished, so resuming now would restart sources
            # that pause() is still stopping. The service has already told the
            # user this was accepted, so record it and let pause() honour it
            # rather than dropping it and recording nothing.
            self._pending_resume = True
            return
        async with self._lock:
            if self._state is not CaptureState.PAUSED:
                return
            await self._sink.resume(self._configuration.session_id)
            if self._microphone is not None:
                self._microphone.resume()
            for source in self._system_sources:
                source.resume()
            self._state = CaptureState.RECORDING

    async def stop(self) -> None:
        self._pending_resume = False
        self._stop_requested = True
        async with self._lock:
            self._stop_requested = False
            if self._state is CaptureState.IDLE:
                return
            if self._microphone is not None:
                await self._microphone.stop()
                self._microphone = None
            for source in self._system_sources:
                await source.stop()
            self._system_sources = []
            self._state = CaptureState.IDLE
            await self._sink.disconnect(self._configuration.session_id, "user_stopped")

    def _status(self, level: StatusLevel, code: str, message: str) -> None:
        self._sink.send_status(CaptureStatusEvent(
            session_id=self._configuration.session_id, source=AudioSource.SYSTEM, level=level, code=code, message=message,
        ))
