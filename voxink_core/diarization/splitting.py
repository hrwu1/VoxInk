"""Split one transcript block into same-speaker pieces using word timings.

A transcript block is a chunker window, up to fifteen seconds long; a turn in
a conversation lasts a second or two. Labeling the block as a whole therefore
cannot work on real dialogue, and this module is what makes splitting safe:
with forced-alignment word timings, every word can be placed against the
exclusive turns and consecutive same-speaker words become one piece.

Nothing here imports a model. The rules that matter -- which words count as
placed, what a run too short to trust is, when a block is too broken to split
-- are all here, where they run in microseconds and can be tested exhaustively.

Text is never rewritten. A piece is a character range of the canonical block,
and the ranges of a block's pieces tile it exactly, so concatenating them
reproduces the block byte for byte.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence
import unicodedata

from ..models import WordTimestamp
from .models import SpeakerTurn
from .transcript import TranscriptBlock


#: A piece whose words the aligner could not place in the block's audio.
UNALIGNED = "unaligned"


@dataclass(frozen=True, slots=True)
class SplitPolicy:
    """Every number here is policy, not physics."""

    #: A timed word overlapping no exclusive turn adopts the nearest one within
    #: this gap. Diarization and alignment boundaries disagree by tens of
    #: milliseconds routinely; a word further from every turn than this is
    #: not evidence of anyone.
    max_gap_ms: int = 750
    #: Consecutive words the aligner stacked on one identical instant, at or
    #: past this count, are text it could not find in the audio -- typically a
    #: hallucinated or repeated phrase. They stay unlabeled rather than
    #: inheriting a neighbour's speaker.
    collapse_run_words: int = 6
    #: A run shorter than this, between two runs that agree with each other,
    #: is alignment jitter and joins them. A genuine one-word interjection is
    #: normally longer.
    min_island_ms: int = 300
    #: A word timed at or beyond the block's end plus this tolerance was not
    #: found in the block's audio.
    end_tolerance_ms: int = 250
    #: A cut that falls mid-clause moves to the nearest punctuation boundary
    #: within this many words. Alignment jitter is a character or two, and a
    #: speaker change almost always coincides with a clause boundary the
    #: transcriber already punctuated.
    snap_words: int = 2

    def __post_init__(self) -> None:
        if self.collapse_run_words < 2:
            raise ValueError("collapse_run_words must be at least 2")
        for name in ("max_gap_ms", "min_island_ms", "end_tolerance_ms", "snap_words"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")


@dataclass(frozen=True, slots=True)
class WordSpan:
    """One aligned word: a character range of the block and its session time."""

    text_start: int
    text_end: int
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True, slots=True)
class Piece:
    """A same-speaker run of a block, addressed as a character range of it."""

    text_start: int
    text_end: int
    start_ms: int
    end_ms: int
    speaker_id: str | None
    #: Share of the piece's directly attributed speech held by its speaker.
    speaker_coverage: float
    attributed_ms: int
    reason: str | None

    @property
    def labeled(self) -> bool:
        return self.speaker_id is not None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "text_start": self.text_start,
            "text_end": self.text_end,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "speaker_id": self.speaker_id,
            "speaker_coverage": round(self.speaker_coverage, 4),
            "attributed_ms": self.attributed_ms,
        }
        if self.reason is not None:
            payload["unlabeled_reason"] = self.reason
        return payload


def map_words(text: str, words: Sequence[WordTimestamp]) -> tuple[WordSpan, ...] | None:
    """Locate each aligned word in the block text, or None if they do not fit.

    The aligner tokenizes the text itself -- one token per CJK character, one
    per whitespace-delimited word otherwise -- and strips everything that is
    not a letter, digit, or apostrophe. So a token is a subsequence of the
    original text in order, and can be found by walking forward character by
    character. Any letter or digit skipped on the way is proof the words came
    from different text, and then the timings cannot be trusted to line up
    with this block at all.
    """
    spans: list[WordSpan] = []
    cursor = 0
    for word in words:
        if word.end_ms < word.start_ms:
            return None
        first: int | None = None
        position = cursor
        for character in word.text:
            found = text.find(character, position)
            if found < 0 or any(_is_kept(skipped) for skipped in text[position:found]):
                return None
            if first is None:
                first = found
            position = found + 1
        if first is None:
            return None
        spans.append(WordSpan(first, position, word.start_ms, word.end_ms))
        cursor = position
    return tuple(spans)


def split_block(
    block: TranscriptBlock,
    words: Sequence[WordTimestamp],
    exclusive_turns: Sequence[SpeakerTurn],
    policy: SplitPolicy | None = None,
) -> tuple[Piece, ...]:
    """Cut a block into same-speaker pieces, or return nothing if it cannot be.

    An empty result means "label the block as a whole, the old way": the
    words did not map onto the text, or not one of them could be placed
    against a speaker.
    """
    policy = policy or SplitPolicy()
    spans = map_words(block.text, words)
    if not spans:
        return ()
    turns = [turn for turn in exclusive_turns if turn.exclusive]
    direct = _attribute(spans, turns, block, policy)
    resolved = _inherit(direct)
    if all(state is None or state == UNALIGNED for state in resolved):
        return ()
    runs = _smooth(_runs(resolved), spans, policy)
    runs = _snap(runs, block.text, spans, policy)
    return tuple(_pieces(block.text, spans, direct, runs))


# -- attribution ---------------------------------------------------------------


def _attribute(
    spans: Sequence[WordSpan],
    turns: Sequence[SpeakerTurn],
    block: TranscriptBlock,
    policy: SplitPolicy,
) -> list[str | None]:
    """What each word says on its own: a speaker, UNALIGNED, or None (no view)."""
    states: list[str | None] = [None] * len(spans)
    limit = block.end_ms + policy.end_tolerance_ms
    for index, span in enumerate(spans):
        if span.start_ms >= limit:
            states[index] = UNALIGNED
            continue
        if span.duration_ms <= 0:
            # An instant, not an interval: the aligner quantizes to tens of
            # milliseconds and neighbouring characters often share one. It is
            # evidence only if a turn plainly contains it.
            containing = [turn for turn in turns if turn.start_ms <= span.start_ms < turn.end_ms]
            if len({turn.speaker_id for turn in containing}) == 1:
                states[index] = containing[0].speaker_id
            continue
        states[index] = _speaker_for(span, turns, policy)
    _mark_collapsed(spans, states, policy)
    return states


def _speaker_for(
    span: WordSpan, turns: Sequence[SpeakerTurn], policy: SplitPolicy
) -> str | None:
    covered: dict[str, int] = {}
    for turn in turns:
        overlap = turn.overlap_ms(span.start_ms, span.end_ms)
        if overlap:
            covered[turn.speaker_id] = covered.get(turn.speaker_id, 0) + overlap
    if covered:
        ranked = sorted(covered.items(), key=lambda item: (-item[1], item[0]))
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            return None
        return ranked[0][0]
    nearest: tuple[int, str] | None = None
    for turn in turns:
        gap = max(turn.start_ms - span.end_ms, span.start_ms - turn.end_ms)
        if gap <= policy.max_gap_ms and (nearest is None or gap < nearest[0]):
            nearest = (gap, turn.speaker_id)
    return nearest[1] if nearest else None


def _mark_collapsed(
    spans: Sequence[WordSpan], states: list[str | None], policy: SplitPolicy
) -> None:
    """Runs of words stacked on one instant are text the audio does not hold."""
    start = 0
    while start < len(spans):
        end = start + 1
        anchor = spans[start]
        while (
            end < len(spans)
            and spans[end].start_ms == anchor.start_ms
            and spans[end].end_ms == anchor.end_ms
        ):
            end += 1
        if anchor.duration_ms <= 0 and end - start >= policy.collapse_run_words:
            for index in range(start, end):
                states[index] = UNALIGNED
        start = end


def _inherit(direct: Sequence[str | None]) -> list[str | None]:
    """Words with no view of their own follow the word before them.

    Nearly all of these are the quantized instants between two placed words,
    so they belong with what precedes them in reading order. Leading words
    with nothing before them take the first resolved word after them.
    """
    resolved = list(direct)
    previous: str | None = None
    for index, state in enumerate(resolved):
        if state is None:
            resolved[index] = previous
        else:
            previous = state
    following: str | None = None
    for index in range(len(resolved) - 1, -1, -1):
        if resolved[index] is None:
            resolved[index] = following
        else:
            following = resolved[index]
    return resolved


# -- runs and pieces -----------------------------------------------------------


def _runs(states: Sequence[str | None]) -> list[tuple[str | None, list[int]]]:
    runs: list[tuple[str | None, list[int]]] = []
    for index, state in enumerate(states):
        if runs and runs[-1][0] == state:
            runs[-1][1].append(index)
        else:
            runs.append((state, [index]))
    return runs


def _smooth(
    runs: list[tuple[str | None, list[int]]],
    spans: Sequence[WordSpan],
    policy: SplitPolicy,
) -> list[tuple[str | None, list[int]]]:
    """Absorb a short run whose neighbours agree with each other.

    Merging can create a new short island, so this repeats until nothing
    changes.
    """
    runs = list(runs)
    changed = True
    while changed and len(runs) >= 3:
        changed = False
        for index in range(1, len(runs) - 1):
            state, run = runs[index]
            left_state, left = runs[index - 1]
            right_state, right = runs[index + 1]
            if left_state != right_state:
                continue
            duration = spans[run[-1]].end_ms - spans[run[0]].start_ms
            if duration < policy.min_island_ms:
                runs[index - 1 : index + 2] = [(left_state, left + run + right)]
                changed = True
                break
    return runs


def _snap(
    runs: list[tuple[str | None, list[int]]],
    text: str,
    spans: Sequence[WordSpan],
    policy: SplitPolicy,
) -> list[tuple[str | None, list[int]]]:
    """Move each cut to the nearest clause boundary within `snap_words`.

    A cut already at punctuation stays. Otherwise the words between the old
    and new cut change sides, which overrides the aligner by at most a couple
    of characters -- its own jitter -- in exchange for pieces that start
    where a reader expects a sentence to.
    """
    if policy.snap_words <= 0:
        return runs
    runs = [(state, list(indices)) for state, indices in runs]
    for position in range(len(runs) - 1):
        left_state, left = runs[position]
        right_state, right = runs[position + 1]
        cut = right[0]
        if _at_clause_boundary(text, spans, cut):
            continue
        candidates = [
            index
            for index in range(max(left[0] + 1, cut - policy.snap_words), min(right[-1], cut + policy.snap_words) + 1)
            if index != cut and _at_clause_boundary(text, spans, index)
        ]
        if not candidates:
            continue
        target = min(candidates, key=lambda index: (abs(index - cut), index))
        if target < cut:
            moved = [index for index in left if index >= target]
            runs[position] = (left_state, [index for index in left if index < target])
            runs[position + 1] = (right_state, moved + right)
        else:
            moved = [index for index in right if index < target]
            runs[position] = (left_state, left + moved)
            runs[position + 1] = (right_state, [index for index in right if index >= target])
    return runs


def _at_clause_boundary(text: str, spans: Sequence[WordSpan], index: int) -> bool:
    """Whether the text between the previous word and this one has punctuation."""
    if index <= 0 or index >= len(spans):
        return False
    between = text[spans[index - 1].text_end : spans[index].text_start]
    return any(not _is_kept(c) and not c.isspace() for c in between)


def _pieces(
    text: str,
    spans: Sequence[WordSpan],
    direct: Sequence[str | None],
    runs: list[tuple[str | None, list[int]]],
) -> list[Piece]:
    pieces: list[Piece] = []
    for position, (state, run) in enumerate(runs):
        text_start = 0 if position == 0 else spans[run[0]].text_start
        text_end = len(text) if position == len(runs) - 1 else spans[runs[position + 1][1][0]].text_start
        start_ms = min(spans[index].start_ms for index in run)
        end_ms = max(max(spans[index].end_ms for index in run), start_ms)
        attributed = sum(
            spans[index].duration_ms
            for index in run
            if direct[index] not in (None, UNALIGNED)
        )
        winner_ms = sum(
            spans[index].duration_ms for index in run if direct[index] == state
        )
        speaker = None if state in (None, UNALIGNED) else state
        pieces.append(
            Piece(
                text_start=text_start,
                text_end=text_end,
                start_ms=start_ms,
                end_ms=end_ms,
                speaker_id=speaker,
                speaker_coverage=(winner_ms / attributed) if speaker and attributed else 0.0,
                attributed_ms=attributed,
                reason=None if speaker else UNALIGNED,
            )
        )
    return pieces


def _is_kept(character: str) -> bool:
    """Whether the aligner's tokenizer would have kept this character."""
    if character == "'":
        return True
    category = unicodedata.category(character)
    return category.startswith("L") or category.startswith("N")
