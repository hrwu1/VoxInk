"""Suppress text that chunk overlap causes a provider to transcribe twice.

Chunkers deliberately retain a short audio overlap between adjacent chunks so
words are not clipped at a boundary.  The provider therefore transcribes the
overlapping audio once per chunk, and the repeated words must be removed before
a segment is finalized into Markdown.

Deduplication is keyed by session and stream: microphone and system audio are
independent timelines and must never suppress each other, and every client
hardcodes the same stream ids, so two concurrent sessions must not either.
"""

from __future__ import annotations

import unicodedata
from dataclasses import replace

from .models import TranscriptEvent


# Whitespace, punctuation, symbols, and control characters carry no matching
# signal, and the provider may punctuate the same audio differently in two
# chunks, so they are dropped before comparing.
_IGNORED_CATEGORIES = frozenset({"Z", "P", "S", "C"})
# Leading punctuation left behind by a cut is noise; symbols such as "$" are
# part of the text and are kept.
_LEADING_CATEGORIES = frozenset({"Z", "P", "C"})


class OverlapDeduplicator:
    """Remove the already-emitted prefix from each newly finalized segment.

    `max_overlap_chars` bounds how far back a repeat is searched for. It is
    generous because a longer match is a *more* specific one: the false
    positive risk is governed by the minimum length, not the maximum, and the
    window has to cover a whole segment for a fully repeated chunk to collapse.

    The minimum is script-aware. Two Han characters are a word and a reliable
    signal, but two Latin characters are not: ending one segment with "to" and
    starting the next with "To be honest" is ordinary speech, not an overlap.
    Latin matches therefore have to be noticeably longer to be believed.
    """

    def __init__(
        self,
        *,
        max_overlap_chars: int = 240,
        min_match_chars: int = 2,
        min_latin_match_chars: int = 4,
    ) -> None:
        if max_overlap_chars <= 0:
            raise ValueError("max_overlap_chars must be positive")
        if min_match_chars <= 0 or min_latin_match_chars <= 0:
            raise ValueError("match minimums must be positive")
        self.max_overlap_chars = max_overlap_chars
        self.min_match_chars = min_match_chars
        self.min_latin_match_chars = min_latin_match_chars
        self._tails: dict[tuple[str, str], str] = {}

    def deduplicate(self, event: TranscriptEvent) -> TranscriptEvent:
        """Return `event` with any text already emitted for its stream removed.

        Partial events are returned untouched because they are disposable. The
        returned text may be empty when a whole chunk repeated its predecessor;
        callers must skip persisting and publishing such an event.

        Timestamps are left alone. They come from the capture clock, and
        shrinking them to match the removed characters would invent precision
        the provider never reported.
        """
        if not event.is_final or not event.text:
            return event
        text = event.text
        key = (event.session_id, event.stream_id)
        cut = self._overlap_length(self._tails.get(key, ""), text)
        deduplicated = _strip_leading(text[cut:]) if cut else text
        self._remember(key, deduplicated)
        if deduplicated == text:
            return event
        return replace(event, text=deduplicated)

    def reset(self, session_id: str | None = None, stream_id: str | None = None) -> None:
        """Forget one stream's history, one whole session's, or everything.

        History is keyed by session as well as stream because every client
        hardcodes `microphone-1` and `system-1`: without the session, two
        concurrent sessions would suppress each other's text.
        """
        if session_id is None:
            self._tails.clear()
        elif stream_id is None:
            for key in [key for key in self._tails if key[0] == session_id]:
                del self._tails[key]
        else:
            self._tails.pop((session_id, stream_id), None)

    def _remember(self, key: tuple[str, str], text: str) -> None:
        if not text:
            return
        combined = self._tails.get(key, "") + text
        self._tails[key] = combined[-self.max_overlap_chars :]

    def _overlap_length(self, tail: str, text: str) -> int:
        """Return how many leading characters of `text` repeat the end of `tail`."""
        if not tail or not text:
            return 0
        tail_normalized, _ = _normalize(tail[-self.max_overlap_chars :])
        text_normalized, offsets = _normalize(text[: self.max_overlap_chars])
        limit = min(len(tail_normalized), len(text_normalized))
        for length in range(limit, self.min_match_chars - 1, -1):
            matched = text_normalized[:length]
            if tail_normalized[-length:] != matched:
                continue
            if length < self._minimum_for(matched):
                continue
            cut = offsets[length - 1] + 1
            if _splits_a_word(text, cut):
                continue
            return cut
        return 0

    def _minimum_for(self, matched: str) -> int:
        if matched.isascii():
            return max(self.min_match_chars, self.min_latin_match_chars)
        return self.min_match_chars


def _normalize(value: str) -> tuple[str, list[int]]:
    """Casefold and drop separators, keeping each kept character's real index."""
    characters: list[str] = []
    offsets: list[int] = []
    for index, character in enumerate(value):
        if unicodedata.category(character)[0] in _IGNORED_CATEGORIES:
            continue
        folded = character.casefold()
        characters.append(folded)
        # Folding can expand one character into several ("ß" becomes "ss").
        # Mapping every produced character back to the same source index keeps
        # cut positions aligned with the original text.
        offsets.extend([index] * len(folded))
    return "".join(characters), offsets


def _strip_leading(text: str) -> str:
    index = 0
    while index < len(text) and unicodedata.category(text[index])[0] in _LEADING_CATEGORIES:
        index += 1
    return text[index:]


def _splits_a_word(text: str, cut: int) -> bool:
    """Reject a match that would slice through a space-separated word."""
    if cut <= 0 or cut >= len(text):
        return False
    return _is_word_character(text[cut - 1]) and _is_word_character(text[cut])


def _is_word_character(character: str) -> bool:
    return character.isascii() and character.isalnum()
