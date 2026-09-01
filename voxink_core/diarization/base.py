"""The provider contract, and the offline model check every provider shares.

Like `models`, this module must not import pyannote or Torch: a caller has to
be able to ask "are the weights actually here?" and get a typed answer without
paying to load a deep-learning stack first.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..local_models import MODEL_MANIFEST, inspect_model_package
from .models import DiarizationError, DiarizationErrorCode, DiarizationRequest, DiarizationResult


@dataclass(frozen=True, slots=True)
class LocalDiarizationModel:
    """A validated local model package, with the provenance artifacts record."""

    path: Path
    model_id: str
    source_model_id: str
    revision: str
    license: str


@runtime_checkable
class DiarizationProvider(Protocol):
    """Anything that can turn retained audio into speaker turns."""

    name: str

    def diarize(self, request: DiarizationRequest) -> DiarizationResult: ...


def require_local_model(model_path: str | Path) -> LocalDiarizationModel:
    """Validate a local model package, or raise a typed `model_unavailable`.

    This is the guard that keeps the offline promise honest. Loading a pipeline
    from an incomplete directory would send the hub library looking for the
    missing pieces, and the failure would surface much later as a network error
    rather than as "your model is not installed".

    Checksums are deliberately not verified here. This runs before every job,
    the weights are tens of megabytes, and the manifest's per-file size check
    already catches a truncated download; a full verification belongs in
    preflight, where the user is waiting for exactly that answer.
    """
    path = Path(model_path).expanduser()
    inspection = inspect_model_package(path)
    if inspection.package is None or inspection.failures:
        raise DiarizationError(
            DiarizationErrorCode.MODEL_UNAVAILABLE,
            "; ".join(inspection.failures)
            or f"no diarization model package at {path.name}",
            model_path=path.name,
        )
    try:
        manifest = json.loads((path / MODEL_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiarizationError(
            DiarizationErrorCode.MODEL_UNAVAILABLE,
            f"model manifest could not be read: {exc}",
            model_path=path.name,
        ) from exc
    revision = str(manifest.get("revision") or "")
    source_model_id = str(manifest.get("source_model_id") or "")
    if not revision or not source_model_id:
        # Without these, a derived artifact cannot say which weights produced
        # it, and the whole point of recording a producer is reproducibility.
        raise DiarizationError(
            DiarizationErrorCode.MODEL_UNAVAILABLE,
            "model manifest is missing source_model_id or revision",
            model_path=path.name,
        )
    return LocalDiarizationModel(
        path=inspection.package.path,
        model_id=inspection.package.model_id,
        source_model_id=source_model_id,
        revision=revision,
        license=str(manifest.get("license") or "unknown"),
    )
