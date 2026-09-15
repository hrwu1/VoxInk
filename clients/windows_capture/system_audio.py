"""All-system audio through WASAPI endpoint loopback.

The counterpart of `SystemAudioCapture.swift`, the mixed fallback path.
PortAudio exposes each render endpoint as a `[Loopback]` input; opening the
one that mirrors the default output device captures everything the user
hears, mixed into the single `system-1` stream. Unlike the process-loopback
path it cannot isolate applications, exclude this process, or follow a
change of default output device, so the coordinator prefers process loopback
and comes here only when that is unavailable.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .frame_queue import AudioFrameQueue, AudioFrameSink
from .models import (
    AudioFrame,
    AudioSource,
    CaptureError,
    CaptureStatusEvent,
    StatusLevel,
    SystemAudioSelection,
)
from .pcm_normalizer import PCMNormalizer
from .windows_audio import loopback_device, require_sounddevice


def pcm_peak_amplitude(data: bytes) -> int:
    if len(data) < 2:
        return 0
    peak = 0
    for offset in range(0, len(data) - 1, 2):
        value = int.from_bytes(data[offset:offset + 2], "little", signed=True)
        peak = max(peak, abs(value))
    return peak


class LoopbackCapture:
    STREAM_ID = "system-1"
    BLOCK_MS = 20

    def __init__(
        self,
        selection: SystemAudioSelection,
        session_id: str,
        sample_rate: int,
        sink: AudioFrameSink,
        sounddevice: Any | None = None,
    ) -> None:
        self._selection = selection
        self._session_id = session_id
        self._sink = sink
        self._sounddevice = sounddevice
        self._normalizer = PCMNormalizer(sample_rate)
        self._queue = AudioFrameQueue(sink, session_id, AudioSource.SYSTEM)
        self._stream: Any = None
        self._input_rate = 0
        self._lock = threading.Lock()
        self._sequence = 0
        self._captured_frames = 0
        self._peak_amplitude = 0
        self._endpoint_name = ""

    @property
    def dropped_frame_count(self) -> int:
        return self._queue.dropped_frame_count

    async def start(self) -> None:
        if not self._selection.is_all:
            raise CaptureError.loopback_unavailable("endpoint loopback cannot isolate applications")
        sounddevice = self._sounddevice or require_sounddevice()
        index, endpoint_name = loopback_device(sounddevice)
        try:
            info = sounddevice.query_devices(index)
        except Exception as exc:  # noqa: BLE001
            raise CaptureError.loopback_unavailable(str(exc)) from exc
        channels = min(2, int(info.get("max_input_channels", 0)))
        rate = int(info.get("default_samplerate", 0) or 0)
        if channels <= 0 or rate <= 0:
            raise CaptureError.audio_format_unavailable(f"loopback reports {channels} channels at {rate} Hz")
        self._input_rate = rate
        self._endpoint_name = endpoint_name
        self._queue.start()
        try:
            self._stream = sounddevice.InputStream(
                device=index,
                channels=channels,
                samplerate=rate,
                dtype="float32",
                blocksize=rate * self.BLOCK_MS // 1000,
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001
            self._release_stream()
            await self._queue.finish_and_drain()
            raise CaptureError.loopback_unavailable(str(exc)) from exc
        self._status(StatusLevel.INFO, "system_capture_started", f"Capturing all system audio through WASAPI loopback on {endpoint_name}.")

    async def pause(self) -> None:
        """The loopback stream stays open across a pause; only delivery stops."""
        await self._queue.finish_and_drain()

    def resume(self) -> None:
        self._queue.start()

    async def stop(self) -> None:
        self._release_stream()
        await self._queue.finish_and_drain()
        with self._lock:
            captured, peak = self._captured_frames, self._peak_amplitude
        self._status(
            StatusLevel.INFO if captured > 0 and peak > 0 else StatusLevel.WARNING,
            "system_capture_summary",
            f"Captured {captured} system audio frames from {self._endpoint_name or 'loopback'} "
            f"(output peak {peak} of 32768).",
        )

    def _release_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        for action in (stream.stop, stream.close):
            try:
                action()
            except Exception:  # noqa: BLE001
                pass

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        del frames, time_info, status
        try:
            data = self._normalizer.normalize(indata, self._input_rate)
        except CaptureError as exc:
            self._status(StatusLevel.WARNING, "system_frame_dropped", str(exc))
            return
        if not data:
            return
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            self._captured_frames += 1
            self._peak_amplitude = max(self._peak_amplitude, pcm_peak_amplitude(data))
        self._queue.submit(AudioFrame(
            session_id=self._session_id,
            stream_id=self.STREAM_ID,
            source=AudioSource.SYSTEM,
            sequence=sequence,
            timestamp_ns=time.monotonic_ns(),
            sample_rate=self._normalizer.output_sample_rate,
            pcm_s16le=data,
        ))

    def _status(self, level: StatusLevel, code: str, message: str) -> None:
        self._sink.send_status(CaptureStatusEvent(
            session_id=self._session_id, source=AudioSource.SYSTEM, level=level, code=code, message=message,
        ))
