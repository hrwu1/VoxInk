#!/usr/bin/env python3
"""Transcribe an audio file with the local Qwen3-ASR model."""

import argparse
import os
from pathlib import Path

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import torch
from qwen_asr import Qwen3ASRModel


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL_DIR = PROJECT_DIR / "Qwen3-ASR-1.7B"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path, help="Audio file to transcribe")
    parser.add_argument(
        "--model",
        type=Path,
        default=DEFAULT_MODEL_DIR,
        help=f"Local model directory (default: {DEFAULT_MODEL_DIR})",
    )
    parser.add_argument(
        "--language",
        default=None,
        help='Force a language such as "English"; default is automatic detection',
    )
    parser.add_argument(
        "--device",
        choices=("auto", "mps", "cpu"),
        default="auto",
        help="Inference device (default: auto)",
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=512,
        help="Maximum transcript tokens to generate (default: 512)",
    )
    return parser.parse_args()


def choose_device(requested: str) -> str:
    if requested == "auto":
        return "mps" if torch.backends.mps.is_available() else "cpu"
    if requested == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError("MPS was requested but is not available to PyTorch")
    return requested


def main() -> None:
    args = parse_args()
    audio_path = args.audio.expanduser().resolve()
    model_path = args.model.expanduser().resolve()

    if not audio_path.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    if not model_path.is_dir():
        raise FileNotFoundError(f"Model directory not found: {model_path}")

    device = choose_device(args.device)
    dtype = torch.float16 if device == "mps" else torch.float32
    print(f"Loading {model_path.name} on {device} ({dtype})...", flush=True)

    model = Qwen3ASRModel.from_pretrained(
        str(model_path),
        dtype=dtype,
        device_map=device,
        attn_implementation="sdpa",
        max_inference_batch_size=1,
        max_new_tokens=args.max_new_tokens,
    )

    print(f"Transcribing {audio_path.name}...", flush=True)
    result = model.transcribe(
        audio=str(audio_path),
        language=args.language,
    )[0]

    print(f"Language: {result.language}")
    print(result.text)


if __name__ == "__main__":
    main()
