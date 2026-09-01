"""Local, provider-neutral speaker diarization.

Importing this package must stay cheap: it deliberately re-exports only the
contract, never the pyannote adapter, so that a caller wanting a `SpeakerTurn`
does not pay to import Torch. Reach for `voxink_core.diarization.pyannote` when
you actually intend to run a model.
"""

from __future__ import annotations

from .base import DiarizationProvider, LocalDiarizationModel, require_local_model
from .models import (
    DiarizationError,
    DiarizationErrorCode,
    DiarizationRequest,
    DiarizationResult,
    JobState,
    SpeakerTurn,
    normalize_turns,
    seconds_to_ms,
    sort_turns,
    speaker_label,
)

__all__ = [
    "DiarizationError",
    "DiarizationErrorCode",
    "DiarizationProvider",
    "DiarizationRequest",
    "DiarizationResult",
    "JobState",
    "LocalDiarizationModel",
    "SpeakerTurn",
    "normalize_turns",
    "require_local_model",
    "seconds_to_ms",
    "sort_turns",
    "speaker_label",
]
