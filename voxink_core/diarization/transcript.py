"""Read canonical transcript parts without ever writing to them.

`MarkdownTranscriptWriter` owns the block format; this reads it back. The
transcript has no stable per-event identifier -- `segment_id` exists on the
event but is not persisted -- so a block is referenced by the part it lives in,
its position in that part, and a digest of its own bytes. The digest is what
makes staleness detectable: if a block's text changes, any speaker artifact
derived from it is provably out of date rather than quietly wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any, Iterator

from ..models import Source
from ..persistence.markdown import COMPLETE_MARKER, PART_PREFIX, RECOVERED_MARKER


PART_GLOB = f"{PART_PREFIX}[0-9][0-9][0-9].md"

_SOURCE_BY_TAG = {"MIC": Source.MICROPHONE, "SYSTEM": Source.SYSTEM}
_TAG_BY_SOURCE = {source: tag for tag, source in _SOURCE_BY_TAG.items()}

#: Matches the header `MarkdownTranscriptWriter._event_block` writes. Hours are
#: not capped at two digits: the writer widens the field rather than truncating,
#: and a session long enough to need three would otherwise silently stop
#: parsing.
_HEADER = re.compile(
    r"^\*\*\[(?P<start>\d{2,}:\d{2}:\d{2}\.\d{3})-(?P<end>\d{2,}:\d{2}:\d{2}\.\d{3})\] "
    r"\[(?P<source>MIC|SYSTEM)(?:: (?P<channel>[^\]]+))?\](?: \[(?P<speaker>[^\]]+)\])?\*\*$",
    re.MULTILINE,
)

#: Annotations the writer appends beside blocks and that are not transcript
#: text: a user's marker (its own header, with a label line under it) and a
#: translation quoted under the block it belongs to.
_MARK_HEADER = re.compile(r"^\*\*\[\d{2,}:\d{2}:\d{2}\.\d{3}\] \[MARK\]\*\*$", re.MULTILINE)
_ANNOTATION_LINE = re.compile(r"^> .*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class TranscriptBlock:
    """One finalized transcript block, addressed the way artifacts cite it."""

    part: str
    block: int
    source: Source
    start_ms: int
    end_ms: int
    text: str
    speaker: str | None
    sha256: str
    #: The header line exactly as canonical Markdown wrote it. Kept so derived
    #: artifacts can reproduce a block verbatim instead of re-deriving its
    #: timestamp formatting and risking drift from the canonical writer.
    header: str = ""

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)

    @property
    def has_valid_range(self) -> bool:
        """Whether this block spans real time and can carry a speaker label."""
        return 0 <= self.start_ms < self.end_ms

    def canonical_ref(self) -> dict[str, Any]:
        return {"part": self.part, "block": self.block, "sha256": self.sha256}


def parse_part(path: str | Path) -> tuple[TranscriptBlock, ...]:
    """Parse one transcript part into blocks, in file order."""
    part_path = Path(path)
    text = part_path.read_text(encoding="utf-8", errors="replace")
    return tuple(_blocks(part_path.name, text))


def read_blocks(session_dir: str | Path) -> tuple[TranscriptBlock, ...]:
    """Parse every transcript part in a session, ordered by part then position.

    Parts are ordered by their numeric suffix rather than lexically, and blocks
    keep file order within a part. Both writers are append-only, so file order
    is chronological order.
    """
    directory = Path(session_dir)
    blocks: list[TranscriptBlock] = []
    for part_path in sorted(directory.glob(PART_GLOB), key=_part_number):
        blocks.extend(parse_part(part_path))
    return tuple(blocks)


def part_digests(session_dir: str | Path) -> dict[str, str]:
    """Digest every transcript part, for recording what an artifact derived from."""
    directory = Path(session_dir)
    return {
        path.name: file_digest(path)
        for path in sorted(directory.glob(PART_GLOB), key=_part_number)
    }


def file_digest(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def block_digest(text: str) -> str:
    """Digest one block's canonical text.

    The digested text is the header line, a newline, and the body with trailing
    whitespace removed -- deliberately excluding the blank line that separates
    blocks, so that a part gaining a completion marker or a trailing newline
    does not invalidate every block inside it.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_tag(source: Source) -> str:
    """The header tag the canonical writer uses for a source."""
    return _TAG_BY_SOURCE[Source(source)]


def parse_timestamp(value: str) -> int:
    """Parse `HH:MM:SS.mmm` into milliseconds."""
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(".")
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1_000
        + int(millis)
    )


def _blocks(part_name: str, text: str) -> Iterator[TranscriptBlock]:
    matches = list(_HEADER.finditer(text))
    for index, match in enumerate(matches, start=1):
        # A block runs to the next header, not to the next blank line: a
        # transcript body may legitimately contain one, and splitting on blank
        # lines would truncate it.
        body_end = matches[index].start() if index < len(matches) else len(text)
        body = text[match.end() : body_end]
        for marker in (COMPLETE_MARKER, RECOVERED_MARKER):
            body = body.replace(marker, "")
        # A marker and everything under it belongs to the user, not to the
        # block; a quoted line under a block is its translation, not speech.
        mark = _MARK_HEADER.search(body)
        if mark:
            body = body[: mark.start()]
        body = _ANNOTATION_LINE.sub("", body)
        canonical = f"{match.group(0)}\n{body.strip()}"
        yield TranscriptBlock(
            part=part_name,
            block=index,
            source=_SOURCE_BY_TAG[match.group("source")],
            start_ms=parse_timestamp(match.group("start")),
            end_ms=parse_timestamp(match.group("end")),
            text=body.strip(),
            speaker=match.group("speaker"),
            sha256=block_digest(canonical),
            header=match.group(0),
        )


def _part_number(path: Path) -> int:
    return int(path.stem.removeprefix(PART_PREFIX))
