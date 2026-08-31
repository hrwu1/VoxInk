"""Read the transcript artifacts a session left behind.

`MarkdownTranscriptWriter` owns the part naming and completion markers; this
module owns reading them back, so no caller has to reverse engineer a filename
convention (`PROJECT_SPEC.md` §14.6).

This is deliberately a filesystem scan. It is the stopgap the durable session
registry replaces: it can report artifacts that exist, but it cannot report a
session's lifecycle state, revision, or recovery status without opening files.
Keeping it here means the service, not the CLI, owns the scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .persistence.markdown import COMPLETE_MARKER, PART_PREFIX, RECOVERED_MARKER


PART_GLOB = f"{PART_PREFIX}[0-9][0-9][0-9].md"
_STATUS_MARKERS = ((COMPLETE_MARKER, "complete"), (RECOVERED_MARKER, "recovered"))


@dataclass(frozen=True, slots=True)
class TranscriptPart:
    part: int
    path: Path
    size_bytes: int
    modified_at: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "part": self.part,
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class TranscriptArtifacts:
    session_id: str
    directory: Path
    parts: tuple[TranscriptPart, ...]
    metadata: dict[str, str] = field(default_factory=dict)
    audio_bytes: int = 0

    @property
    def size_bytes(self) -> int:
        return sum(part.size_bytes for part in self.parts)

    @property
    def total_bytes(self) -> int:
        """Everything the session left on disk, transcript and audio together.

        Audio dominates this by orders of magnitude, which is the whole reason
        it is worth reporting: a session's transcript is kilobytes and its
        recording is hundreds of megabytes.
        """
        return self.size_bytes + self.audio_bytes

    @property
    def is_interrupted(self) -> bool:
        return any(part.status == "interrupted" for part in self.parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "directory": str(self.directory),
            "parts": [part.to_dict() for part in self.parts],
            "part_count": len(self.parts),
            "size_bytes": self.size_bytes,
            "audio_bytes": self.audio_bytes,
            "total_bytes": self.total_bytes,
            "interrupted": self.is_interrupted,
            "metadata": dict(self.metadata),
        }


def discover_transcripts(output_root: str | Path) -> tuple[TranscriptArtifacts, ...]:
    """List every session directory holding at least one transcript part."""
    root = Path(output_root).expanduser()
    if not root.is_dir():
        return ()
    found: list[TranscriptArtifacts] = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        artifacts = read_transcript(root, directory.name)
        if artifacts is not None:
            found.append(artifacts)
    # Session identifiers are timestamps, so newest-first needs no stat call.
    found.reverse()
    return tuple(found)


def read_transcript(output_root: str | Path, session_id: str) -> TranscriptArtifacts | None:
    """Describe one session's transcript parts, or None when it has none."""
    directory = _session_directory(output_root, session_id)
    if directory is None:
        return None
    parts = tuple(_describe_part(path) for path in sorted(directory.glob(PART_GLOB)))
    if not parts:
        return None
    return TranscriptArtifacts(
        session_id=session_id,
        directory=directory,
        parts=parts,
        metadata=_front_matter(parts[0].path),
        audio_bytes=_audio_bytes(directory),
    )


def _audio_bytes(directory: Path) -> int:
    """Total size of retained WAV parts, from directory entries alone.

    Sizes come from `stat`, so reporting what a session costs never opens an
    audio file.
    """
    total = 0
    for path in directory.glob("*.wav"):
        try:
            total += path.stat().st_size
        except OSError:
            # A part removed between listing and stat is simply not counted.
            continue
    return total


def read_transcript_text(
    output_root: str | Path, session_id: str, *, part: int | None = None
) -> str:
    """Return the transcript body, without the per-part front matter."""
    artifacts = read_transcript(output_root, session_id)
    if artifacts is None:
        raise FileNotFoundError(f"no transcript parts for session: {session_id}")
    selected = artifacts.parts
    if part is not None:
        selected = tuple(item for item in artifacts.parts if item.part == part)
        if not selected:
            raise FileNotFoundError(f"no transcript part {part} for session: {session_id}")
    return "\n".join(_body(item.path) for item in selected).strip() + "\n"


def _session_directory(output_root: str | Path, session_id: str) -> Path | None:
    """Resolve a session directory that stays inside the output root."""
    root = Path(output_root).expanduser().resolve()
    if not session_id or "/" in session_id or session_id in {".", ".."}:
        return None
    directory = (root / session_id).resolve()
    if directory.parent != root or not directory.is_dir():
        return None
    return directory


def _describe_part(path: Path) -> TranscriptPart:
    stat = path.stat()
    return TranscriptPart(
        part=int(path.stem.removeprefix(PART_PREFIX)),
        path=path,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        status=_status(path),
    )


def _status(path: Path) -> str:
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 256))
        tail = handle.read().decode("utf-8", errors="replace").rstrip()
    for marker, status in _STATUS_MARKERS:
        if tail.endswith(marker):
            return status
    # No marker means the writer never closed the part: either it is being
    # written right now, or the process died before it could be finished.
    return "interrupted"


def _front_matter(path: Path) -> dict[str, str]:
    """Read the leading YAML block as flat strings, without a YAML dependency."""
    metadata: dict[str, str] = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        if handle.readline().rstrip("\n") != "---":
            return metadata
        for line in handle:
            line = line.rstrip("\n")
            if line == "---":
                break
            key, separator, value = line.partition(":")
            if separator:
                metadata[key.strip()] = value.strip().strip('"')
    return metadata


def _body(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if text.startswith("---\n"):
        _, _, remainder = text.partition("\n---\n")
        text = remainder or text
    for marker, _ in _STATUS_MARKERS:
        text = text.replace(marker, "")
    return text.strip()
