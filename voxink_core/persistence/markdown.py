"""Append-only, rotating Markdown transcript storage."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TextIO

from ..models import SessionConfig, TranscriptEvent
from .transcript_format import format_header, format_timestamp


PART_PREFIX = "transcript-part-"
COMPLETE_MARKER = "<!-- voxink-part-status: complete -->"
RECOVERED_MARKER = "<!-- voxink-part-status: recovered-after-interruption -->"


class MarkdownTranscriptWriter:
    """Write finalized events and rotate without resetting session timestamps."""

    def __init__(
        self,
        session_dir: str | Path,
        config: SessionConfig,
        *,
        max_part_seconds: float = 6 * 60 * 60,
        max_part_bytes: int = 5 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        durable_flush: bool = True,
    ) -> None:
        if max_part_seconds <= 0 or max_part_bytes <= 0:
            raise ValueError("rotation limits must be positive")
        self.session_dir = Path(session_dir)
        self.config = config
        self.max_part_seconds = max_part_seconds
        self.max_part_bytes = max_part_bytes
        self._clock = clock
        self._wall_clock = wall_clock
        self._durable_flush = durable_flush
        self._handle: TextIO | None = None
        self._path: Path | None = None
        self._part = 0
        self._part_started_clock = 0.0
        self._session_started: datetime | None = None
        self._part_has_events = False

    @property
    def current_path(self) -> Path | None:
        return self._path

    @property
    def part_number(self) -> int:
        return self._part

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    def open(self) -> Path:
        if self._handle is not None:
            assert self._path is not None
            return self._path
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._recover_incomplete_parts()
        self._session_started = self._wall_clock()
        return self._open_next_part()

    def append(self, event: TranscriptEvent) -> Path | None:
        """Append one final event; partial events deliberately remain ephemeral."""
        if not event.is_final:
            return None
        if event.session_id != self.config.session_id:
            raise ValueError("event belongs to a different session")
        if event.source not in self.config.sources:
            raise ValueError("event source is not enabled for this session")
        if self._handle is None:
            self.open()

        block = _event_block(event)
        encoded_size = len(block.encode("utf-8"))
        if self._should_rotate(encoded_size):
            self._finish_part(COMPLETE_MARKER)
            self._open_next_part()

        assert self._handle is not None and self._path is not None
        self._handle.write(block)
        self._flush()
        self._part_has_events = True
        return self._path

    def append_mark(self, at_ms: int, label: str = "") -> Path | None:
        """Write a user marker at a session-relative time.

        A marker is not speech: it gets its own header so a reader can find it
        and the diarization parser can skip it.
        """
        if at_ms < 0:
            raise ValueError("at_ms must be non-negative")
        text = " ".join(label.split())
        block = f"**[{format_timestamp(at_ms)}] [MARK]**\n{text}\n\n" if text else (
            f"**[{format_timestamp(at_ms)}] [MARK]**\n\n"
        )
        return self._append_block(block)

    def append_translation(self, event: TranscriptEvent, language: str, text: str) -> Path | None:
        """Quote a translation under the transcript, keyed by the block's time.

        Translation arrives after the block it belongs to was already written,
        so the quote names the block's start time rather than assuming it is
        the last thing on the page.
        """
        if event.session_id != self.config.session_id:
            raise ValueError("event belongs to a different session")
        cleaned = " ".join(text.split())
        if not cleaned:
            return None
        block = f"> [{format_timestamp(event.start_ms)} · {language}] {cleaned}\n\n"
        return self._append_block(block)

    def _append_block(self, block: str) -> Path:
        if self._handle is None:
            self.open()
        encoded_size = len(block.encode("utf-8"))
        if self._should_rotate(encoded_size):
            self._finish_part(COMPLETE_MARKER)
            self._open_next_part()
        assert self._handle is not None and self._path is not None
        self._handle.write(block)
        self._flush()
        self._part_has_events = True
        return self._path

    def close(self) -> None:
        self._finish_part(COMPLETE_MARKER)

    def __enter__(self) -> "MarkdownTranscriptWriter":
        self.open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        # An exception simulates an interrupted part for recovery on next launch.
        if exc_type is None:
            self.close()
        elif self._handle is not None:
            self._handle.close()
            self._handle = None

    def _open_next_part(self) -> Path:
        existing = self._existing_parts()
        highest = max((_part_number(path) for path in existing), default=0)
        self._part = max(self._part, highest) + 1
        path = self.session_dir / f"{PART_PREFIX}{self._part:03d}.md"
        # Exclusive creation prevents accidental overwrite after a crash or race.
        handle = path.open("x", encoding="utf-8", newline="\n")
        self._handle = handle
        self._path = path
        self._part_started_clock = self._clock()
        self._part_has_events = False
        part_started = self._wall_clock()
        if self._session_started is None:
            self._session_started = part_started
        handle.write(_front_matter(self.config, self._part, self._session_started, part_started))
        self._flush()
        return path

    def _should_rotate(self, next_block_bytes: int) -> bool:
        assert self._path is not None
        elapsed = self._clock() - self._part_started_clock
        projected_size = self._path.stat().st_size + next_block_bytes
        # Never rotate an empty part: a very small test/user limit or one large
        # segment must not create an infinite series of header-only files.
        return self._part_has_events and (
            elapsed >= self.max_part_seconds or projected_size > self.max_part_bytes
        )

    def _finish_part(self, marker: str) -> None:
        if self._handle is None:
            return
        self._handle.write(f"\n{marker}\n")
        self._flush()
        self._handle.close()
        self._handle = None

    def _flush(self) -> None:
        assert self._handle is not None
        self._handle.flush()
        if self._durable_flush:
            os.fsync(self._handle.fileno())

    def _existing_parts(self) -> list[Path]:
        return sorted(self.session_dir.glob(f"{PART_PREFIX}[0-9][0-9][0-9].md"))

    def _recover_incomplete_parts(self) -> None:
        for path in self._existing_parts():
            tail = _read_tail(path)
            if not tail.rstrip().endswith((COMPLETE_MARKER, RECOVERED_MARKER)):
                with path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(f"\n{RECOVERED_MARKER}\n")
                    handle.flush()
                    if self._durable_flush:
                        os.fsync(handle.fileno())


def _front_matter(
    config: SessionConfig,
    part: int,
    session_started: datetime,
    part_started: datetime,
) -> str:
    sources = ", ".join(_yaml_quote(source.value) for source in config.sources)
    applications = ", ".join(_yaml_quote(app) for app in config.applications)
    languages = ", ".join(_yaml_quote(lang) for lang in config.languages)
    return (
        "---\n"
        "schema_version: 1\n"
        f"session_id: {_yaml_quote(config.session_id)}\n"
        f"part: {part}\n"
        f"session_started: {_yaml_quote(session_started.isoformat())}\n"
        f"part_started: {_yaml_quote(part_started.isoformat())}\n"
        f"sources: [{sources}]\n"
        f"applications: [{applications}]\n"
        f"model: {_yaml_quote(config.model)}\n"
        f"provider: {_yaml_quote(config.provider)}\n"
        f"languages: [{languages}]\n"
        f"audio_saved: {str(config.audio_saved).lower()}\n"
        f"audio_separate: {str(config.save_separate_audio).lower()}\n"
        f"audio_combined: {str(config.save_combined_audio).lower()}\n"
        "---\n\n"
        "## Transcript\n\n"
    )


def _event_block(event: TranscriptEvent) -> str:
    # A per-application stream names its application inside the source tag,
    # so the tag stays one bracket and the speaker bracket keeps its place.
    channel = (event.channel_name or event.channel) if event.source.value == "system" else None
    header = format_header(
        event.start_ms, event.end_ms, event.source,
        channel=" ".join(channel.split()) if channel else None,
        speaker=event.speaker,
    )
    return f"{header}\n{event.text.strip()}\n\n"


def _yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _part_number(path: Path) -> int:
    return int(path.stem.removeprefix(PART_PREFIX))


def _read_tail(path: Path, size: int = 256) -> str:
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - size))
        return handle.read().decode("utf-8", errors="replace")
