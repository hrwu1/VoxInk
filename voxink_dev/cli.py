"""Command-line entry point for the VoxInk developer edition."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .preflight import (
    DEFAULT_MODEL_PATH,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_STATE_ROOT,
    PreflightError,
    PreflightOptions,
    run_preflight,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument(
        "--source", choices=("microphone", "system", "applications"), default="microphone"
    )
    parser.add_argument("--native-capture", type=Path)
    parser.add_argument("--retain-audio", action="store_true")
    parser.add_argument("--skip-checksums", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--preflight-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_preflight(
            PreflightOptions(
                model_path=args.model,
                output_root=args.output,
                state_root=args.state_root,
                sources=(args.source,),
                retain_audio=args.retain_audio,
                native_capture=args.native_capture,
                verify_checksums=not args.skip_checksums,
            )
        )
    except PreflightError as exc:
        print(f"VoxInk cannot start:\n{exc}", file=sys.stderr)
        return 2
    print(
        f"Preflight passed: {report.checked_files} model files verified, "
        f"{report.free_bytes // (1024 * 1024)} MiB free."
    )
    if args.preflight_only:
        return 0
    print("Supervisor startup will be added in the next implementation step.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
