"""Decide which transcript blocks may carry a speaker label.

The rule is deliberately reluctant. A wrong speaker label is worse than no
label: it misattributes words to a person, and the reader has no way to tell a
confident label from a guess. So anything ambiguous stays blank and the raw
turns in `speaker-turns.json` remain available for a human to inspect.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..models import Source
from .models import SpeakerTurn
from .transcript import TranscriptBlock


@dataclass(frozen=True, slots=True)
class ReconciliationPolicy:
    """Version-one labelling policy. Every number here is policy, not physics."""

    #: Share of *attributed speech* the winning speaker must hold.
    min_coverage: float = 0.70
    #: Absolute floor of attributed speech in a segment.
    min_attributed_ms: int = 200
    #: Attributed speech as a share of the segment's wall-clock duration.
    min_attributed_ratio: float = 0.20
    #: How much of a segment a raw speaker must occupy to count as present.
    overlap_min_ms: int = 100

    def __post_init__(self) -> None:
        if not 0.0 < self.min_coverage <= 1.0:
            raise ValueError("min_coverage must be within (0, 1]")
        if not 0.0 <= self.min_attributed_ratio <= 1.0:
            raise ValueError("min_attributed_ratio must be within [0, 1]")
        if self.min_attributed_ms < 0 or self.overlap_min_ms < 0:
            raise ValueError("duration thresholds must not be negative")


@dataclass(frozen=True, slots=True)
class SegmentLabel:
    """One block's speaker decision, with the evidence behind it."""

    block: TranscriptBlock
    speaker_id: str | None
    #: The gating ratio: winner's time over all attributed speech in the block.
    speaker_coverage: float
    #: The same win measured against wall-clock duration. Recorded but not
    #: gated on, so evaluation can retune the policy against real captures
    #: without re-running any models.
    segment_coverage: float
    attributed_ms: int
    overlap_detected: bool
    reason: str | None

    @property
    def labeled(self) -> bool:
        return self.speaker_id is not None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "canonical_ref": self.block.canonical_ref(),
            "speaker_id": self.speaker_id,
            "speaker_coverage": round(self.speaker_coverage, 4),
            "segment_coverage": round(self.segment_coverage, 4),
            "attributed_ms": self.attributed_ms,
            "overlap_detected": self.overlap_detected,
        }
        if self.reason is not None:
            payload["unlabeled_reason"] = self.reason
        return payload


def reconcile(
    blocks: Iterable[TranscriptBlock],
    *,
    exclusive_turns: Sequence[SpeakerTurn],
    raw_turns: Sequence[SpeakerTurn],
    source: Source,
    policy: ReconciliationPolicy | None = None,
) -> tuple[SegmentLabel, ...]:
    """Attach a speaker to each block that clearly has exactly one."""
    policy = policy or ReconciliationPolicy()
    source = Source(source)
    exclusive = [turn for turn in exclusive_turns if turn.source is source]
    raw = [turn for turn in raw_turns if turn.source is source]
    # Every block is returned, including blocks from another source: the
    # derived Markdown has to reproduce the transcript in full, so a block this
    # job cannot speak for is carried through unlabeled rather than dropped.
    return tuple(_label(block, exclusive, raw, source, policy) for block in blocks)


def _label(
    block: TranscriptBlock,
    exclusive: Sequence[SpeakerTurn],
    raw: Sequence[SpeakerTurn],
    source: Source,
    policy: ReconciliationPolicy,
) -> SegmentLabel:
    if block.source is not source:
        return _unlabeled(block, reason="other_source")
    if not block.has_valid_range:
        return _unlabeled(block, reason="invalid_range")

    covered: dict[str, int] = defaultdict(int)
    for turn in exclusive:
        overlap = turn.overlap_ms(block.start_ms, block.end_ms)
        if overlap:
            covered[turn.speaker_id] += overlap

    overlap_detected = (
        len(
            {
                turn.speaker_id
                for turn in raw
                if turn.overlap_ms(block.start_ms, block.end_ms) >= policy.overlap_min_ms
            }
        )
        >= 2
    )

    attributed = sum(covered.values())
    duration = block.duration_ms
    if attributed <= 0:
        return _unlabeled(block, reason="no_speech", overlap_detected=overlap_detected)

    ranked = sorted(covered.items(), key=lambda item: (-item[1], item[0]))
    winner, winner_ms = ranked[0]
    coverage = winner_ms / attributed
    segment_coverage = winner_ms / duration if duration else 0.0

    required = max(policy.min_attributed_ms, policy.min_attributed_ratio * duration)
    if attributed < required:
        # Mostly silence. The speaker may well be right, but a sliver of audio
        # is not evidence, and this is the case the wall-clock rule conflated
        # with genuine ambiguity.
        return _unlabeled(
            block,
            reason="insufficient_speech",
            coverage=coverage,
            segment_coverage=segment_coverage,
            attributed_ms=attributed,
            overlap_detected=overlap_detected,
        )
    if len(ranked) > 1 and ranked[1][1] == winner_ms:
        return _unlabeled(
            block,
            reason="tie",
            coverage=coverage,
            segment_coverage=segment_coverage,
            attributed_ms=attributed,
            overlap_detected=overlap_detected,
        )
    if coverage < policy.min_coverage:
        return _unlabeled(
            block,
            reason="low_coverage",
            coverage=coverage,
            segment_coverage=segment_coverage,
            attributed_ms=attributed,
            overlap_detected=overlap_detected,
        )
    return SegmentLabel(
        block=block,
        speaker_id=winner,
        speaker_coverage=coverage,
        segment_coverage=segment_coverage,
        attributed_ms=attributed,
        overlap_detected=overlap_detected,
        reason=None,
    )


def _unlabeled(
    block: TranscriptBlock,
    *,
    reason: str,
    coverage: float = 0.0,
    segment_coverage: float = 0.0,
    attributed_ms: int = 0,
    overlap_detected: bool = False,
) -> SegmentLabel:
    return SegmentLabel(
        block=block,
        speaker_id=None,
        speaker_coverage=coverage,
        segment_coverage=segment_coverage,
        attributed_ms=attributed_ms,
        overlap_detected=overlap_detected,
        reason=reason,
    )
