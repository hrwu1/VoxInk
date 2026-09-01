"""Write and read the derived speaker artifacts.

Two files are produced beside the transcript, and neither is canonical:
`speaker-turns.json` is the machine-readable source of truth for diarization
output, and `transcript-with-speakers.md` is a readable view of it. The
canonical `transcript-part-*.md` files are never opened for writing here.

Both are written atomically, and the JSON -- which carries the `ready` status
everything else keys off -- is published last, so a reader can never see a
completed status pointing at a half-written companion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from ..models import Source
from .alignment import SegmentLabel
from .models import (
    DiarizationError,
    DiarizationErrorCode,
    DiarizationResult,
    SpeakerTurn,
)
from .transcript import file_digest


SCHEMA_VERSION = 1
TURNS_FILENAME = "speaker-turns.json"
MARKDOWN_FILENAME = "transcript-with-speakers.md"
PRODUCER_NAME = "voxink-pyannote-community-1"
PRODUCER_VERSION = "1"

#: Derived artifacts are user data written into the user's own directory, and
#: they describe who was speaking. Group and world get nothing.
_FILE_MODE = 0o600


@dataclass(frozen=True, slots=True)
class ArtifactPaths:
    turns: Path
    markdown: Path


def write_artifacts(
    session_dir: str | Path,
    *,
    session_id: str,
    source: Source,
    result: DiarizationResult,
    labels: Sequence[SegmentLabel],
    source_artifacts: Mapping[str, str],
    created_at: datetime | None = None,
) -> ArtifactPaths:
    """Publish both derived artifacts, or leave the previous pair untouched."""
    directory = Path(session_dir)
    source = Source(source)
    stamp = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    document = build_document(
        session_id=session_id,
        source=source,
        result=result,
        labels=labels,
        source_artifacts=source_artifacts,
        created_at=stamp,
    )
    markdown = render_markdown(
        session_id=session_id,
        source=source,
        result=result,
        labels=labels,
        source_artifacts=source_artifacts,
        created_at=stamp,
    )
    validate_document(document)
    paths = ArtifactPaths(directory / TURNS_FILENAME, directory / MARKDOWN_FILENAME)
    try:
        # Markdown first: if publishing the JSON then fails, the readable view
        # is merely orphaned. Publishing the JSON first would announce `ready`
        # for a companion that does not exist.
        _atomic_write(paths.markdown, markdown.encode("utf-8"))
        _atomic_write(
            paths.turns, (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        )
    except OSError as exc:
        raise DiarizationError(
            DiarizationErrorCode.ARTIFACT_WRITE_FAILED,
            f"derived artifact could not be written: {exc}",
            artifact=getattr(exc, "filename", None) and Path(exc.filename).name,
        ) from exc
    return paths


def build_document(
    *,
    session_id: str,
    source: Source,
    result: DiarizationResult,
    labels: Sequence[SegmentLabel],
    source_artifacts: Mapping[str, str],
    created_at: datetime,
) -> dict[str, Any]:
    """Assemble `speaker-turns.json` as a plain dictionary."""
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ready",
        "session_id": session_id,
        "source": Source(source).value,
        "producer": {
            "name": PRODUCER_NAME,
            "version": PRODUCER_VERSION,
            "model_id": result.model,
            "model_revision": result.model_revision,
        },
        "created_at": _timestamp(created_at),
        "source_artifacts": [
            {"path": path, "sha256": digest}
            for path, digest in sorted(source_artifacts.items())
        ],
        "raw_turns": [turn.to_dict() for turn in result.raw_turns],
        "exclusive_turns": [turn.to_dict() for turn in result.exclusive_turns],
        "segments": [label.to_dict() for label in labels],
    }


def render_markdown(
    *,
    session_id: str,
    source: Source,
    result: DiarizationResult,
    labels: Sequence[SegmentLabel],
    source_artifacts: Mapping[str, str],
    created_at: datetime,
) -> str:
    """Render the readable view, copying canonical blocks in their original order."""
    digests = "\n".join(
        f'  - path: "{path}"\n    sha256: "{digest}"'
        for path, digest in sorted(source_artifacts.items())
    )
    header = (
        "---\n"
        "schema_version: 1\n"
        "derived: true\n"
        f'session_id: "{session_id}"\n'
        f'source: "{Source(source).value}"\n'
        f'producer: "{PRODUCER_NAME}"\n'
        f'producer_version: "{PRODUCER_VERSION}"\n'
        f'model_id: "{result.model}"\n'
        f'model_revision: "{result.model_revision}"\n'
        f'created_at: "{_timestamp(created_at)}"\n'
        f'speaker_turns: "{TURNS_FILENAME}"\n'
        "source_artifacts:\n"
        f"{digests}\n"
        "---\n\n"
        "# Transcript with speakers\n\n"
        "Derived from the canonical transcript parts listed above; it is not\n"
        "one of them. Blocks whose speaker could not be determined confidently\n"
        "are reproduced without a label.\n\n"
    )
    return header + "".join(_block_markdown(label) for label in labels)


def validate_document(document: Mapping[str, Any]) -> None:
    """Check the JSON contract before anything is published.

    This is a structural check, not a full JSON Schema: it exists so a bug in
    assembly fails here rather than becoming a malformed file a future reader
    has to defend against.
    """
    required = {
        "schema_version",
        "status",
        "session_id",
        "source",
        "producer",
        "created_at",
        "source_artifacts",
        "raw_turns",
        "exclusive_turns",
        "segments",
    }
    missing = sorted(required - set(document))
    if missing:
        raise ValueError(f"speaker-turns document is missing keys: {', '.join(missing)}")
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported speaker-turns schema_version")
    if document["status"] not in {"ready", "failed", "stale"}:
        raise ValueError("unsupported speaker-turns status")
    if not document["session_id"] or not document["source"]:
        raise ValueError("speaker-turns document must identify its session and source")
    Source(document["source"])
    producer = document["producer"]
    for key in ("name", "version", "model_id", "model_revision"):
        if not producer.get(key):
            raise ValueError(f"producer.{key} must not be empty")
    for group in ("raw_turns", "exclusive_turns"):
        for turn in document[group]:
            if not 0 <= turn["start_ms"] < turn["end_ms"]:
                raise ValueError(f"{group} contains an invalid time range")
    for segment in document["segments"]:
        reference = segment["canonical_ref"]
        if not reference.get("part") or not reference.get("sha256"):
            raise ValueError("every segment must reference a canonical block")
        if not 0.0 <= segment["speaker_coverage"] <= 1.0:
            raise ValueError("speaker_coverage must be a ratio within [0, 1]")
    _reject_sensitive_keys(document)


def read_document(session_dir: str | Path) -> dict[str, Any] | None:
    """Read `speaker-turns.json`, or None when this session has none."""
    path = Path(session_dir) / TURNS_FILENAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise DiarizationError(
            DiarizationErrorCode.ARTIFACT_WRITE_FAILED,
            f"existing speaker artifact could not be read: {exc}",
            artifact=TURNS_FILENAME,
        ) from exc


def stale_sources(session_dir: str | Path, document: Mapping[str, Any]) -> tuple[str, ...]:
    """Names of recorded sources that no longer match what is on disk.

    A changed source means the artifact describes a transcript that no longer
    exists. It is regenerated wholesale, never patched: there is no way to tell
    which blocks moved from a digest that only says "different".
    """
    directory = Path(session_dir)
    changed: list[str] = []
    for entry in document.get("source_artifacts", ()):
        path = directory / entry["path"]
        if not path.is_file() or file_digest(path) != entry["sha256"]:
            changed.append(entry["path"])
    return tuple(sorted(changed))


def _block_markdown(label: SegmentLabel) -> str:
    block = label.block
    header = block.header or f"**[{block.start_ms}-{block.end_ms}] [{block.source.value}]**"
    # A block that already carries a canonical speaker keeps it; this view adds
    # information, it never overrides what the transcript itself recorded.
    if label.speaker_id and not block.speaker:
        header = f"{header[:-2]} [{label.speaker_id}]**"
    return f"{header}\n{block.text}\n\n"


def _atomic_write(path: Path, payload: bytes) -> None:
    """Replace a file's contents, or leave the previous version in place."""
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        dir=directory, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, _FILE_MODE)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _reject_sensitive_keys(document: Mapping[str, Any]) -> None:
    """Guard the rule that no transcript text or credential reaches this file."""
    forbidden = {"text", "token", "hf_token", "authorization", "audio"}
    def walk(node: Any) -> Iterable[str]:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key in forbidden:
                    yield key
                yield from walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                yield from walk(item)

    found = sorted(set(walk(document)))
    if found:
        raise ValueError(
            f"speaker-turns document must not carry: {', '.join(found)}"
        )


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
