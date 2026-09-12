"""Canonical transcript headers, shared by writers and derived readers.

Plain headers retain their original representation. Headers with escaped
labels carry an invisible marker so legacy literal backslashes are never
mistaken for escapes when old transcript parts are read.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from ..models import Source


_ESCAPING_MARKER = " <!-- voxink-header-escaping: 1 -->"
_SOURCE_BY_TAG = {"MIC": Source.MICROPHONE, "SYSTEM": Source.SYSTEM}
_TAG_BY_SOURCE = {source: tag for tag, source in _SOURCE_BY_TAG.items()}
# Hours widen beyond two digits. Escaped closing brackets can occur in a
# channel; the speaker is the final field and also accepts legacy raw brackets.
HEADER_PATTERN = re.compile(
    r"^\*\*\[(?P<start>\d{2,}:\d{2}:\d{2}\.\d{3})-(?P<end>\d{2,}:\d{2}:\d{2}\.\d{3})\] "
    r"\[(?P<source>MIC|SYSTEM)(?:: (?P<channel>[^\r\n]+?))?\]"
    r"(?: \[(?P<speaker>[^\r\n]+)\])?\*\*"
    rf"(?P<escaped>{re.escape(_ESCAPING_MARKER)})?$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class TranscriptHeader:
    start_ms: int
    end_ms: int
    source: Source
    channel: str | None = None
    speaker: str | None = None


def parse_header(text: str) -> TranscriptHeader | None:
    match = HEADER_PATTERN.fullmatch(text)
    if match is None:
        return None
    labels = [match.group("channel"), match.group("speaker")]
    if match.group("escaped"):
        escapes = {"\\": "\\", "[": "[", "]": "]", "n": "\n", "r": "\r"}
        labels = [
            re.sub(r"\\([\\\[\]nr])", lambda item: escapes[item[1]], label)
            if label is not None else None
            for label in labels
        ]
    return TranscriptHeader(
        parse_timestamp(match.group("start")),
        parse_timestamp(match.group("end")),
        _SOURCE_BY_TAG[match.group("source")],
        *labels,
    )


def format_header(
    start_ms: int,
    end_ms: int,
    source: Source,
    *,
    channel: str | None = None,
    speaker: str | None = None,
) -> str:
    labels = [channel, speaker]
    encoded = [
        label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        .replace("\n", "\\n").replace("\r", "\\r")
        if label else label
        for label in labels
    ]
    tag = source_tag(source)
    if encoded[0]:
        tag += f": {encoded[0]}"
    speaker_tag = f" [{encoded[1]}]" if encoded[1] else ""
    marker = _ESCAPING_MARKER if encoded != labels else ""
    return (
        f"**[{format_timestamp(start_ms)}-{format_timestamp(end_ms)}] "
        f"[{tag}]{speaker_tag}**{marker}"
    )


def source_tag(source: Source) -> str:
    return _TAG_BY_SOURCE[Source(source)]


def format_timestamp(milliseconds: int) -> str:
    """Render session milliseconds as `HH:MM:SS.mmm`."""
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def parse_timestamp(value: str) -> int:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(".")
    return int(hours) * 3_600_000 + int(minutes) * 60_000 + int(seconds) * 1_000 + int(millis)
