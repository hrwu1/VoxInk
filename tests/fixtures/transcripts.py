"""Build real canonical transcript parts for diarization tests.

These go through `MarkdownTranscriptWriter` rather than writing Markdown by
hand. A reader tested against hand-written fixtures only proves it can parse
what the test author imagined; going through the real writer means a change to
the canonical format fails these tests instead of silently producing empty
speaker artifacts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from voxink_core.models import SessionConfig, Source, TranscriptEvent
from voxink_core.persistence.markdown import MarkdownTranscriptWriter


SESSION_ID = "2026-08-31_14-30-00-000000"


def make_event(
    start_ms: int,
    end_ms: int,
    text: str,
    *,
    source: Source = Source.MICROPHONE,
    session_id: str = SESSION_ID,
    speaker: str | None = None,
) -> TranscriptEvent:
    return TranscriptEvent(
        session_id=session_id,
        segment_id=f"seg-{start_ms}",
        stream_id=f"{source.value}-1",
        source=source,
        start_ms=start_ms,
        end_ms=end_ms,
        text=text,
        is_final=True,
        speaker=speaker,
    )


def write_session(
    directory: str | Path,
    events: Iterable[TranscriptEvent],
    *,
    session_id: str = SESSION_ID,
    sources: Sequence[Source] = (Source.MICROPHONE,),
    max_part_bytes: int = 5 * 1024 * 1024,
) -> SessionConfig:
    """Write events as canonical parts, rotating when `max_part_bytes` is small."""
    config = SessionConfig(
        session_id=session_id,
        sources=tuple(sources),
        audio_saved=True,
        save_separate_audio=True,
    )
    writer = MarkdownTranscriptWriter(
        directory,
        config,
        max_part_bytes=max_part_bytes,
        durable_flush=False,
    )
    writer.open()
    for event in events:
        writer.append(event)
    writer.close()
    return config
