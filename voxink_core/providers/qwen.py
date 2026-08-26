"""Lazy, offline-only adapter for the workspace's local Qwen3-ASR model."""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import AsyncIterable, Callable
from pathlib import Path
from typing import Any

from ..models import TranscriptEvent, TranscriptionOptions
from .base import AudioInput

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


ModelLoader = Callable[[Path, str, int], Any]


class QwenLocalProvider:
    """Load Qwen only on first use and never resolve a model over the network."""

    name = "qwen-local"

    def __init__(
        self,
        model_path: str | Path,
        *,
        device: str = "auto",
        max_new_tokens: int = 512,
        model_loader: ModelLoader | None = None,
    ) -> None:
        if device not in {"auto", "mps", "cpu"}:
            raise ValueError("device must be one of: auto, mps, cpu")
        if max_new_tokens <= 0:
            raise ValueError("max_new_tokens must be positive")
        self.model_path = Path(model_path).expanduser().resolve()
        self.device = device
        self.max_new_tokens = max_new_tokens
        self._model_loader = model_loader
        self._model: Any = None
        self._load_lock = asyncio.Lock()
        # One model is shared by every source stream, so inference must be
        # serialized: the model is built with max_inference_batch_size=1 and
        # concurrent generate calls would corrupt its state.
        self._inference_lock = asyncio.Lock()

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    async def load(self) -> None:
        if self.is_loaded:
            return
        async with self._load_lock:
            if self.is_loaded:
                return
            self._validate_local_model()
            loader = self._model_loader or _load_qwen_model
            self._model = await asyncio.to_thread(
                loader, self.model_path, self.device, self.max_new_tokens
            )

    async def transcribe_file(
        self, audio: AudioInput, options: TranscriptionOptions
    ) -> TranscriptEvent:
        await self.load()
        audio_value: str | bytes
        if isinstance(audio, (str, Path)):
            audio_path = Path(audio).expanduser().resolve()
            if not audio_path.is_file():
                raise FileNotFoundError(f"audio file not found: {audio_path}")
            audio_value = str(audio_path)
        elif isinstance(audio, bytes):
            audio_value = audio
        else:
            raise TypeError("audio must be a path or bytes")

        # The load lock is released by now, so waiting here cannot deadlock it.
        async with self._inference_lock:
            model = self._model
            if model is None:
                raise RuntimeError("the model was unloaded before transcription began")
            result = await asyncio.to_thread(
                model.transcribe,
                audio=audio_value,
                language=_qwen_language(options.language),
            )
            if inspect.isawaitable(result):
                result = await result
        if not result:
            raise RuntimeError("Qwen returned no transcription result")
        first = result[0]
        text = str(_result_value(first, "text", "")).strip()
        language = _normalize_language(_result_value(first, "language", None))
        return TranscriptEvent(
            session_id=options.session_id,
            segment_id=options.segment_id,
            stream_id=options.stream_id,
            source=options.source,
            start_ms=options.start_ms,
            end_ms=options.end_ms,
            language=language or options.language,
            text=text,
            is_final=True,
        )

    async def transcribe_stream(
        self,
        frames: AsyncIterable[bytes],
        options: TranscriptionOptions,
    ) -> AsyncIterable[TranscriptEvent]:
        del frames, options
        raise NotImplementedError("Qwen streaming is implemented by the chunking pipeline")
        yield  # pragma: no cover - keeps this an async generator

    async def unload(self) -> None:
        self._model = None

    def _validate_local_model(self) -> None:
        if not self.model_path.is_dir():
            raise FileNotFoundError(f"local model directory not found: {self.model_path}")
        if not (self.model_path / "config.json").is_file():
            raise FileNotFoundError(f"model config not found: {self.model_path / 'config.json'}")


def _load_qwen_model(model_path: Path, requested_device: str, max_new_tokens: int) -> Any:
    # Imports intentionally occur only inside load(), keeping API startup lightweight.
    import torch
    from qwen_asr import Qwen3ASRModel

    if requested_device == "auto":
        device = "mps" if torch.backends.mps.is_available() else "cpu"
    elif requested_device == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError("MPS was requested but is not available to PyTorch")
    else:
        device = requested_device
    dtype = torch.float16 if device == "mps" else torch.float32
    return Qwen3ASRModel.from_pretrained(
        str(model_path),
        dtype=dtype,
        device_map=device,
        attn_implementation="sdpa",
        max_inference_batch_size=1,
        max_new_tokens=max_new_tokens,
        local_files_only=True,
    )


def _result_value(result: Any, name: str, default: Any) -> Any:
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


def _qwen_language(language: str | None) -> str | None:
    return {"en": "English", "zh": "Chinese"}.get(language, language)


def _normalize_language(language: Any) -> str | None:
    if language is None:
        return None
    value = str(language).strip()
    return {"english": "en", "chinese": "zh", "mandarin": "zh"}.get(
        value.lower(), value
    )
