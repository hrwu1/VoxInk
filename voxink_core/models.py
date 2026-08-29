"""Stable, provider-neutral domain models used by the service and clients."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class Source(str, Enum):
    """The independent audio source that produced a transcript."""

    MICROPHONE = "microphone"
    SYSTEM = "system"


@dataclass(frozen=True)
class WordTimestamp:
    text: str
    start_ms: int
    end_ms: int
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("word text must not be empty")
        _validate_time_range(self.start_ms, self.end_ms)
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class TranscriptEvent:
    session_id: str
    segment_id: str
    stream_id: str
    source: Source
    start_ms: int
    end_ms: int
    text: str
    is_final: bool
    language: str | None = None
    language_confidence: float | None = None
    speaker: str | None = None
    words: tuple[WordTimestamp, ...] | None = None
    diagnostics: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in ("session_id", "segment_id", "stream_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.source, Source):
            object.__setattr__(self, "source", Source(self.source))
        _validate_time_range(self.start_ms, self.end_ms)
        if self.language_confidence is not None and not 0 <= self.language_confidence <= 1:
            raise ValueError("language_confidence must be between 0 and 1")
        if self.words is not None and not isinstance(self.words, tuple):
            object.__setattr__(self, "words", tuple(self.words))

    def to_dict(self, *, include_diagnostics: bool = False) -> dict[str, Any]:
        data = asdict(self)
        data["source"] = self.source.value
        if not include_diagnostics:
            data.pop("diagnostics")
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TranscriptEvent":
        values = dict(data)
        values["source"] = Source(values["source"])
        words = values.get("words")
        if words is not None:
            values["words"] = tuple(
                item if isinstance(item, WordTimestamp) else WordTimestamp(**item)
                for item in words
            )
        return cls(**values)


@dataclass(frozen=True)
class SessionConfig:
    """Configuration and non-secret metadata for one logical capture session."""

    session_id: str
    sources: tuple[Source, ...]
    applications: tuple[str, ...] = ()
    provider: str = "qwen-local"
    model: str = "Qwen3-ASR-1.7B"
    languages: tuple[str, ...] = ("en", "zh")
    audio_saved: bool = False
    save_separate_audio: bool = True
    save_combined_audio: bool = False
    output_directory: Path | None = None

    def __post_init__(self) -> None:
        if (
            not self.session_id
            or self.session_id in {".", ".."}
            or any(char in self.session_id for char in "/\\\0")
        ):
            raise ValueError("session_id must be a non-empty path-safe name")
        if not self.sources:
            raise ValueError("at least one source is required")
        object.__setattr__(self, "sources", tuple(Source(item) for item in self.sources))
        object.__setattr__(self, "applications", tuple(self.applications))
        object.__setattr__(self, "languages", tuple(self.languages))
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("sources must not contain duplicates")
        for name in ("provider", "model"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if self.output_directory is not None:
            object.__setattr__(self, "output_directory", Path(self.output_directory))
        if not self.audio_saved and self.save_combined_audio:
            raise ValueError("combined audio cannot be saved when audio_saved is false")
        # Recording every source and then discarding both the separate and the
        # combined tracks would burn disk for nothing, so reject it up front.
        if self.audio_saved and not (self.save_separate_audio or self.save_combined_audio):
            raise ValueError(
                "audio_saved requires save_separate_audio, save_combined_audio, or both"
            )


@dataclass(frozen=True)
class TranscriptionOptions:
    """Context needed to normalize one provider result into an event."""

    session_id: str
    segment_id: str
    stream_id: str
    source: Source
    start_ms: int = 0
    end_ms: int = 0
    language: str | None = None
    # User-defined terms that bias recognition toward names and jargon the model
    # cannot guess. Providers that have no biasing mechanism ignore it.
    context: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, Source):
            object.__setattr__(self, "source", Source(self.source))
        _validate_time_range(self.start_ms, self.end_ms)


def _validate_time_range(start_ms: int, end_ms: int) -> None:
    if isinstance(start_ms, bool) or isinstance(end_ms, bool):
        raise TypeError("timestamps must be integers")
    if not isinstance(start_ms, int) or not isinstance(end_ms, int):
        raise TypeError("timestamps must be integers")
    if start_ms < 0 or end_ms < start_ms:
        raise ValueError("timestamps must satisfy 0 <= start_ms <= end_ms")
