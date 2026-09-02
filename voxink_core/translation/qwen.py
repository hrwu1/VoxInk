"""Local Qwen3 instruct model used as a translator, loaded offline only."""

from __future__ import annotations

import gc
import os
from pathlib import Path
import threading
from typing import Any, Callable

from .base import LANGUAGE_NAMES

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


#: Returns `(tokenizer, model, device)`.
ModelLoader = Callable[[Path, str], tuple[Any, Any, str]]

_SYSTEM_PROMPT = (
    "You are a professional interpreter. Translate the user's text from {source} "
    "to {target}. Reply with the translation only: no notes, no quotes, no "
    "explanations. Keep names, numbers, and code words as they are."
)


class QwenLocalTranslator:
    """Translate short segments with a local Qwen3 instruct checkpoint.

    Inference is serialized: one model serves every stream, and the service
    calls this from a worker thread so captions are never held up by it.
    """

    name = "qwen-translate-local"

    def __init__(
        self,
        model_path: str | Path,
        *,
        device: str = "auto",
        max_new_tokens: int = 256,
        model_loader: ModelLoader | None = None,
    ) -> None:
        if device not in {"auto", "mps", "cpu"}:
            raise ValueError("device must be one of: auto, mps, cpu")
        self.model_path = Path(model_path).expanduser().resolve()
        self.device = device
        self.max_new_tokens = max_new_tokens
        self._model_loader = model_loader
        self._lock = threading.Lock()
        self._tokenizer: Any = None
        self._model: Any = None
        self._resolved_device: str | None = None

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def resolved_device(self) -> str | None:
        return self._resolved_device

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            if not (self.model_path / "config.json").is_file():
                raise FileNotFoundError(f"translation model not found: {self.model_path}")
            loader = self._model_loader or _load_qwen
            self._tokenizer, self._model, self._resolved_device = loader(self.model_path, self.device)

    def translate(self, text: str, source_language: str, target_language: str) -> str:
        cleaned = " ".join(text.split())
        if not cleaned:
            return ""
        self.load()
        source = LANGUAGE_NAMES.get(source_language, source_language)
        target = LANGUAGE_NAMES.get(target_language, target_language)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT.format(source=source, target=target)},
            {"role": "user", "content": cleaned},
        ]
        with self._lock:
            tokenizer, model = self._tokenizer, self._model
            if model is None:
                raise RuntimeError("the translation model was unloaded")
            return _generate(tokenizer, model, messages, self.max_new_tokens, cleaned)

    def unload(self) -> None:
        with self._lock:
            model, self._model = self._model, None
            self._tokenizer = None
        if model is not None:
            del model
            gc.collect()
            try:
                import torch  # noqa: PLC0415

                if torch.backends.mps.is_available():
                    torch.mps.empty_cache()
            except (ImportError, RuntimeError):
                pass


def _load_qwen(model_path: Path, requested_device: str) -> tuple[Any, Any, str]:
    import torch  # noqa: PLC0415 - deferred: importing Torch costs seconds
    from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: PLC0415

    if requested_device == "auto":
        device = "mps" if torch.backends.mps.is_available() else "cpu"
    elif requested_device == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError("MPS was requested but is not available to PyTorch")
    else:
        device = requested_device
    dtype = torch.float16 if device == "mps" else torch.float32
    tokenizer = AutoTokenizer.from_pretrained(str(model_path), local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        str(model_path), dtype=dtype, local_files_only=True
    )
    model.to(device)
    model.eval()
    return tokenizer, model, device


def _generate(tokenizer: Any, model: Any, messages: list[dict[str, str]], max_new_tokens: int, text: str) -> str:
    import torch  # noqa: PLC0415

    prompt = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
        # Qwen3 reasons out loud unless told not to; a caption cannot wait.
        enable_thinking=False,
    )
    inputs = tokenizer([prompt], return_tensors="pt").to(model.device)
    # A translation is about as long as its source; the cap stops a runaway
    # generation from stalling every later segment behind it.
    budget = max(16, min(max_new_tokens, 2 * len(text) + 16))
    with torch.no_grad():
        output = model.generate(
            **inputs,
            max_new_tokens=budget,
            do_sample=False,
            pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id,
        )
    generated = output[0][inputs["input_ids"].shape[-1] :]
    decoded = tokenizer.decode(generated, skip_special_tokens=True)
    # Some checkpoints still emit an empty think block even when disabled.
    if "</think>" in decoded:
        decoded = decoded.rsplit("</think>", 1)[-1]
    return " ".join(decoded.split())
