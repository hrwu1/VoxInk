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
from ..persistence.markdown import format_timestamp
from .alignment import SegmentLabel
from .splitting import Piece
from .models import (
    DiarizationError,
    DiarizationErrorCode,
    DiarizationResult,
    SpeakerTurn,
)
from .transcript import TranscriptBlock, file_digest, read_blocks, source_tag


SCHEMA_VERSION = 1
TURNS_FILENAME = "speaker-turns.json"
MARKDOWN_FILENAME = "transcript-with-speakers.md"
PRODUCER_NAME = "voxink-pyannote-community-1"
#: Bumped when the meaning of the output changes. Version 2 added `pieces`:
#: a block may be split at speaker boundaries by forced word alignment.
PRODUCER_VERSION = "2"

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
    aligner: Mapping[str, str] | None = None,
    speakers: Mapping[str, Mapping[str, Any]] | None = None,
) -> ArtifactPaths:
    """Publish both derived artifacts, or leave the previous pair untouched.

    `aligner` names the word-alignment model that split blocks into pieces,
    as `model_id` and `model_revision`, or is None when no block was aligned.
    `speakers` carries, per anonymous speaker id, the optional `name`,
    `registry_id`, `similarity`, and `embedding` the identification step
    produced; names appear in the Markdown view in place of the ids.
    """
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
        aligner=aligner,
        speakers=speakers,
    )
    markdown = render_markdown(
        session_id=session_id,
        source=source,
        result=result,
        labels=labels,
        source_artifacts=source_artifacts,
        created_at=stamp,
        aligner=aligner,
        names=speaker_names(document),
    )
    validate_document(document)
    return _publish(directory, document, markdown)


def _publish(directory: Path, document: Mapping[str, Any], markdown: str) -> ArtifactPaths:
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


def speaker_names(document: Mapping[str, Any]) -> dict[str, str]:
    """Anonymous id to display name, for every speaker the document names."""
    names = {}
    for speaker_id, info in (document.get("speakers") or {}).items():
        name = (info or {}).get("name")
        if isinstance(name, str) and name.strip():
            names[str(speaker_id)] = " ".join(name.split())
    return names


def rename_speaker(
    session_dir: str | Path,
    speaker_id: str,
    name: str | None,
    *,
    registry_id: str | None = None,
    similarity: float | None = None,
) -> dict[str, Any]:
    """Name (or un-name) one anonymous speaker and re-render the Markdown view.

    The JSON keeps its turns, segments, and embeddings; only the `speakers`
    entry changes, and the readable view is rebuilt from the canonical blocks
    so it never drifts from the transcript.
    """
    directory = Path(session_dir)
    document = read_document(directory)
    if document is None:
        raise DiarizationError(
            DiarizationErrorCode.AUDIO_UNAVAILABLE, "this session has no speaker artifacts"
        )
    speakers = dict(document.get("speakers") or {})
    known = {turn["speaker_id"] for turn in document.get("raw_turns", ())}
    if speaker_id not in known and speaker_id not in speakers:
        raise DiarizationError(
            DiarizationErrorCode.INVALID_AUDIO, f"unknown speaker: {speaker_id}"
        )
    entry = dict(speakers.get(speaker_id) or {})
    cleaned = " ".join((name or "").split())[:80]
    entry["name"] = cleaned or None
    entry["registry_id"] = registry_id if cleaned else None
    entry["similarity"] = similarity if cleaned else None
    entry.setdefault("embedding", None)
    speakers[speaker_id] = entry
    document = {**document, "speakers": speakers}
    validate_document(document)
    markdown = render_from_document(directory, document)
    _publish(directory, document, markdown)
    return document


