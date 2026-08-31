"""Dependency-free energy VAD and utterance chunking.

This deliberately simple implementation is intended for bringing up the live
pipeline.  It is not an accuracy-oriented replacement for a trained VAD.

The gate adapts to the input rather than sitting at an absolute level.  A fixed
threshold cannot be right across microphones and input gains: the same constant
that ignores a noisy room will discard ordinary speech from a quiet one.  The
threshold is derived from a running noise floor, so what the gate answers is
"is this louder than this room?" rather than "is this louder than 500?".
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
    silence_duration_ms: int = 700
    min_speech_duration_ms: int = 200
    max_chunk_duration_ms: int = 15_000
    pre_roll_ms: int = 200
    overlap_ms: int = 200

    # Gate.  `threshold_rms` pins the gate to an absolute level and disables
    # adaptation; it exists for tests that exercise chunking mechanics against a
    # known gate, and for the diagnostic client's flag.  Leave it None in the
    # live path, where the input level is not known in advance.
    threshold_rms: float | None = None
    noise_floor_ratio: float = 3.0
    absolute_floor_rms: float = 10.0
    release_ratio: float = 0.5
    noise_floor_window_ms: int = 5_000
    noise_floor_percentile: float = 10.0
    # Deliberately shorter than `min_speech_duration_ms`. Until the estimate is
    # established the gate can only be the absolute floor, which the background
    # of a normal room clears; establishing it within a few frames means any
    # such false onset is discarded as too short to be speech long before it
    # could be emitted as a chunk.
    noise_floor_min_observations: int = 5

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        if self.frame_duration_ms <= 0:
            raise ValueError("frame_duration_ms must be positive")
        if self.threshold_rms is not None and self.threshold_rms < 0:
            raise ValueError("threshold_rms cannot be negative")
        if self.noise_floor_ratio < 1.0:
            raise ValueError("noise_floor_ratio must be at least one")
        if self.absolute_floor_rms < 0:
            raise ValueError("absolute_floor_rms cannot be negative")
        if not 0.0 < self.release_ratio <= 1.0:
            raise ValueError("release_ratio must fall in (0, 1]")
        if self.noise_floor_window_ms < self.frame_duration_ms:
            raise ValueError("noise_floor_window_ms must be at least one frame")
        if not 0.0 < self.noise_floor_percentile < 100.0:
            raise ValueError("noise_floor_percentile must fall in (0, 100)")
        if self.noise_floor_min_observations < 1:
            raise ValueError("noise_floor_min_observations must be positive")
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
        self._background: Deque[float] = deque(
            maxlen=max(1, _ceil_frames(config.noise_floor_window_ms, config.frame_duration_ms))
        )

    @property
    def active(self) -> bool:
        return bool(self._active)

    @property
    def noise_floor_rms(self) -> float | None:
        """Current background estimate, or None while it is not yet trustworthy.

        Below `noise_floor_min_observations` there is no estimate rather than a
        bad one: a handful of frames cannot say what a room sounds like, and a
        gate derived from two of them is worse than the absolute floor.
        """
        if len(self._background) < self.config.noise_floor_min_observations:
            return None
        ordered = sorted(self._background)
        index = int(len(ordered) * self.config.noise_floor_percentile / 100.0)
        return ordered[min(index, len(ordered) - 1)]

    @property
    def threshold_rms(self) -> float:
        """Level a frame must reach to be voiced, given the current state."""
        return self._gate(active=bool(self._active))

    @property
    def next_sample(self) -> int:
        """Sample index the next fed frame will occupy."""
        return self._next_sample

    @property
    def next_sample_ms(self) -> float:
        """Position of the sample clock, for anchoring chunks to capture time."""
        return self._next_sample * 1000.0 / self.config.sample_rate_hz

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
        """Discard buffered audio and restart the sample clock.

        Used when capture pauses. The clock restarts at zero because paused
        wall time is not audio: callers re-anchor the next chunk to the capture
        timeline instead of letting pre-pause samples inflate every later
        timestamp.

        The background estimate is kept. It describes the room and the
        microphone, not the buffer, and neither changes because capture paused;
        discarding it would make the first seconds after every resume gate on
        the absolute floor alone.
        """
        self._pending.clear()
        self._active.clear()
        self._pre_roll.clear()
        self._speech_frames = 0
        self._trailing_silence_frames = 0
        self._next_sample = 0

    def _feed_frame(self, frame: bytes) -> list[AudioChunk]:
        start_sample = self._next_sample
        self._next_sample += self.config.samples_per_frame
        rms = pcm_s16le_rms(frame)
        # The gate is read before the floor is updated, so that a frame cannot
        # raise the floor and then be judged against it.
        voiced = rms >= self._gate(active=bool(self._active))
        self._observe_background(rms)

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

    def _gate(self, *, active: bool) -> float:
        """Return the level a frame must reach to count as speech.

        A pinned `threshold_rms` disables adaptation entirely, including the
        release below, so that a test asking for a fixed gate gets exactly that.

        Otherwise the gate sits a fixed ratio above the measured background, and
        once an utterance is active it drops by `release_ratio`.  Speech does not
        end at the volume it started: trailing syllables and quiet word endings
        fall well below the level that opened the gate, and holding the entry
        threshold through them is what splits one sentence into several chunks
        and drops the words in between.
        """
        if self.config.threshold_rms is not None:
            return self.config.threshold_rms
        floor = self.noise_floor_rms
        # With no estimate yet, treat the absolute floor as the background and
        # apply the same ratio to it. Gating on the bare absolute floor would
        # admit the background of any ordinary room as speech for as long as it
        # took to measure one, and the release below would admit even more.
        threshold = max(
            self.config.absolute_floor_rms,
            (floor if floor is not None else self.config.absolute_floor_rms)
            * self.config.noise_floor_ratio,
        )
        if active and floor is not None:
            # Hysteresis refines a measured gate. There is nothing to refine
            # while the background is still unknown, so it is not loosened.
            return threshold * self.config.release_ratio
        return threshold

    def _observe_background(self, rms: float) -> None:
        """Record a frame as evidence of the background level.

        A low percentile over a window, rather than a running average, is what
        makes this survive both of the things that break a simpler estimator.
        Speech occupies the top of the distribution, so it cannot drag the floor
        up and gate itself out over a long utterance.  Digital silence is
        excluded outright: a muted input or a dropped buffer is the absence of a
        signal, not evidence that the room became quieter, and an estimator that
        believes it collapses the gate for as long as it takes to recover.

        Once the estimate is established, frames loud enough to be speech stop
        contributing to it. Without that, a stretch of speech with no background
        under it walks the floor up behind itself until the gate closes over the
        speech that raised it.  While the estimate is still being established
        every audible frame counts, because a gate that only ever learns from
        frames it already considers quiet can never discover a louder room.
        """
        if rms < self.config.absolute_floor_rms:
            return
        established = len(self._background) >= self.config.noise_floor_min_observations
        if established and rms >= self._gate(active=False):
            return
        self._background.append(rms)

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
