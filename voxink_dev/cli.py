"""Command-line entry point for the VoxInk developer edition."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import signal
import subprocess
import sys
import time

from .preflight import (
    DEFAULT_MODEL_PATH,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_STATE_ROOT,
    PreflightError,
    PreflightOptions,
    run_preflight,
)
from .supervisor import HelperConfig, HelperSupervisor, SupervisorError
from .diagnostics import DiagnosticLog


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
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto")
    parser.add_argument("--session-id")
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
    if args.source != "microphone":
        print("System capture integration is not available in this implementation slice.", file=sys.stderr)
        return 2
    session_id = args.session_id or datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    diagnostics = DiagnosticLog(report.log_root)
    supervisor = HelperSupervisor(
        HelperConfig(report.model_path, report.output_root, device=args.device),
        diagnostics=diagnostics,
    )
    try:
        health = supervisor.start()
        print(
            f"Helper ready on a private loopback port "
            f"(protocol {health['protocol_version']}, provider {health['provider']})."
        )
        endpoint = f"ws://127.0.0.1:{supervisor.port}/v1/sessions/{session_id}/stream"
        command = [
            sys.executable,
            "-m",
            "clients.python_capture",
            "--url",
            endpoint,
            "--session-id",
            session_id,
        ]
        capture = subprocess.Popen(command, env=supervisor.capture_environment())
        diagnostics.emit("capture.started", component="capture", pid=capture.pid)
        try:
            while capture.poll() is None:
                helper = supervisor.process
                if helper is None or helper.poll() is not None:
                    raise SupervisorError("transcription helper exited unexpectedly")
                time.sleep(0.1)
            diagnostics.emit(
                "capture.stopped",
                component="capture",
                pid=capture.pid,
                return_code=capture.returncode,
            )
            transcripts = sorted((report.output_root / session_id).glob("transcript-part-*.md"))
            if transcripts:
                print(f"Transcript saved: {transcripts[-1]}")
            else:
                print("Session stopped before any transcript text was finalized.")
            return int(capture.returncode)
        except KeyboardInterrupt:
            capture.send_signal(signal.SIGINT)
            return capture.wait(timeout=10)
        finally:
            if capture.poll() is None:
                capture.terminate()
                capture.wait(timeout=5)
    except (OSError, SupervisorError, subprocess.TimeoutExpired) as exc:
        diagnostics.emit("supervisor.failed", level="error", component="supervisor")
        print(f"VoxInk failed: {exc}", file=sys.stderr)
        return 1
    finally:
        supervisor.stop()
        diagnostics.close()


if __name__ == "__main__":
    raise SystemExit(main())
