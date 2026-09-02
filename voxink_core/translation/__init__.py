"""On-device translation of finalized transcript segments."""

from .base import TranslationProvider, other_language, script_language
from .qwen import QwenLocalTranslator

__all__ = ["TranslationProvider", "QwenLocalTranslator", "other_language", "script_language"]
