"""Microphone capture through PortAudio.

The counterpart of `MicrophoneCapture.swift`. The device is opened at its own
rate and channel count and every buffer goes through the normalizer, so the
resampler keeps its state across buffers exactly as the native backend does.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .frame_queue import AudioFrameQueue, AudioFrameSink
from .models import AudioFrame, AudioSource, CaptureError
from .pcm_normalizer import PCMNormalizer
from .windows_audio import require_sounddevice, resolve_input_device


class MicrophoneCapture:
    STREAM_ID = "microphone-1"
    BLOCK_MS = 20

    def __init__(
        self,
        session_id: str,
        sample_rate: int,
        sink: AudioFrameSink,
        device_uid: str | None = None,
        sounddevice: Any | None = None,
    ) -> None:
        self._session_id = session_id
        self._device_uid = device_uid
        self._sounddevice = sounddevice
        self._normalizer = PCMNormalizer(sample_rate)
        self._queue = AudioFrameQueue(sink, session_id, AudioSource.MICROPHONE)
        self._stream: Any = None
        self._input_rate = 0
        self._lock = threading.Lock()
        self._sequence = 0

    @property
    def dropped_frame_count(self) -> int:
        return self._queue.dropped_frame_count

    async def start(self) -> None:
        sounddevice = self._sounddevice or require_sounddevice()
        device = resolve_input_device(self._device_uid, sounddevice)
        try:
            info = sounddevice.query_devices(device if device is not None else sounddevice.default.device[0])
        except Exception as exc:  # noqa: BLE001
            raise CaptureError.microphone_unavailable(str(exc)) from exc
        channels = min(2, int(info.get("max_input_channels", 0)))
        rate = int(info.get("default_samplerate", 0) or 0)
        if channels <= 0 or rate <= 0:
            raise CaptureError.audio_format_unavailable(f"microphone reports {channels} channels at {rate} Hz")
        self._input_rate = rate
        self._queue.start()
        try:
            self._stream = sounddevice.InputStream(
                device=device,
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
            if isinstance(exc, CaptureError):
                raise
            raise CaptureError.microphone_unavailable(str(exc)) from exc

    async def pause(self) -> None:
        """Stop delivering audio while the session is paused. Frames captured
        before the pipeline closes are still sent, so the utterance in
        progress is finalized rather than truncated."""
        await self._queue.finish_and_drain()

    def resume(self) -> None:
        self._queue.start()

    async def stop(self) -> None:
        self._release_stream()
        await self._queue.finish_and_drain()

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
            # The driver reuses its buffer; the normalizer copies what it reads.
            data = self._normalizer.normalize(indata, self._input_rate)
        except CaptureError:
            # Capture continues after an isolated conversion failure.
            return
        if not data:
            return
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        self._queue.submit(AudioFrame(
            session_id=self._session_id,
            stream_id=self.STREAM_ID,
            source=AudioSource.MICROPHONE,
            sequence=sequence,
            timestamp_ns=time.monotonic_ns(),
            sample_rate=self._normalizer.output_sample_rate,
            pcm_s16le=data,
        ))