def render_from_document(session_dir: str | Path, document: Mapping[str, Any]) -> str:
    """Re-render the Markdown view from a stored document and the canonical parts."""
    directory = Path(session_dir)
    blocks = {(block.part, block.block): block for block in read_blocks(directory)}
    names = speaker_names(document)
    producer = document.get("producer") or {}
    aligner = producer.get("aligner")
    body = []
    for segment in document.get("segments", ()):
        reference = segment.get("canonical_ref") or {}
        block = blocks.get((reference.get("part"), reference.get("block")))
        if block is None:
            continue
        pieces = tuple(
            (
                int(piece["text_start"]), int(piece["text_end"]),
                int(piece["start_ms"]), int(piece["end_ms"]), piece.get("speaker_id"),
            )
            for piece in segment.get("pieces", ())
        )
        body.append(_render_block(block, segment.get("speaker_id"), pieces, names))
    header = _markdown_header(
        session_id=str(document.get("session_id", "")),
        source=Source(document.get("source", "microphone")),
        model=str(producer.get("model_id", "")),
        model_revision=str(producer.get("model_revision", "")),
        source_artifacts={
            item["path"]: item["sha256"] for item in document.get("source_artifacts", ())
        },
        created_at=str(document.get("created_at", "")),
        aligner=aligner,
        names=names,
    )
    return header + "".join(body)


