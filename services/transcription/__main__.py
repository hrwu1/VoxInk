"""Run the VoxInk service on the loopback interface only."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from .app import PROJECT_ROOT, ServiceSettings, create_app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output", type=Path, default=Path.home() / "Documents" / "VoxInk")
    parser.add_argument(
        "--model",
        type=Path,
        default=PROJECT_ROOT / "models" / "Qwen3-ASR-1.7B",
    )
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto")
    parser.add_argument(
        "--vocabulary",
        type=Path,
        default=None,
        help="custom vocabulary file (default: vocabulary.txt beside the transcripts)",
    )
    parser.add_argument(
        "--translation-model",
        type=Path,
        default=PROJECT_ROOT / "models" / "Qwen3-0.6B",
        help="local instruct model used to translate captions (optional)",
    )
    parser.add_argument(
        "--partial-captions",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="show captions while an utterance is still being spoken (default: on)",
    )
    parser.add_argument(
        "--echo-suppression",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="drop microphone chunks that echo a system channel (default: on)",
    )
    parser.add_argument(
        "--speaker-threshold",
        type=float,
        default=0.62,
        help="cosine similarity needed to recognize a remembered voice (default: 0.62)",
    )
    args = parser.parse_args()
    auth_secret = os.environ.get("VOXINK_AUTH_SECRET")
    if not auth_secret:
        parser.error("VOXINK_AUTH_SECRET must be set by the local supervisor")
    app = create_app(
        ServiceSettings(
            model_path=args.model.expanduser().resolve(),
            output_root=args.output.expanduser().resolve(),
            device=args.device,
            vocabulary_path=(
                args.vocabulary.expanduser().resolve() if args.vocabulary else None
            ),
            auth_secret=auth_secret,
            ui_bootstrap_secret=os.environ.get("VOXINK_UI_BOOTSTRAP_SECRET"),
            native_capture_path=(
                Path(value).expanduser().resolve()
                if (value := os.environ.get("VOXINK_NATIVE_CAPTURE"))
                else None
            ),
            translation_model_path=args.translation_model.expanduser().resolve(),
            partial_captions=args.partial_captions,
            echo_suppression=args.echo_suppression,
            speaker_threshold=args.speaker_threshold,
        )
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
