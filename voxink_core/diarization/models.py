"""Stable, provider-neutral diarization domain models.

Nothing here may import pyannote or Torch. The whole point of this module is
that the rules with real decisions in them -- rounding, label assignment,
ordering -- can be tested in milliseconds without loading a model, so the only
untested surface left is the thin adapter that talks to the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..models import Source


#: Longest diagnostic message kept on an error. Metadata crosses a process
#: boundary and lands in an artifact a user can read, so it is bounded here
#: rather than trusting every raiser to be brief.
MAX_DIAGNOSTIC_CHARS = 500

_SOURCE_PREFIX = {Source.MICROPHONE: "MIC", Source.SYSTEM: "SYSTEM"}


class DiarizationErrorCode(str, Enum):
    """Typed failures a caller can branch on without parsing a message."""

    AUDIO_UNAVAILABLE = "audio_unavailable"
    MODEL_UNAVAILABLE = "model_unavailable"
    INVALID_AUDIO = "invalid_audio"
    WORKER_CRASHED = "worker_crashed"
    WORKER_TIMED_OUT = "worker_timed_out"
    INSUFFICIENT_DISK = "insufficient_disk"
    ARTIFACT_STALE = "artifact_stale"
    ARTIFACT_WRITE_FAILED = "artifact_write_failed"


class JobState(str, Enum):
    """Externally meaningful diarization job states."""

    DISABLED = "disabled"
    WAITING_FOR_STOP = "waiting_for_stop"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    STALE = "stale"


class DiarizationError(RuntimeError):
    """A typed diarization failure carrying bounded, non-sensitive metadata."""

    def __init__(
        self,
        code: DiarizationErrorCode,
        message: str,
        **metadata: Any,
    ) -> None:
        self.code = DiarizationErrorCode(code)
        self.message = message[:MAX_DIAGNOSTIC_CHARS]
        self.metadata = dict(metadata)
        super().__init__(f"{self.code.value}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class DiarizationRequest:
    session_id: str
    source: Source
    audio_path: Path
    model_path: Path
    min_speakers: int | None = None
    max_speakers: int | None = None

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id must not be empty")
        if not isinstance(self.source, Source):
            object.__setattr__(self, "source", Source(self.source))
        object.__setattr__(self, "audio_path", Path(self.audio_path))
        object.__setattr__(self, "model_path", Path(self.model_path))
        for name in ("min_speakers", "max_speakers"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise TypeError(f"{name} must be an integer or None")
            if value is not None and value < 1:
                raise ValueError(f"{name} must be at least 1")
        if (
            self.min_speakers is not None
            and self.max_speakers is not None
            and self.min_speakers > self.max_speakers
        ):
            raise ValueError("min_speakers must not exceed max_speakers")


@dataclass(frozen=True)
class AlignmentBlock:
    """One transcript block's text and window, for word-level alignment.

    The `id` is opaque to the aligner: it is whatever the caller needs to
    match timings back to the block, and it comes back unchanged.
    """

    id: str
    start_ms: int
    end_ms: int
    text: str

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("id must not be empty")
        for value in (self.start_ms, self.end_ms):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError("timestamps must be integers")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("timestamps must satisfy 0 <= start_ms < end_ms")
        if not self.text.strip():
            raise ValueError("text must not be blank")

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
        }


@dataclass(frozen=True)
class SpeakerTurn:
    """One speaker's uninterrupted activity, in session-relative milliseconds."""

    speaker_id: str
    start_ms: int
    end_ms: int
    source: Source
    exclusive: bool

    def __post_init__(self) -> None:
        if not self.speaker_id:
            raise ValueError("speaker_id must not be empty")
        if not isinstance(self.source, Source):
            object.__setattr__(self, "source", Source(self.source))
        for value in (self.start_ms, self.end_ms):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError("timestamps must be integers")
        # Unlike a transcript event, a zero-length turn is meaningless: it
        # covers no audio and can never win a coverage comparison.
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("timestamps must satisfy 0 <= start_ms < end_ms")

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def overlap_ms(self, start_ms: int, end_ms: int) -> int:
        """Milliseconds this turn shares with a half-open interval."""
        return max(0, min(self.end_ms, end_ms) - max(self.start_ms, start_ms))

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker_id": self.speaker_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "exclusive": self.exclusive,
        }


