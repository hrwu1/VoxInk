"""Transcription provider implementations."""

from .base import TranscriptionProvider
from .qwen import QwenLocalProvider

__all__ = ["QwenLocalProvider", "TranscriptionProvider"]
