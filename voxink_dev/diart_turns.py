"""Compatibility imports for the core-owned, model-independent Diart turns."""

from voxink_core.diarization.diart_turns import (
    Span,
    SpanSmoother,
    TurnBuffer,
    context_window,
    partition,
)

__all__ = ["Span", "SpanSmoother", "TurnBuffer", "context_window", "partition"]
