"""Interactive command-line entry point for live microphone capture."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import os
import sys

from .client import CaptureConfig, MicrophoneCaptureClient, MissingDependency
from .vad import VadConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stream a microphone to local VoxInk")
    parser.add_argument("--url", default=None, help="WebSocket URL (session ID is appended by default)")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--stream-id", default="microphone-1")
    parser.add_argument("--device", default=None, help="sounddevice input device ID or name")
    parser.add_argument("--sample-rate", type=int, default=16_000)
    parser.add_argument("--frame-ms", type=int, default=20)
    parser.add_argument(
        "--stream-mode",
        choices=("frames", "vad-chunks"),
        default="vad-chunks",
        help="vad-chunks transcribes live; frames is reserved for server-side VAD",
    )
    parser.add_argument(
        "--language",
        choices=("en", "zh"),
        default=None,
        help="force a language; omit for automatic detection",
    )
    parser.add_argument("--vad-threshold", type=float, default=500.0)
    parser.add_argument("--silence-ms", type=int, default=700)
    parser.add_argument("--min-speech-ms", type=int, default=200)
    parser.add_argument("--max-chunk-ms", type=int, default=15_000)
    parser.add_argument("--pre-roll-ms", type=int, default=200)
    parser.add_argument("--overlap-ms", type=int, default=200)
    return parser


async def run(args: argparse.Namespace) -> int:
    session_id = args.session_id or datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    url = args.url or f"ws://127.0.0.1:8765/v1/sessions/{session_id}/stream"
    device: str | int | None = args.device
    if isinstance(device, str) and device.isdecimal():
        device = int(device)
    vad = VadConfig(
        sample_rate_hz=args.sample_rate,
        frame_duration_ms=args.frame_ms,
        threshold_rms=args.vad_threshold,
        silence_duration_ms=args.silence_ms,
        min_speech_duration_ms=args.min_speech_ms,
        max_chunk_duration_ms=args.max_chunk_ms,
        pre_roll_ms=args.pre_roll_ms,
        overlap_ms=args.overlap_ms,
    )
    client = MicrophoneCaptureClient(
        CaptureConfig(
            websocket_url=url,
            session_id=session_id,
            stream_id=args.stream_id,
            sample_rate_hz=args.sample_rate,
            frame_duration_ms=args.frame_ms,
            device=device,
            stream_mode=args.stream_mode,
            language=args.language,
            vad=vad,
            auth_token=os.environ.get("VOXINK_AUTH_SECRET"),
        )
    )

    print(f"Session: {session_id}")
    print(f"Endpoint: {url}")
    print("Commands: start, pause, resume, stop, quit")
    while True:
        try:
            command = (await asyncio.to_thread(input, "voxink> ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            command = "quit"
        try:
            if command == "start":
                await client.start()
                print("capturing")
            elif command == "pause":
                await client.pause()
                print("paused")
            elif command == "resume":
                await client.resume()
                print("capturing")
            elif command == "stop":
                await client.stop()
                print(f"stopped (dropped frames: {client.dropped_frames})")
            elif command in {"quit", "exit"}:
                await client.stop()
                return 0
            elif command:
                print("unknown command; use start, pause, resume, stop, or quit")
        except MissingDependency as exc:
            print(f"error: {exc}", file=sys.stderr)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
