"""Name a live stream's speaker from the voices the user chose to remember.

Diarization after Stop has the whole recording and a clustering step; a live
caption has one utterance. The embedding model is stable enough that a few
seconds of one voice match an enrolled print, but a single short utterance is
not: on the reference sample a 2 s clip reaches a cosine of roughly 0.4
against the same voice, while the registry asks for 0.62. So each stream keeps
a duration-weighted rolling mean of its recent segment embeddings and matches
that, once enough speech has accumulated.

A microphone stream is mostly one voice and settles on a name within a few
utterances. A system stream where several people alternate blurs its mean
and stays anonymous, which is the safe failure: a wrong name on a caption is
worse than none. As a second guard the utterance itself must resemble the
matched voice, so a brief interjection by someone else is not labelled with
the dominant speaker's name.

The model loads lazily on first use, only when the registry holds a voice,
and any failure switches naming off for the process rather than touching the
captions.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Callable, Deque

import numpy as np

from ..speakers import SpeakerRegistry, SpeakerRegistryError, cosine
from .base import require_local_model
from .pyannote import OFFLINE_ENVIRONMENT, apply_offline_environment, resolve_device

#: Turns a mono float32 waveform at 16 kHz into one embedding vector.
Embedder = Callable[[np.ndarray], np.ndarray]
#: Builds the embedder; returns it together with the id of the model whose
#: space the registry's prints live in.
EmbedderFactory = Callable[[], tuple[Embedder, str]]

SAMPLE_RATE = 16_000


@dataclass(frozen=True, slots=True)
class LiveSpeakerPolicy:
    #: Speech kept in the rolling mean, in seconds of segment audio.
    window_s: float = 20.0
    #: A segment shorter than this carries too little voice to embed.
    min_segment_s: float = 1.0
    #: Accumulated speech before the mean is trusted enough to match.
    min_speech_s: float = 4.0
    #: Cosine the utterance itself must reach against the matched voice.
    segment_guard: float = 0.3

    def __post_init__(self) -> None:
        if self.window_s <= 0 or self.min_segment_s <= 0 or self.min_speech_s <= 0:
            raise ValueError("window_s, min_segment_s, and min_speech_s must be positive")
        if not 0.0 <= self.segment_guard <= 1.0:
            raise ValueError("segment_guard must fall in [0, 1]")


@dataclass(slots=True)
class _StreamVoice:
    recent: Deque[tuple[np.ndarray, float]]
    speech_s: float = 0.0


class LiveSpeakerIdentifier:
    def __init__(
        self,
        registry: SpeakerRegistry,
        *,
        model_path: Path,
        device: str = "auto",
        policy: LiveSpeakerPolicy | None = None,
        embedder_factory: EmbedderFactory | None = None,
    ) -> None:
        self.registry = registry
        self.model_path = Path(model_path)
        self.device = device
        self.policy = policy or LiveSpeakerPolicy()
        self._factory = embedder_factory or self._load_pyannote
        self._embedder: Embedder | None = None
        self._model_id: str | None = None
        self._lock = threading.Lock()
        self._streams: dict[tuple[str, str], _StreamVoice] = {}
        #: Why naming switched itself off, if it did.
        self.error: str | None = None

    @property
    def is_loaded(self) -> bool:
        return self._embedder is not None

    def identify(
        self, session_id: str, stream_id: str, pcm: bytes, sample_rate: int, channels: int = 1
    ) -> str | None:
        """The remembered name for this utterance's voice, or None.

        Safe to call from a worker thread; never raises.
        """
        if self.error is not None or sample_rate != SAMPLE_RATE or channels != 1:
            return None
        duration = len(pcm) / 2 / sample_rate
        if duration < self.policy.min_segment_s:
            return None
        try:
            if not self.registry.list():
                return None
        except SpeakerRegistryError:
            return None
        with self._lock:
            try:
                return self._identify_locked(session_id, stream_id, pcm, duration)
            except Exception as exc:  # noqa: BLE001 - names are an extra, never a failure
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                return None

    def forget(self, session_id: str) -> None:
        with self._lock:
            for key in [key for key in self._streams if key[0] == session_id]:
                del self._streams[key]

    def _identify_locked(self, session_id: str, stream_id: str, pcm: bytes, duration: float) -> str | None:
        if self._embedder is None:
            self._embedder, self._model_id = self._factory()
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        vector = np.asarray(self._embedder(samples), dtype=np.float64).reshape(-1)
        if vector.size == 0 or not np.all(np.isfinite(vector)):
            return None
        voice = self._streams.setdefault((session_id, stream_id), _StreamVoice(recent=deque()))
        voice.recent.append((vector, duration))
        voice.speech_s += duration
        while voice.speech_s - voice.recent[0][1] >= self.policy.window_s and len(voice.recent) > 1:
            _, dropped = voice.recent.popleft()
            voice.speech_s -= dropped
        if voice.speech_s < self.policy.min_speech_s:
            return None
        mean = np.average(
            np.stack([item for item, _ in voice.recent]),
            axis=0,
            weights=np.asarray([weight for _, weight in voice.recent]),
        )
        match = self.registry.match(mean.tolist(), model_id=self._model_id)
        if match is None:
            return None
        if cosine(vector.tolist(), match.speaker.embedding) < self.policy.segment_guard:
            return None
        return match.speaker.name

    def _load_pyannote(self) -> tuple[Embedder, str]:
        model = require_local_model(self.model_path)
        apply_offline_environment()
        import torch  # noqa: PLC0415
        from pyannote.audio.pipelines.speaker_verification import (  # noqa: PLC0415
            PretrainedSpeakerEmbedding,
        )

        embedder = PretrainedSpeakerEmbedding(
            str(model.path / "embedding"), device=torch.device(resolve_device(self.device))
        )

        def embed(samples: np.ndarray) -> np.ndarray:
            waveform = torch.from_numpy(np.ascontiguousarray(samples))[None, None, :]
            return np.asarray(embedder(waveform))[0]

        return embed, model.source_model_id


__all__ = ["Embedder", "EmbedderFactory", "LiveSpeakerIdentifier", "LiveSpeakerPolicy", "OFFLINE_ENVIRONMENT"]
