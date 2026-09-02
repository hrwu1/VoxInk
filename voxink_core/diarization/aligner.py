"""The Qwen3 forced-aligner adapter.

This is the only module in the package allowed to know that `qwen_asr` exists.
It turns one transcript block plus the audio under it into `WordTimestamp`
triples on the session clock, and everything about which words belong to
which speaker stays in `splitting`, where it runs without Torch.

The aligner is a 0.6B model that answers "when in this audio is each of
these words said", given the text. It is not a transcriber: hand it text the
audio does not contain and it will stack those words on one instant, which
is exactly what `splitting` watches for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence
import wave

from ..models import WordTimestamp
from .base import LocalDiarizationModel, require_local_model
from .models import AlignmentBlock, DiarizationError, DiarizationErrorCode
from .pyannote import EXPECTED_SAMPLE_RATE, resolve_device


#: The aligner's own input ceiling. A chunker window is fifteen seconds, so
#: this is never reached by a real block; it exists so a malformed block
#: cannot make the model allocate without bound.
MAX_BLOCK_SECONDS = 180

_SAMPLE_WIDTH = 2
_CJK_RANGES = (
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0x20000, 0x2A6DF),
    (0x2A700, 0x2B73F),
    (0x2B740, 0x2B81F),
    (0x2B820, 0x2CEAF),
    (0xF900, 0xFAFF),
)


class QwenForcedAligner:
    """Word timings for transcript blocks through Qwen3-ForcedAligner-0.6B."""

    name = "qwen-aligner-local"

    def __init__(self, *, device: str = "auto") -> None:
        self.device = device
        self.resolved_device: str | None = None
        self._aligner: Any | None = None
        self._model: LocalDiarizationModel | None = None

    def load(self, model_path: str | Path) -> LocalDiarizationModel:
        """Validate the package and load the model, once."""
        model = require_local_model(model_path)
        if self._aligner is not None and self._model is not None and self._model.path == model.path:
            return self._model
        import torch  # noqa: PLC0415 - deferred: costs seconds
        from qwen_asr.inference.qwen3_forced_aligner import (  # noqa: PLC0415
            Qwen3ForcedAligner,
        )

        device = resolve_device(self.device)
        try:
            aligner = Qwen3ForcedAligner.from_pretrained(
                str(model.path),
                dtype=torch.float16 if device == "mps" else torch.float32,
                device_map=device,
                local_files_only=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise DiarizationError(
                DiarizationErrorCode.MODEL_UNAVAILABLE,
                f"aligner could not be loaded: {type(exc).__name__}: {exc}",
                model_path=model.path.name,
            ) from exc
        self.resolved_device = device
        self._aligner = aligner
        self._model = model
        return model

    def align(
        self, audio_path: str | Path, blocks: Sequence[AlignmentBlock]
    ) -> dict[str, tuple[WordTimestamp, ...]]:
        """Time the words of each block, keyed by block id.

        A block that fails to align is left out rather than failing the rest:
        every block is independent, and the caller labels an absent block the
        way it would without an aligner at all.
        """
        if self._aligner is None:
            raise RuntimeError("load() must succeed before align()")
        timings: dict[str, tuple[WordTimestamp, ...]] = {}
        with wave.open(str(audio_path), "rb") as reader:
            if reader.getframerate() != EXPECTED_SAMPLE_RATE or reader.getnchannels() != 1:
                raise DiarizationError(
                    DiarizationErrorCode.INVALID_AUDIO,
                    "aligner expects the joined 16 kHz mono session audio",
                )
            total_frames = reader.getnframes()
            for block in blocks:
                if block.duration_ms > MAX_BLOCK_SECONDS * 1000:
                    continue
                samples = _window(reader, total_frames, block.start_ms, block.end_ms)
                if samples is None:
                    continue
                try:
                    result = self._aligner.align(
                        audio=(samples, EXPECTED_SAMPLE_RATE),
                        text=block.text,
                        language=_language_for(block.text),
                    )[0]
                except Exception:  # noqa: BLE001 - one block, not the job
                    continue
                words = _words(result, block.start_ms)
                if words:
                    timings[block.id] = words
        return timings


def _window(reader: wave.Wave_read, total_frames: int, start_ms: int, end_ms: int) -> Any:
    """Cut one block's audio as float32 in [-1, 1], or None if it is off the end."""
    import numpy as np  # noqa: PLC0415 - deferred with the model it feeds

    frames_per_ms = EXPECTED_SAMPLE_RATE // 1000
    start = min(start_ms * frames_per_ms, total_frames)
    end = min(end_ms * frames_per_ms, total_frames)
    if end <= start:
        return None
    reader.setpos(start)
    raw = reader.readframes(end - start)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _words(result: Any, offset_ms: int) -> tuple[WordTimestamp, ...]:
    words: list[WordTimestamp] = []
    for item in result:
        text = str(getattr(item, "text", "") or "")
        if not text:
            continue
        start_ms = offset_ms + _to_ms(getattr(item, "start_time", 0.0))
        end_ms = offset_ms + _to_ms(getattr(item, "end_time", 0.0))
        # `WordTimestamp` refuses end < start; the aligner's repair pass makes
        # the sequence monotonic but a single item can still come back
        # inverted, and that item is an instant rather than an error.
        words.append(WordTimestamp(text=text, start_ms=start_ms, end_ms=max(start_ms, end_ms)))
    return tuple(words)


def _to_ms(seconds: Any) -> int:
    return int(round(float(seconds) * 1000.0))


def _language_for(text: str) -> str:
    """The aligner tokenizes CJK per character and everything else per word.

    The language name only changes tokenization for Japanese and Korean, so
    Mandarin, English, and a mix of the two all take the same path here.
    """
    for character in text:
        code = ord(character)
        if any(low <= code <= high for low, high in _CJK_RANGES):
            return "Chinese"
    return "English"
