"""Provider-neutral translation contract.

Translation is a second, optional pass over finalized text. It never touches
the canonical transcript block; the service appends what comes back as a
quoted line and shows it under the caption. A provider that cannot translate
a pair raises; the service treats that as "no translation", never as a failed
transcript.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


LANGUAGE_NAMES = {"en": "English", "zh": "Chinese"}


@runtime_checkable
class TranslationProvider(Protocol):
    name: str

    @property
    def is_loaded(self) -> bool: ...

    def load(self) -> None: ...

    def translate(self, text: str, source_language: str, target_language: str) -> str: ...

    def unload(self) -> None: ...


def script_language(text: str) -> str:
    """Guess the language of a segment from its script when the model did not say.

    Any CJK ideograph makes it Chinese: a mixed sentence is Chinese with English
    words in it far more often than the reverse, and either way the other
    language is the one worth translating into.
    """
    for char in text:
        code = ord(char)
        if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF or 0x3000 <= code <= 0x303F:
            return "zh"
    return "en"


def other_language(source_language: str | None, text: str) -> tuple[str, str]:
    """The `(source, target)` pair for a segment, translating into the other one."""
    source = (source_language or "").lower()[:2] or script_language(text)
    if source not in LANGUAGE_NAMES:
        source = script_language(text)
    return source, ("en" if source == "zh" else "zh")