@dataclass(frozen=True)
class DiarizationResult:
    model: str
    model_revision: str
    raw_turns: tuple[SpeakerTurn, ...]
    exclusive_turns: tuple[SpeakerTurn, ...]

    def __post_init__(self) -> None:
        for name in ("model", "model_revision"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        for name in ("raw_turns", "exclusive_turns"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def speaker_ids(self) -> tuple[str, ...]:
        return tuple(sorted({turn.speaker_id for turn in self.raw_turns}))


def seconds_to_ms(seconds: float) -> int:
    """Convert pipeline seconds to milliseconds, rounding halves upward.

    Python's built-in `round` is half-to-even, so 0.0005 and 0.0015 would round
    in opposite directions. Diarization boundaries are compared against
    transcript timestamps, and a rule that depends on the parity of the
    neighbouring millisecond is not one anybody can reason about, so halves go
    up consistently instead.
    """
    if not math.isfinite(seconds):
        raise ValueError("timestamp must be a finite number of seconds")
    return math.floor(seconds * 1000.0 + 0.5)


def speaker_label(source: Source, index: int) -> str:
    """Anonymous, source-scoped, session-local speaker label."""
    if index < 0:
        raise ValueError("speaker index must not be negative")
    return f"{_SOURCE_PREFIX[Source(source)]}_SPEAKER_{index:02d}"


def normalize_turns(
    raw: Sequence[tuple[str, float, float]],
    exclusive: Sequence[tuple[str, float, float]],
    *,
    source: Source,
) -> tuple[tuple[SpeakerTurn, ...], tuple[SpeakerTurn, ...]]:
    """Turn pipeline-local `(label, start_s, end_s)` triples into stable turns.

    Pipeline labels are an implementation detail that can differ between runs,
    so they are replaced by anonymous labels ordered by when each speaker first
    talks. Ordering off the *raw* turns, as the design requires, means the
    exclusive view cannot silently renumber speakers when overlap removal
    happens to clip somebody's opening words.
    """
    source = Source(source)
    raw_spans = _to_spans(raw)
    exclusive_spans = _to_spans(exclusive)
    mapping = _label_mapping(raw_spans, exclusive_spans, source)
    return (
        _build_turns(raw_spans, mapping, source, exclusive=False),
        _build_turns(exclusive_spans, mapping, source, exclusive=True),
    )


def sort_turns(turns: Iterable[SpeakerTurn]) -> tuple[SpeakerTurn, ...]:
    """Order turns by start, then end, then speaker: a total, stable order."""
    return tuple(
        sorted(turns, key=lambda turn: (turn.start_ms, turn.end_ms, turn.speaker_id))
    )


def _to_spans(triples: Sequence[tuple[str, float, float]]) -> list[tuple[str, int, int]]:
    """Round to milliseconds and drop spans too short to survive the conversion.

    A turn narrower than a millisecond carries no information that could be
    aligned to a transcript segment, and it cannot be represented by
    `SpeakerTurn`. Dropping it is deliberate: raising would let one hairline
    boundary fail an otherwise good session.
    """
    spans: list[tuple[str, int, int]] = []
    for label, start_s, end_s in triples:
        if not label:
            raise ValueError("pipeline speaker label must not be empty")
        start_ms = seconds_to_ms(start_s)
        end_ms = seconds_to_ms(end_s)
        if start_ms < 0:
            raise ValueError("pipeline produced a negative timestamp")
        if end_ms <= start_ms:
            continue
        spans.append((str(label), start_ms, end_ms))
    return spans


def _label_mapping(
    raw_spans: Sequence[tuple[str, int, int]],
    exclusive_spans: Sequence[tuple[str, int, int]],
    source: Source,
) -> dict[str, str]:
    first_seen: dict[str, int] = {}
    for label, start_ms, _ in raw_spans:
        if label not in first_seen or start_ms < first_seen[label]:
            first_seen[label] = start_ms
    # A speaker present only in the exclusive view has no raw first turn to sort
    # by. It should not happen, but ordering must stay total and deterministic
    # rather than depending on dictionary insertion, so such labels are ordered
    # after every raw speaker by their own first appearance.
    extra: dict[str, int] = {}
    for label, start_ms, _ in exclusive_spans:
        if label in first_seen:
            continue
        if label not in extra or start_ms < extra[label]:
            extra[label] = start_ms
    ordered = sorted(first_seen.items(), key=lambda item: (item[1], item[0]))
    ordered += sorted(extra.items(), key=lambda item: (item[1], item[0]))
    return {label: speaker_label(source, index) for index, (label, _) in enumerate(ordered)}


def _build_turns(
    spans: Sequence[tuple[str, int, int]],
    mapping: Mapping[str, str],
    source: Source,
    *,
    exclusive: bool,
) -> tuple[SpeakerTurn, ...]:
    return sort_turns(
        SpeakerTurn(
            speaker_id=mapping[label],
            start_ms=start_ms,
            end_ms=end_ms,
            source=source,
            exclusive=exclusive,
        )
        for label, start_ms, end_ms in spans
    )
