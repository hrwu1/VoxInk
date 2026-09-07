"""Small, model-independent turn buffers for the opt-in Diart experiment.

Times are integer milliseconds relative to the replay/capture, before the
session offset is added. A speaker tuple describes concurrent voices; it never
implies waveform separation or verified word attribution.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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


def partition(start_ms: int, end_ms: int, tracks: Iterable[Span], *, edge_tolerance_ms: int = 0) -> list[Span]:
    """Cover the whole interval, including overlaps and unassigned regions."""
    if end_ms <= start_ms:
        return []
    if edge_tolerance_ms < 0:
        raise ValueError("edge tolerance must be nonnegative")
    clipped = [
        Span(start_ms if t.start_ms <= start_ms + edge_tolerance_ms else t.start_ms,
             end_ms if t.end_ms >= end_ms - edge_tolerance_ms else t.end_ms, t.speakers)
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


def context_window(span: Span, available_ms: int, *, pad_ms: int = 250, min_ms: int = 2_000,
                   floor_ms: int = 0) -> tuple[int, int]:
    """Keep context around short speech; callers must mark its attribution unsure.

    `floor_ms` bounds how far back the window may reach, so a caller passing the
    previous turn's end never re-sends a whole turn and consecutive windows
    overlap by at most `pad_ms`. It also bounds the backward half of the `min_ms`
    expansion, which is then taken from the audio ahead instead; that audio
    belongs to the next turn, so a live caller wanting no repeated captions
    passes `min_ms=0` and accepts a short window for a short turn.
    """
    if available_ms < span.end_ms or pad_ms < 0 or min_ms < 0 or floor_ms < 0:
        raise ValueError("invalid context bounds")
    if floor_ms >= span.end_ms:
        raise ValueError("context floor leaves no untranscribed audio in this span")
    start = max(floor_ms, span.start_ms - pad_ms)
    end = min(available_ms, span.end_ms + pad_ms)
    missing = max(0, min_ms - (end - start))
    start = max(floor_ms, start - (missing + 1) // 2)
    end = min(available_ms, max(end, start + min_ms))
    start = max(floor_ms, min(start, end - min_ms))
    return start, end


class SpanSmoother:
    """Absorb brief unassigned gaps so one turn is not split into fragments.

    Diart binarizes each step on its own, so a single turn usually arrives as
    several spans separated by sub-second unassigned gaps, and a caller that
    treats every span as a turn shows an unlabeled caption between two halves of
    one sentence. A gap shorter than `max_pause_ms` therefore takes the speakers
    of the turn it interrupts; a longer one stays unassigned and ends that turn.

    Input and output are both contiguous partitions and no speech is dropped:
    only the label of a gap changes. Deciding a gap needs the span after it, so
    a turn's final span is emitted up to `max_pause_ms` later than it arrives.
    """

    def __init__(self, *, max_pause_ms: int = 750) -> None:
        if max_pause_ms <= 0:
            raise ValueError("max_pause_ms must be positive")
        self.max_pause_ms = max_pause_ms
        self.gap: Span | None = None
        self.speakers: tuple[str, ...] = ()
        self.cursor_ms = 0

    def feed(self, span: Span) -> list[Span]:
        if span.start_ms != self.cursor_ms:
            raise ValueError("smoother input must be contiguous and monotonic")
        self.cursor_ms = span.end_ms
        if self.gap is None:
            # Only speech can be interrupted: a gap outside a turn is silence.
            if not span.speakers and self.speakers:
                self.gap = span
                return []
            self.speakers = span.speakers
            return [span]
        if not span.speakers:
            self.gap = replace(self.gap, end_ms=span.end_ms)
            if self.gap.end_ms - self.gap.start_ms < self.max_pause_ms:
                return []
            ready, self.gap, self.speakers = [self.gap], None, ()
            return ready
        # The interrupted turn either resumes or ended at this gap; either way
        # the pause belongs to it rather than to a caption of its own.
        ready, self.gap = [replace(self.gap, speakers=self.speakers), span], None
        self.speakers = span.speakers
        return ready

    def flush(self) -> list[Span]:
        ready = [replace(self.gap, speakers=self.speakers)] if self.gap is not None else []
        self.gap, self.speakers, self.cursor_ms = None, (), 0
        return ready
