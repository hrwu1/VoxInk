"""Run the VoxInk service on the loopback interface only."""

from __future__ import annotations

import argparse
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
    args = parser.parse_args()
    app = create_app(
        ServiceSettings(
            model_path=args.model.expanduser().resolve(),
            output_root=args.output.expanduser().resolve(),
            device=args.device,
        )
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
