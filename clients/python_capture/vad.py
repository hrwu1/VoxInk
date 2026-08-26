"""Dependency-free energy VAD and utterance chunking.

This deliberately simple implementation is intended for bringing up the live
pipeline.  It is not an accuracy-oriented replacement for a trained VAD.
"""

from __future__ import annotations

from array import array
from collections import deque
from dataclasses import dataclass
import math
import sys
from typing import Deque


@dataclass(frozen=True, slots=True)
class VadConfig:
    sample_rate_hz: int = 16_000
    frame_duration_ms: int = 20
    threshold_rms: float = 500.0
    silence_duration_ms: int = 700
    min_speech_duration_ms: int = 200
    max_chunk_duration_ms: int = 15_000
    pre_roll_ms: int = 200
    overlap_ms: int = 200

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        if self.frame_duration_ms <= 0:
            raise ValueError("frame_duration_ms must be positive")
        if self.threshold_rms < 0:
            raise ValueError("threshold_rms cannot be negative")
        if self.silence_duration_ms < self.frame_duration_ms:
            raise ValueError("silence_duration_ms must be at least one frame")
        if self.min_speech_duration_ms < 0:
            raise ValueError("min_speech_duration_ms cannot be negative")
        if self.max_chunk_duration_ms < self.frame_duration_ms:
            raise ValueError("max_chunk_duration_ms must be at least one frame")
        if self.pre_roll_ms < 0 or self.overlap_ms < 0:
            raise ValueError("pre-roll and overlap cannot be negative")

    @property
    def samples_per_frame(self) -> int:
        samples = self.sample_rate_hz * self.frame_duration_ms
        if samples % 1000:
            raise ValueError("sample rate and frame duration must form whole samples")
        return samples // 1000


@dataclass(frozen=True, slots=True)
class AudioChunk:
    pcm: bytes
    start_sample: int
    end_sample: int
    speech_samples: int
    reason: str

    def start_ms(self, sample_rate_hz: int) -> float:
        return self.start_sample * 1000.0 / sample_rate_hz

    def end_ms(self, sample_rate_hz: int) -> float:
        return self.end_sample * 1000.0 / sample_rate_hz


def pcm_s16le_rms(pcm: bytes) -> float:
    """Return RMS amplitude for headerless signed 16-bit little-endian PCM."""
    if len(pcm) % 2:
        raise ValueError("PCM byte length must be divisible by two")
    if not pcm:
        return 0.0
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    square_sum = sum(sample * sample for sample in samples)
    return math.sqrt(square_sum / len(samples))


class EnergyVadChunker:
    """Group fixed-size PCM frames into utterances using an RMS threshold.

    Chunks include configurable pre-roll and trailing silence.  After a chunk
    is finalized, its tail is retained as overlap for the next utterance.
    """

    def __init__(self, config: VadConfig) -> None:
        self.config = config
        self._frame_bytes = config.samples_per_frame * 2
        self._silence_frames = _ceil_frames(
            config.silence_duration_ms, config.frame_duration_ms
        )
        self._min_speech_frames = _ceil_frames(
            config.min_speech_duration_ms, config.frame_duration_ms
        )
        self._max_chunk_frames = max(
            1, config.max_chunk_duration_ms // config.frame_duration_ms
        )
        self._pre_roll_frames = _ceil_frames(
            config.pre_roll_ms, config.frame_duration_ms
        )
        self._overlap_frames = _ceil_frames(
            config.overlap_ms, config.frame_duration_ms
        )
        self._pending = bytearray()
        self._pre_roll: Deque[tuple[int, bytes]] = deque(
            maxlen=max(1, self._pre_roll_frames, self._overlap_frames)
        )
        self._active: list[tuple[int, bytes]] = []
        self._speech_frames = 0
        self._trailing_silence_frames = 0
        self._next_sample = 0

    @property
    def active(self) -> bool:
        return bool(self._active)

    def feed(self, pcm: bytes) -> list[AudioChunk]:
        """Consume arbitrary-length PCM and return zero or more final chunks."""
        self._pending.extend(pcm)
        emitted: list[AudioChunk] = []
        while len(self._pending) >= self._frame_bytes:
            frame = bytes(self._pending[: self._frame_bytes])
            del self._pending[: self._frame_bytes]
            emitted.extend(self._feed_frame(frame))
        return emitted

    def flush(self, reason: str = "stop") -> list[AudioChunk]:
        """Finalize a sufficiently long active utterance and reset state."""
        if self._pending:
            # A short final frame is padded so the sample clock stays frame based.
            padded = bytes(self._pending) + bytes(self._frame_bytes - len(self._pending))
            self._pending.clear()
            chunks = self._feed_frame(padded)
        else:
            chunks = []
        if self._active and self._speech_frames >= self._min_speech_frames:
            chunks.append(self._emit(reason))
        else:
            self._reset_active(keep_overlap=False)
        return chunks

    def reset(self) -> None:
        """Discard buffered audio, for example when capture is paused."""
        self._pending.clear()
        self._active.clear()
        self._pre_roll.clear()
        self._speech_frames = 0
        self._trailing_silence_frames = 0

    def _feed_frame(self, frame: bytes) -> list[AudioChunk]:
        start_sample = self._next_sample
        self._next_sample += self.config.samples_per_frame
        voiced = pcm_s16le_rms(frame) >= self.config.threshold_rms

        if not self._active:
            if voiced:
                self._active = [*self._pre_roll, (start_sample, frame)]
                self._speech_frames = 1
                self._trailing_silence_frames = 0
            else:
                self._append_pre_roll(start_sample, frame)
            return []

        self._active.append((start_sample, frame))
        if voiced:
            self._speech_frames += 1
            self._trailing_silence_frames = 0
        else:
            self._trailing_silence_frames += 1

        if len(self._active) >= self._max_chunk_frames:
            return [self._emit("max_duration")]
        if self._trailing_silence_frames >= self._silence_frames:
            if self._speech_frames >= self._min_speech_frames:
                return [self._emit("silence")]
            self._reset_active(keep_overlap=True)
        return []

    def _append_pre_roll(self, start_sample: int, frame: bytes) -> None:
        if self._pre_roll_frames:
            self._pre_roll.append((start_sample, frame))
        else:
            self._pre_roll.clear()

    def _emit(self, reason: str) -> AudioChunk:
        first_start = self._active[0][0]
        end_sample = self._active[-1][0] + self.config.samples_per_frame
        pcm = b"".join(frame for _, frame in self._active)
        chunk = AudioChunk(
            pcm=pcm,
            start_sample=first_start,
            end_sample=end_sample,
            speech_samples=self._speech_frames * self.config.samples_per_frame,
            reason=reason,
        )
        self._reset_active(keep_overlap=True)
        return chunk

    def _reset_active(self, *, keep_overlap: bool) -> None:
        retained = self._active[-self._overlap_frames :] if (
            keep_overlap and self._overlap_frames
        ) else []
        self._active = []
        self._speech_frames = 0
        self._trailing_silence_frames = 0
        self._pre_roll.clear()
        for item in retained:
            self._pre_roll.append(item)


def _ceil_frames(duration_ms: int, frame_duration_ms: int) -> int:
    if duration_ms <= 0:
        return 0
    return (duration_ms + frame_duration_ms - 1) // frame_duration_ms
