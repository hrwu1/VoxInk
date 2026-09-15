"""Audio the service could not take: timeline clamping, in-memory backlog,
and the WAV spool that is the last resort of a recording.

The counterpart of `AudioBacklog.swift`.
"""

from __future__ import annotations

from pathlib import Path
import struct

from .models import AudioFrame


class StreamClock:
    """Frame start times on the session timeline that never step backwards.

    A frame's start is its capture time minus its duration. After an audio
    overload the driver delivers the late buffers in a burst, so that
    arithmetic puts one frame inside the previous one, and a consumer that
    tiles audio by time (live speakers) refuses the overlap. Clamping a start
    to the previous frame's end keeps the frames contiguous; the next on-time
    frame lands after that end anyway, so the drift is bounded by the burst.
    """

    def __init__(self) -> None:
        self._last_end_ms: dict[str, float] = {}

    def start_ms(self, stream_id: str, captured_at_ms: float, duration_ms: float) -> float:
        nominal = max(0.0, captured_at_ms - duration_ms)
        return max(nominal, self._last_end_ms.get(stream_id, 0.0))

    def advance(self, stream_id: str, end_ms: float) -> None:
        """Record that a frame ending at `end_ms` was delivered."""
        self._last_end_ms[stream_id] = max(self._last_end_ms.get(stream_id, 0.0), end_ms)


class AudioBacklog:
    """Frames captured while the service could not take them, in capture order.

    Bounded in bytes: past the capacity the oldest frames are handed back to
    the caller, which writes them to the spool file, so memory stays flat no
    matter how long the service is away and nothing is dropped.
    """

    def __init__(self, capacity_bytes: int) -> None:
        self.capacity_bytes = max(1, capacity_bytes)
        self.frames: list[AudioFrame] = []
        self.byte_count = 0

    @property
    def is_empty(self) -> bool:
        return not self.frames

    def append(self, frame: AudioFrame) -> list[AudioFrame]:
        """Keep `frame` for later delivery; returns the oldest frames that no
        longer fit. Eviction frees a quarter of the capacity at a time so a
        long outage spools in batches rather than one frame per frame."""
        self.frames.append(frame)
        self.byte_count += len(frame.pcm_s16le)
        if self.byte_count <= self.capacity_bytes:
            return []
        target = self.capacity_bytes - self.capacity_bytes // 4
        evicted_bytes = 0
        count = 0
        while count < len(self.frames) - 1 and self.byte_count - evicted_bytes > target:
            evicted_bytes += len(self.frames[count].pcm_s16le)
            count += 1
        evicted = self.frames[:count]
        del self.frames[:count]
        self.byte_count -= evicted_bytes
        return evicted

    def drain(self) -> list[AudioFrame]:
        drained = self.frames
        self.frames = []
        self.byte_count = 0
        return drained


class AudioSpool:
    """A WAV file holding audio the service never received.

    The last resort of a recording: whatever cannot be delivered live is at
    least on disk, beside the transcript, in a form any transcriber reads. The
    RIFF sizes are patched every couple of seconds so a helper that dies
    mid-write leaves a readable file, not a header claiming zero samples.
    """

    HEADER_LENGTH = 44

    def __init__(self, directory: Path, stream_id: str, sample_rate: int, channels: int = 1) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        index = 1
        while True:
            candidate = directory / f"{stream_id}-spool-{index:03d}.wav"
            if not candidate.exists():
                break
            index += 1
        self.path = candidate
        self.sample_rate = sample_rate
        self.channels = channels
        self.data_bytes = 0
        self.is_closed = False
        self._bytes_since_header = 0
        # Exclusive creation: the name was chosen a moment ago, and a second
        # backend racing for it must not share the file.
        self._handle = open(candidate, "x+b")
        try:
            candidate.chmod(0o600)
        except OSError:
            pass
        self._handle.write(self.header(0, sample_rate, channels))

    @property
    def duration_seconds(self) -> float:
        return self.data_bytes / float(self.sample_rate * self.channels * 2)

    def append(self, pcm: bytes) -> None:
        if self.is_closed:
            return
        self._handle.write(pcm)
        self.data_bytes += len(pcm)
        self._bytes_since_header += len(pcm)
        if self._bytes_since_header >= self.sample_rate * self.channels * 2 * 2:
            self._patch_header()
            self._handle.seek(0, 2)

    def close(self) -> None:
        if self.is_closed:
            return
        self.is_closed = True
        try:
            self._patch_header()
        finally:
            self._handle.close()

    def _patch_header(self) -> None:
        self._handle.seek(0)
        self._handle.write(self.header(self.data_bytes, self.sample_rate, self.channels))
        self._handle.flush()
        self._bytes_since_header = 0

    @staticmethod
    def header(data_bytes: int, sample_rate: int, channels: int) -> bytes:
        payload = min(data_bytes, 0xFFFF_FFFF)
        block_align = channels * 2
        return b"".join((
            b"RIFF",
            struct.pack("<I", min(36 + payload, 0xFFFF_FFFF)),
            b"WAVE",
            b"fmt ",
            struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, sample_rate * block_align, block_align, 16),
            b"data",
            struct.pack("<I", payload),
        ))
