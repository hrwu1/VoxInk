"""Small, model-independent turn buffers for the opt-in Diart experiment.

Times are integer milliseconds relative to the replay/capture, before the
session offset is added. A speaker tuple describes concurrent voices; it never
implies waveform separation or verified word attribution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Span:
    start_ms: int
    end_ms: int
    speakers: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("span must have a positive duration and nonnegative start")
        if self.speakers != tuple(sorted(set(self.speakers))):
            raise ValueError("speakers must be unique and sorted")


def partition(start_ms: int, end_ms: int, tracks: Iterable[Span]) -> list[Span]:
    """Cover the whole interval, including overlaps and unassigned regions."""
    if end_ms <= start_ms:
        return []
    clipped = [
        Span(max(start_ms, t.start_ms), min(end_ms, t.end_ms), t.speakers)
        for t in tracks if t.end_ms > start_ms and t.start_ms < end_ms
    ]
    edges = sorted({start_ms, end_ms, *(p for t in clipped for p in (t.start_ms, t.end_ms))})
    result: list[Span] = []
    for a, b in zip(edges, edges[1:]):
        labels = tuple(sorted({s for t in clipped if t.start_ms < b and t.end_ms > a for s in t.speakers}))
        if result and result[-1].speakers == labels:
            result[-1] = Span(result[-1].start_ms, b, labels)
        else:
            result.append(Span(a, b, labels))
    return result


class TurnBuffer:
    """Bound contiguous turns without discarding even one-frame interjections.

    Unknown and overlapping intervals have their own buffers. Repeated online
    windows cannot duplicate audio: callers must feed a contiguous partition.
    """

    def __init__(self, max_ms: int = 8_000) -> None:
        if max_ms <= 0:
            raise ValueError("max_ms must be positive")
        self.max_ms = max_ms
        self.pending: Span | None = None
        self.cursor_ms = 0

    def feed(self, span: Span) -> list[Span]:
        if span.start_ms != self.cursor_ms:
            raise ValueError("turn input must be contiguous and monotonic")
        self.cursor_ms = span.end_ms
        ready: list[Span] = []
        if self.pending and self.pending.speakers != span.speakers:
            ready.append(self.pending)
            self.pending = None
        start = self.pending.start_ms if self.pending else span.start_ms
        while span.end_ms - start >= self.max_ms:
            ready.append(Span(start, start + self.max_ms, span.speakers))
            start += self.max_ms
        self.pending = Span(start, span.end_ms, span.speakers) if start < span.end_ms else None
        return ready

    def flush(self) -> list[Span]:
        result = [self.pending] if self.pending else []
        self.pending = None
        return result


def context_window(span: Span, available_ms: int, *, pad_ms: int = 250, min_ms: int = 2_000) -> tuple[int, int]:
    """Keep context around short speech; callers must mark its attribution unsure."""
    if available_ms < span.end_ms or pad_ms < 0 or min_ms < 0:
        raise ValueError("invalid context bounds")
    start = max(0, span.start_ms - pad_ms)
    end = min(available_ms, span.end_ms + pad_ms)
    missing = max(0, min_ms - (end - start))
    start = max(0, start - (missing + 1) // 2)
    end = min(available_ms, max(end, start + min_ms))
    start = max(0, min(start, end - min_ms))
    return start, end
