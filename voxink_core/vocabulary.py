"""User-defined terms that steer recognition and repair finalized text.

A speech model has no way to guess a personal name, a product name, or in-house
jargon it never saw in training.  Two independent levers fix that, and one
plain-text file drives both:

* Biasing terms are handed to the provider as recognition context, which
  conditions decoding before a word is ever chosen.
* Correction rules rewrite text the model still gets wrong, for the terms
  biasing alone cannot reach.

The file format is deliberately editable by hand.  Blank lines and `#` comments
are ignored, a bare line is a biasing term, and `misheard => Correct` is a
correction rule whose replacement is biased too, so one line covers both levers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


RULE_SEPARATOR = "=>"
COMMENT_PREFIX = "#"
# The context is repeated on every chunk of every stream, so an unbounded term
# list would slow all recognition down to pay for words nobody said. This is a
# guard against a runaway file, not a recommended list length.
MAX_CONTEXT_CHARS = 2000


@dataclass(frozen=True, slots=True)
class Vocabulary:
    """Biasing terms and correction rules read from one vocabulary file."""

    terms: tuple[str, ...] = ()
    corrections: tuple[tuple[str, str], ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.terms and not self.corrections

    @property
    def context(self) -> str | None:
        """Return the biasing hint to hand a provider, or None when there is none.

        Terms are joined plainly rather than wrapped in an instruction: the
        value becomes a system message for the model, and an instruction would
        invite it to answer rather than to transcribe.
        """
        if not self.terms:
            return None
        context = ", ".join(self.terms)
        if len(context) <= MAX_CONTEXT_CHARS:
            return context
        # Truncate on a term boundary so the hint never ends mid-word.
        kept: list[str] = []
        length = 0
        for term in self.terms:
            length += len(term) + (2 if kept else 0)
            if length > MAX_CONTEXT_CHARS:
                break
            kept.append(term)
        return ", ".join(kept) if kept else None

    def apply(self, text: str) -> str:
        """Rewrite `text` with every correction rule, in file order."""
        for pattern, replacement in _compiled(self.corrections):
            # A function replacement keeps a rule such as "\1" literal instead
            # of letting re read it as a group reference.
            text = pattern.sub(lambda _, value=replacement: value, text)
        return text

    @classmethod
    def parse(cls, content: str) -> "Vocabulary":
        terms: list[str] = []
        corrections: list[tuple[str, str]] = []
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(COMMENT_PREFIX):
                continue
            if RULE_SEPARATOR in line:
                misheard, _, correct = line.partition(RULE_SEPARATOR)
                misheard, correct = misheard.strip(), correct.strip()
                if not misheard or not correct:
                    # An incomplete rule is a typo in a file the user edits by
                    # hand; skipping it keeps the rest of the file working.
                    continue
                corrections.append((misheard, correct))
                _remember(terms, correct)
            else:
                _remember(terms, line)
        return cls(tuple(terms), tuple(corrections))

    @classmethod
    def load(cls, path: str | Path) -> "Vocabulary":
        """Read a vocabulary file, treating an absent or unreadable one as empty."""
        try:
            content = Path(path).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return EMPTY_VOCABULARY
        return cls.parse(content)


EMPTY_VOCABULARY = Vocabulary()


class VocabularyFile:
    """Serve the current vocabulary, re-reading the file only when it changes.

    Recognition consults this once per chunk, so the stat that detects an edit
    costs nothing beside the inference it precedes, and a user who adds a name
    mid-session sees it take effect without restarting the helper.
    """

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path).expanduser() if path is not None else None
        self._signature: tuple[int, int] | None = None
        self._vocabulary = EMPTY_VOCABULARY
        self._loaded = False

    def current(self) -> Vocabulary:
        if self.path is None:
            return EMPTY_VOCABULARY
        signature = _signature(self.path)
        if self._loaded and signature == self._signature:
            return self._vocabulary
        self._signature = signature
        self._vocabulary = (
            EMPTY_VOCABULARY if signature is None else Vocabulary.load(self.path)
        )
        self._loaded = True
        return self._vocabulary


def _remember(terms: list[str], term: str) -> None:
    if term not in terms:
        terms.append(term)


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        status = path.stat()
    except OSError:
        return None
    return (status.st_mtime_ns, status.st_size)


@lru_cache(maxsize=8)
def _compiled(
    corrections: tuple[tuple[str, str], ...],
) -> tuple[tuple[re.Pattern[str], str], ...]:
    """Compile one file's rules once instead of once per finalized segment."""
    return tuple((_compile(misheard), correct) for misheard, correct in corrections)


def _compile(misheard: str) -> re.Pattern[str]:
    """Match `misheard` case-insensitively, without cutting a word in half.

    `\\b` is only meaningful next to an ASCII word character.  A term that
    begins or ends with Han characters or punctuation gets no boundary on that
    side, because Chinese is written without spaces and demanding one there
    would never match.
    """
    pattern = re.escape(misheard)
    if _is_word_character(misheard[0]):
        pattern = r"\b" + pattern
    if _is_word_character(misheard[-1]):
        pattern = pattern + r"\b"
    return re.compile(pattern, re.IGNORECASE)


def _is_word_character(character: str) -> bool:
    return character.isascii() and (character.isalnum() or character == "_")
