"""Local, provider-neutral speaker diarization.

Importing this package must stay cheap: it deliberately re-exports only the
contract, never the pyannote adapter, so that a caller wanting a `SpeakerTurn`
does not pay to import Torch. Reach for `voxink_core.diarization.pyannote` when
you actually intend to run a model.
"""

from __future__ import annotations

from .alignment import ReconciliationPolicy, SegmentLabel, reconcile
from .artifacts import (
    MARKDOWN_FILENAME,
    TURNS_FILENAME,
    ArtifactPaths,
    read_document,
    stale_sources,
    write_artifacts,
)
from .base import DiarizationProvider, LocalDiarizationModel, require_local_model
from .transcript import TranscriptBlock, part_digests, read_blocks
from .splitting import Piece, SplitPolicy, split_block
from .models import (
    AlignmentBlock,
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
    "MARKDOWN_FILENAME",
    "TURNS_FILENAME",
    "AlignmentBlock",
    "ArtifactPaths",
    "DiarizationError",
    "DiarizationErrorCode",
    "DiarizationProvider",
    "DiarizationRequest",
    "DiarizationResult",
    "JobState",
    "LocalDiarizationModel",
    "Piece",
    "ReconciliationPolicy",
    "SegmentLabel",
    "SpeakerTurn",
    "SplitPolicy",
    "TranscriptBlock",
    "normalize_turns",
    "part_digests",
    "read_blocks",
    "read_document",
    "reconcile",
    "require_local_model",
    "seconds_to_ms",
    "sort_turns",
    "speaker_label",
    "split_block",
    "stale_sources",
    "write_artifacts",
]
