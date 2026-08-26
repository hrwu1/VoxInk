"""Provider interface for batch and streaming speech-to-text engines."""

from __future__ import annotations

from collections.abc import AsyncIterable
from pathlib import Path
from typing import Protocol, Union, runtime_checkable

from ..models import TranscriptEvent, TranscriptionOptions


AudioInput = Union[str, Path, bytes]


@runtime_checkable
class TranscriptionProvider(Protocol):
    name: str

    @property
    def is_loaded(self) -> bool: ...

    async def load(self) -> None: ...

    async def transcribe_file(
        self, audio: AudioInput, options: TranscriptionOptions
    ) -> TranscriptEvent: ...

    async def transcribe_stream(
        self,
        frames: AsyncIterable[bytes],
        options: TranscriptionOptions,
    ) -> AsyncIterable[TranscriptEvent]: ...

    async def unload(self) -> None: ...