def build_document(
    *,
    session_id: str,
    source: Source,
    result: DiarizationResult,
    labels: Sequence[SegmentLabel],
    source_artifacts: Mapping[str, str],
    created_at: datetime,
    aligner: Mapping[str, str] | None = None,
    speakers: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble `speaker-turns.json` as a plain dictionary."""
    identities: dict[str, dict[str, Any]] = {}
    for speaker_id in result.speaker_ids:
        info = dict((speakers or {}).get(speaker_id) or {})
        embedding = result.embeddings.get(speaker_id)
        identities[speaker_id] = {
            "name": info.get("name"),
            "registry_id": info.get("registry_id"),
            "similarity": info.get("similarity"),
            "embedding": list(embedding) if embedding else None,
        }
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
            "aligner": (
                {
                    "model_id": str(aligner["model_id"]),
                    "model_revision": str(aligner["model_revision"]),
                }
                if aligner
                else None
            ),
        },
        "created_at": _timestamp(created_at),
        "source_artifacts": [
            {"path": path, "sha256": digest}
            for path, digest in sorted(source_artifacts.items())
        ],
        "raw_turns": [turn.to_dict() for turn in result.raw_turns],
        "exclusive_turns": [turn.to_dict() for turn in result.exclusive_turns],
        "segments": [label.to_dict() for label in labels],
        "speakers": identities,
    }


def render_markdown(
    *,
    session_id: str,
    source: Source,
    result: DiarizationResult,
    labels: Sequence[SegmentLabel],
    source_artifacts: Mapping[str, str],
    created_at: datetime,
    aligner: Mapping[str, str] | None = None,
    names: Mapping[str, str] | None = None,
) -> str:
    """Render the readable view, copying canonical blocks in their original order.

    A block split by word alignment is rendered as one sub-block per piece,
    each with its own time range and label. The pieces' text, read in order,
    is the canonical block's text exactly. `names` replaces anonymous ids
    with the names the user gave or the registry recognized.
    """
    names = dict(names or {})
    header = _markdown_header(
        session_id=session_id,
        source=source,
        model=result.model,
        model_revision=result.model_revision,
        source_artifacts=source_artifacts,
        created_at=_timestamp(created_at),
        aligner=aligner,
        names=names,
    )
    return header + "".join(_block_markdown(label, names) for label in labels)


def _markdown_header(
    *,
    session_id: str,
    source: Source,
    model: str,
    model_revision: str,
    source_artifacts: Mapping[str, str],
    created_at: str,
    aligner: Mapping[str, str] | None,
    names: Mapping[str, str],
) -> str:
    digests = "\n".join(
        f'  - path: "{path}"\n    sha256: "{digest}"'
        for path, digest in sorted(source_artifacts.items())
    )
    aligner_lines = (
        f'aligner_model_id: "{aligner["model_id"]}"\n'
        f'aligner_model_revision: "{aligner["model_revision"]}"\n'
        if aligner
        else ""
    )
    name_lines = "".join(
        f'  {speaker_id}: "{name.replace(chr(34), chr(39))}"\n'
        for speaker_id, name in sorted(names.items())
    )
    speakers_block = f"speakers:\n{name_lines}" if name_lines else ""
    return (
        "---\n"
        "schema_version: 1\n"
        "derived: true\n"
        f'session_id: "{session_id}"\n'
        f'source: "{Source(source).value}"\n'
        f'producer: "{PRODUCER_NAME}"\n'
        f'producer_version: "{PRODUCER_VERSION}"\n'
        f'model_id: "{model}"\n'
        f'model_revision: "{model_revision}"\n'
        f"{aligner_lines}"
        f'created_at: "{created_at}"\n'
        f'speaker_turns: "{TURNS_FILENAME}"\n'
        f"{speakers_block}"
        "source_artifacts:\n"
        f"{digests}\n"
        "---\n\n"
        "# Transcript with speakers\n\n"
        "Derived from the canonical transcript parts listed above; it is not\n"
        "one of them. Blocks whose speaker could not be determined confidently\n"
        "are reproduced without a label. A block in which more than one person\n"
        "spoke is shown as several sub-blocks, split where the speaker changed.\n\n"
    )


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
        if "start_ms" in segment and not 0 <= segment["start_ms"] <= segment["end_ms"]:
            raise ValueError("segment times must satisfy 0 <= start_ms <= end_ms")
        _validate_pieces(segment.get("pieces", ()))
    _reject_sensitive_keys(document)


def _validate_pieces(pieces: Sequence[Mapping[str, Any]]) -> None:
    """Pieces must tile their block from its first character on, in order."""
    expected_start = 0
    for piece in pieces:
        if piece["text_start"] != expected_start or piece["text_end"] <= piece["text_start"]:
            raise ValueError("pieces must be contiguous, non-empty character ranges from 0")
        if not 0 <= piece["start_ms"] <= piece["end_ms"]:
            raise ValueError("piece times must satisfy 0 <= start_ms <= end_ms")
        if not 0.0 <= piece["speaker_coverage"] <= 1.0:
            raise ValueError("piece speaker_coverage must be a ratio within [0, 1]")
        expected_start = piece["text_end"]


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


def _block_markdown(label: SegmentLabel, names: Mapping[str, str] | None = None) -> str:
    pieces = tuple(
        (piece.text_start, piece.text_end, piece.start_ms, piece.end_ms, piece.speaker_id)
        for piece in label.pieces
    )
    return _render_block(label.block, label.speaker_id, pieces, names or {})


_PieceTuple = tuple[int, int, int, int, str | None]


def _render_block(
    block: TranscriptBlock,
    speaker_id: str | None,
    pieces: Sequence[_PieceTuple],
    names: Mapping[str, str],
) -> str:
    if pieces:
        # A block that already carries a canonical speaker is never split:
        # the transcript itself said who spoke, and this view adds to that
        # rather than second-guessing it.
        if not block.speaker:
            return "".join(_piece_markdown(block, piece, names) for piece in pieces)
    header = block.header or f"**[{block.start_ms}-{block.end_ms}] [{block.source.value}]**"
    # A block that already carries a canonical speaker keeps it; this view adds
    # information, it never overrides what the transcript itself recorded.
    if speaker_id and not block.speaker:
        header = f"{header[:-2]} [{_display(speaker_id, names)}]**"
    return f"{header}\n{block.text}\n\n"


def _piece_markdown(block: TranscriptBlock, piece: _PieceTuple, names: Mapping[str, str]) -> str:
    text_start, text_end, start_ms, end_ms, speaker_id = piece
    speaker = f" [{_display(speaker_id, names)}]" if speaker_id else ""
    header = (
        f"**[{format_timestamp(start_ms)}-{format_timestamp(end_ms)}] "
        f"[{source_tag(block.source)}]{speaker}**"
    )
    return f"{header}\n{block.text[text_start:text_end].strip()}\n\n"


def _display(speaker_id: str, names: Mapping[str, str]) -> str:
    """A name in brackets must not contain a bracket, or the header breaks."""
    name = names.get(speaker_id)
    if not name:
        return speaker_id
    return name.replace("[", "(").replace("]", ")")


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
