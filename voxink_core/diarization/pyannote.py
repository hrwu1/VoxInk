"""The pyannote Community-1 adapter.

This is the only module in the package allowed to know that pyannote exists.
Everything it learns from the pipeline leaves as plain `(label, start_s, end_s)`
triples, so the rules that decide what a speaker is called and where a turn
begins stay in `models`, where they can be tested without Torch.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Sequence
import wave

from ..models import Source
from .base import LocalDiarizationModel, require_local_model
from .models import (
    DiarizationError,
    DiarizationErrorCode,
    DiarizationRequest,
    DiarizationResult,
    normalize_turns,
    speaker_mapping,
)


#: Set before pyannote is imported, never after: the hub library reads these at
#: import time, and a pipeline that has already decided it may reach the network
#: cannot be talked out of it afterwards. Offline is the product promise, so it
#: is enforced by the process environment rather than by remembering to pass a
#: flag at every call site.
OFFLINE_ENVIRONMENT = {
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "PYANNOTE_METRICS_ENABLED": "0",
}

#: What `AudioRetentionWriter` produces, and therefore the only thing this
#: accepts. Community-1 expects mono 16 kHz; resampling here would hide a
#: capture bug rather than fix one.
EXPECTED_SAMPLE_RATE = 16_000
EXPECTED_CHANNELS = 1
EXPECTED_SAMPLE_WIDTH = 2


def apply_offline_environment(environment: dict[str, str] | None = None) -> None:
    """Force offline hub behaviour. Call before importing pyannote."""
    target = os.environ if environment is None else environment
    target.update(OFFLINE_ENVIRONMENT)


def resolve_device(preference: str = "auto") -> str:
    """Turn a device preference into the device that will actually be used.

    `auto` prefers Apple MPS, which ran roughly nine times faster than CPU and
    produced turn-for-turn identical output on both the single- and
    three-speaker benchmarks. It falls back to CPU wherever MPS is absent, so
    the timeout budget stays sized for CPU: the slow device is still reachable.
    """
    if preference != "auto":
        return preference
    try:
        import torch  # noqa: PLC0415 - deferred: importing Torch costs seconds

        if torch.backends.mps.is_available():
            return "mps"
    except Exception:  # noqa: BLE001 - any probe failure means "use the CPU"
        return "cpu"
    return "cpu"


apply_offline_environment()


class PyannoteCommunityProvider:
    """Local speaker diarization through pyannote Community-1."""

    name = "pyannote-local"

    def __init__(self, *, device: str = "auto") -> None:
        self.device = device
        self.resolved_device: str | None = None
        self._pipeline: Any | None = None
        self._pipeline_path: Path | None = None

    def diarize(self, request: DiarizationRequest) -> DiarizationResult:
        model = require_local_model(request.model_path)
        duration_ms = probe_audio(request.audio_path)
        pipeline = self._load(model)
        try:
            output = pipeline(
                str(request.audio_path),
                **_speaker_bounds(request),
            )
        except Exception as exc:  # noqa: BLE001 - any pipeline failure is one failure
            raise DiarizationError(
                DiarizationErrorCode.WORKER_CRASHED,
                f"{type(exc).__name__}: {exc}",
                audio=request.audio_path.name,
                duration_ms=duration_ms,
            ) from exc
        raw = _triples(getattr(output, "speaker_diarization", None))
        exclusive = _triples(getattr(output, "exclusive_speaker_diarization", None))
        raw_turns, exclusive_turns = normalize_turns(raw, exclusive, source=request.source)
        mapping = speaker_mapping(raw, exclusive, source=request.source)
        embeddings = _embeddings(output, mapping)
        missing = [label for label in mapping if mapping[label] not in embeddings]
        if missing:
            # The pipeline's centroids are NaN whenever it skipped clustering
            # (one speaker, or one cluster), so compute them ourselves from
            # each speaker's own turns with the pipeline's embedding model.
            embeddings.update(
                _fallback_embeddings(pipeline, request.audio_path, exclusive, mapping, missing)
            )
        return DiarizationResult(
            model=model.source_model_id,
            model_revision=model.revision,
            raw_turns=raw_turns,
            exclusive_turns=exclusive_turns,
            embeddings=embeddings,
        )

    def _load(self, model: LocalDiarizationModel) -> Any:
        if self._pipeline is not None and self._pipeline_path == model.path:
            return self._pipeline
        apply_offline_environment()
        from pyannote.audio import Pipeline  # noqa: PLC0415 - deferred: costs seconds

        try:
            pipeline = Pipeline.from_pretrained(str(model.path))
        except Exception as exc:  # noqa: BLE001
            raise DiarizationError(
                DiarizationErrorCode.MODEL_UNAVAILABLE,
                f"pipeline could not be loaded: {type(exc).__name__}: {exc}",
                model_path=model.path.name,
            ) from exc
        if pipeline is None:
            # `from_pretrained` returns None for a config it cannot understand
            # instead of raising, which would otherwise surface as a confusing
            # "NoneType is not callable" at inference time.
            raise DiarizationError(
                DiarizationErrorCode.MODEL_UNAVAILABLE,
                "pipeline configuration was not understood by pyannote",
                model_path=model.path.name,
            )
        device = resolve_device(self.device)
        self.resolved_device = device
        if device != "cpu":
            import torch  # noqa: PLC0415

            try:
                pipeline.to(torch.device(device))
            except Exception:  # noqa: BLE001
                # An accelerator that enumerates but will not accept the model
                # is not a reason to fail the job; CPU always works.
                self.resolved_device = "cpu"
        self._pipeline = pipeline
        self._pipeline_path = model.path
        return pipeline


def probe_audio(audio_path: str | Path) -> int:
    """Validate retained-audio format and return its duration in milliseconds."""
    path = Path(audio_path)
    if not path.is_file():
        raise DiarizationError(
            DiarizationErrorCode.AUDIO_UNAVAILABLE,
            "retained audio is missing",
            audio=path.name,
        )
    try:
        with wave.open(str(path), "rb") as reader:
            rate = reader.getframerate()
            channels = reader.getnchannels()
            width = reader.getsampwidth()
            compression = reader.getcomptype()
            frames = reader.getnframes()
    except (OSError, wave.Error) as exc:
        raise DiarizationError(
            DiarizationErrorCode.INVALID_AUDIO,
            f"audio could not be read: {exc}",
            audio=path.name,
        ) from exc
    if (
        rate != EXPECTED_SAMPLE_RATE
        or channels != EXPECTED_CHANNELS
        or width != EXPECTED_SAMPLE_WIDTH
        or compression != "NONE"
    ):
        raise DiarizationError(
            DiarizationErrorCode.INVALID_AUDIO,
            f"expected {EXPECTED_SAMPLE_RATE} Hz mono 16-bit PCM, "
            f"found {rate} Hz {channels}ch {width * 8}-bit {compression}",
            audio=path.name,
        )
    if frames <= 0:
        raise DiarizationError(
            DiarizationErrorCode.INVALID_AUDIO,
            "audio contains no samples",
            audio=path.name,
        )
    return frames * 1000 // rate


def _speaker_bounds(request: DiarizationRequest) -> dict[str, int]:
    bounds = {}
    if request.min_speakers is not None:
        bounds["min_speakers"] = request.min_speakers
    if request.max_speakers is not None:
        bounds["max_speakers"] = request.max_speakers
    return bounds


def _embeddings(output: Any, mapping: dict[str, str]) -> dict[str, tuple[float, ...]]:
    """One centroid per speaker, re-keyed to the anonymous session ids.

    Community-1 returns `speaker_embeddings[i]` for the i-th label of
    `speaker_diarization.labels()`. A pipeline without embeddings, or one
    whose rows do not line up with its labels, simply yields none: names are
    an extra on top of diarization, never a reason for it to fail.
    """
    vectors = getattr(output, "speaker_embeddings", None)
    annotation = getattr(output, "speaker_diarization", None)
    if vectors is None or annotation is None:
        return {}
    try:
        labels = list(annotation.labels())
        rows = [[float(value) for value in row] for row in vectors]
    except (TypeError, ValueError, AttributeError):
        return {}
    if len(rows) != len(labels):
        return {}
    embeddings = {}
    for label, row in zip(labels, rows):
        speaker_id = mapping.get(str(label))
        if speaker_id is None or not row or any(value != value for value in row):
            continue
        embeddings[speaker_id] = tuple(row)
    return embeddings


#: Audio per speaker fed to the embedding model in the fallback, in seconds.
#: Enough for a stable voice print; bounded so a two-hour monologue does not
#: cost minutes of inference for one vector.
_EMBEDDING_BUDGET_S = 60.0
_EMBEDDING_MIN_TURN_S = 0.5


def _fallback_embeddings(
    pipeline: Any,
    audio_path: Path,
    exclusive: Sequence[tuple[str, float, float]],
    mapping: dict[str, str],
    labels: Sequence[str],
) -> dict[str, tuple[float, ...]]:
    """One duration-weighted mean embedding per speaker from its own turns."""
    embedder = getattr(pipeline, "_embedding", None)
    if embedder is None:
        return {}
    try:
        import numpy as np  # noqa: PLC0415
        import torch  # noqa: PLC0415

        with wave.open(str(audio_path), "rb") as reader:
            rate = reader.getframerate()
            frames = reader.readframes(reader.getnframes())
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    except Exception:  # noqa: BLE001 - embeddings are an extra, never a failure
        return {}
    result: dict[str, tuple[float, ...]] = {}
    for label in labels:
        turns = sorted(
            ((end - start, start, end) for item, start, end in exclusive if item == label),
            reverse=True,
        )
        vectors = []
        weights = []
        budget = _EMBEDDING_BUDGET_S
        for duration, start, end in turns:
            if duration < _EMBEDDING_MIN_TURN_S or budget <= 0.0:
                continue
            clip = samples[int(start * rate) : int(end * rate)]
            if len(clip) < int(_EMBEDDING_MIN_TURN_S * rate):
                continue
            try:
                waveform = torch.from_numpy(clip.copy())[None, None, :]
                vector = np.asarray(embedder(waveform))[0]
            except Exception:  # noqa: BLE001
                continue
            if not np.all(np.isfinite(vector)):
                continue
            vectors.append(vector)
            weights.append(duration)
            budget -= duration
        if not vectors:
            continue
        mean = np.average(np.stack(vectors), axis=0, weights=np.asarray(weights))
        speaker_id = mapping.get(str(label))
        if speaker_id is not None and np.all(np.isfinite(mean)):
            result[speaker_id] = tuple(float(value) for value in mean)
    return result


def _triples(annotation: Any) -> Sequence[tuple[str, float, float]]:
    """Flatten a pyannote `Annotation` into plain triples.

    The track identifier is dropped on purpose: it distinguishes concurrent
    turns *within* one speaker, which carries no meaning downstream and would
    only invite a caller to depend on a pipeline-local detail.
    """
    if annotation is None:
        return ()
    return tuple(
        (str(label), float(segment.start), float(segment.end))
        for segment, _, label in annotation.itertracks(yield_label=True)
    )
