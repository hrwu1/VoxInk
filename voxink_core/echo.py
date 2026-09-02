"""Tell a microphone utterance from the room's echo of the system audio.

When the speakers are on, the microphone hears the remote side of a call a few
tens of milliseconds after the system track played it. Transcribing both would
put the same sentence in the transcript twice under two labels. Because VoxInk
keeps the sources separate, the microphone chunk can be compared with what the
system channels played over the same window: a delayed, attenuated copy
correlates strongly at one lag; the user's own speech does not.

The decision is a normalized cross-correlation peak over a bounded lag range.
It is deliberately conservative: a false "echo" drops words the user said, a
missed echo only repeats words already in the transcript.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
from typing import Deque

import numpy as np


@dataclass(frozen=True, slots=True)
class EchoPolicy:
    sample_rate_hz: int = 16_000
    #: Acoustic path plus device latency; room echo arrives well inside this.
    max_lag_ms: int = 300
    #: Normalized cross-correlation the best lag must reach.
    min_correlation: float = 0.55
    #: RMS the system window must carry before it can explain anything.
    min_reference_rms: float = 30.0
    #: How much system audio to remember per channel. Covers the longest
    #: utterance chunk the VAD emits plus the lag window.
    history_ms: int = 20_000

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        if self.max_lag_ms <= 0:
            raise ValueError("max_lag_ms must be positive")
        if not 0.0 < self.min_correlation <= 1.0:
            raise ValueError("min_correlation must fall in (0, 1]")
        if self.min_reference_rms < 0:
            raise ValueError("min_reference_rms cannot be negative")
        if self.history_ms <= self.max_lag_ms:
            raise ValueError("history_ms must exceed max_lag_ms")


@dataclass(frozen=True, slots=True)
class EchoVerdict:
    echo: bool
    correlation: float
    lag_ms: int
    channel: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "echo": self.echo,
            "correlation": round(self.correlation, 3),
            "lag_ms": self.lag_ms,
            "channel": self.channel,
        }


_NO_ECHO = EchoVerdict(False, 0.0, 0, None)


class EchoDetector:
    """Remember recent system audio per channel and judge microphone chunks."""

    def __init__(self, policy: EchoPolicy | None = None) -> None:
        self.policy = policy or EchoPolicy()
        self._history: dict[tuple[str, str], Deque[tuple[int, bytes]]] = {}
        self._lock = threading.Lock()

    def observe(self, session_id: str, channel: str, start_ms: int, pcm: bytes) -> None:
        """Record what one system channel played from `start_ms`."""
        if not pcm:
            return
        with self._lock:
            history = self._history.setdefault((session_id, channel), deque())
            history.append((int(start_ms), pcm))
            horizon = int(start_ms) - self.policy.history_ms
            while history and history[0][0] < horizon:
                history.popleft()

    def forget(self, session_id: str) -> None:
        with self._lock:
            for key in [key for key in self._history if key[0] == session_id]:
                del self._history[key]

    def has_reference(self, session_id: str) -> bool:
        with self._lock:
            return any(key[0] == session_id and history for key, history in self._history.items())

    def judge(self, session_id: str, start_ms: int, end_ms: int, pcm: bytes) -> EchoVerdict:
        """Is this microphone chunk a delayed copy of any system channel?"""
        if len(pcm) < 4 or end_ms <= start_ms:
            return _NO_ECHO
        with self._lock:
            channels = {
                key[1]: list(history)
                for key, history in self._history.items()
                if key[0] == session_id and history
            }
        if not channels:
            return _NO_ECHO
        rate = self.policy.sample_rate_hz
        microphone = _to_float(pcm)
        # The reference window starts up to max_lag earlier than the chunk, so
        # the echo of something played just before the chunk began is covered.
        window_start = start_ms - self.policy.max_lag_ms
        window_end = end_ms
        best = _NO_ECHO
        for channel, chunks in channels.items():
            reference = _assemble(chunks, window_start, window_end, rate)
            if reference is None:
                continue
            verdict = _correlate(microphone, reference, self.policy, channel)
            if verdict.correlation > best.correlation:
                best = verdict
        return best


def _to_float(pcm: bytes) -> np.ndarray:
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2").astype(np.float32)
    return samples


def _assemble(
    chunks: list[tuple[int, bytes]], window_start_ms: int, window_end_ms: int, rate: int
) -> np.ndarray | None:
    """Lay buffered chunks onto a zeroed timeline covering the window."""
    length = (window_end_ms - window_start_ms) * rate // 1000
    if length <= 0:
        return None
    timeline = np.zeros(length, dtype=np.float32)
    covered = 0
    for chunk_start_ms, pcm in chunks:
        samples = _to_float(pcm)
        offset = (chunk_start_ms - window_start_ms) * rate // 1000
        if offset >= length or offset + len(samples) <= 0:
            continue
        source_start = max(0, -offset)
        target_start = max(0, offset)
        count = min(len(samples) - source_start, length - target_start)
        if count <= 0:
            continue
        timeline[target_start : target_start + count] = samples[source_start : source_start + count]
        covered += count
    return timeline if covered else None


def _correlate(
    microphone: np.ndarray, reference: np.ndarray, policy: EchoPolicy, channel: str
) -> EchoVerdict:
    """Peak normalized cross-correlation of the chunk against the reference.

    The reference is longer than the chunk by the lag window; correlating at
    lag L compares the chunk with reference[L : L + len(chunk)]. Only lags
    within `max_lag_ms` count.
    """
    if len(microphone) < 2 or len(reference) < len(microphone):
        return EchoVerdict(False, 0.0, 0, channel)
    reference_rms = float(np.sqrt(np.mean(reference * reference))) if len(reference) else 0.0
    if reference_rms < policy.min_reference_rms:
        return EchoVerdict(False, 0.0, 0, channel)
    microphone = microphone - microphone.mean()
    reference = reference - reference.mean()
    mic_norm = float(np.sqrt(np.dot(microphone, microphone)))
    if mic_norm == 0.0:
        return EchoVerdict(False, 0.0, 0, channel)
    # The reference begins `max_lag_ms` before the chunk, so offset `window`
    # into it is "no delay" and smaller offsets are earlier playback: the
    # echo delay is the window minus the best offset.
    window_offset = policy.max_lag_ms * policy.sample_rate_hz // 1000
    max_lag = min(len(reference) - len(microphone), window_offset)
    if max_lag < 0:
        return EchoVerdict(False, 0.0, 0, channel)
    # Cross-correlation via FFT: corr[L] = sum_n reference[n + L] * microphone[n].
    size = 1 << (len(reference) + len(microphone)).bit_length()
    spectrum = np.fft.rfft(reference, size) * np.conj(np.fft.rfft(microphone, size))
    correlation = np.fft.irfft(spectrum, size)[: max_lag + 1]
    # Sliding norm of the reference window for each lag.
    squares = np.concatenate(([0.0], np.cumsum(reference * reference, dtype=np.float64)))
    window = len(microphone)
    lags = np.arange(max_lag + 1)
    reference_norms = np.sqrt(np.maximum(squares[lags + window] - squares[lags], 1e-12))
    normalized = correlation / (reference_norms * mic_norm)
    best_lag = int(np.argmax(normalized))
    best = float(normalized[best_lag])
    return EchoVerdict(
        echo=best >= policy.min_correlation,
        correlation=max(0.0, min(1.0, best)),
        lag_ms=max(0, (window_offset - best_lag) * 1000 // policy.sample_rate_hz),
        channel=channel,
    )
