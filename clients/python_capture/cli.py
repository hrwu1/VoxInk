"""The portable microphone capture backend.

Speaks the same command line and signal contract as the Swift `voxink-capture`
so the transcription helper can launch either without knowing which it has:

- `--endpoint`, `--session-id`, `--mic`, `--mic-device`, `--language`,
  `--retain-audio`, `--backend` are accepted; `--system-all`
  and `--app` are refused because only the native macOS helper taps system
  audio.
- `--list-devices` prints `uid<TAB>name[<TAB>default]`; `--list-sources`
  prints the sources this backend can capture, one per line.
- SIGUSR1 pauses, SIGUSR2 resumes, SIGINT or SIGTERM stops and exits 0.
- `VOXINK_AUTH_SECRET` authenticates the WebSocket.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from voxink_core.session_ids import new_session_id

from .client import CaptureConfig, MicrophoneCaptureClient, MissingDependency, list_input_devices

SOURCES = ("microphone",)
SYSTEM_AUDIO_HINT = (
    "system audio needs the native macOS capture helper; "
    "build it with ./scripts/build-macos-capture"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voxink-mic-capture",
        description="Stream the microphone to a local VoxInk helper.",
    )
    parser.add_argument("--endpoint", default=None, help="session WebSocket URL")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--mic", action="store_true", help="capture the microphone (the default)")
    parser.add_argument(
        "--mic-device", default=None, metavar="UID", help="input device from --list-devices"
    )
    parser.add_argument("--language", choices=("en", "zh"), default=None)
    parser.add_argument("--retain-audio", action="store_true")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--list-sources", action="store_true")
    # Native-only options: refused, or accepted and ignored, so one command
    # line fits both backends.
    parser.add_argument("--system-all", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--app", action="append", default=[], help=argparse.SUPPRESS)
    parser.add_argument("--list-apps", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--backend", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--sample-rate", type=int, default=16_000, help=argparse.SUPPRESS)
    parser.add_argument("--frame-ms", type=int, default=20, help=argparse.SUPPRESS)
    return parser


def capture_config(args: argparse.Namespace) -> CaptureConfig:
    """The capture this command line asks for, or ValueError when it cannot be served."""
    if args.system_all or args.app:
        raise ValueError(SYSTEM_AUDIO_HINT)
    session_id = args.session_id or new_session_id()
    device: str | int | None = args.mic_device
    if isinstance(device, str) and device.isdecimal():
        device = int(device)
    return CaptureConfig(
        websocket_url=args.endpoint or f"ws://127.0.0.1:8300/v1/sessions/{session_id}/stream",
        session_id=session_id,
        sample_rate_hz=args.sample_rate,
        frame_duration_ms=args.frame_ms,
        device=device,
        language=args.language,
        retain_audio=args.retain_audio,
        auth_token=os.environ.get("VOXINK_AUTH_SECRET"),
    )


async def run(client: MicrophoneCaptureClient) -> int:
    """Capture until stopped or failed; always release the microphone and socket."""
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    controls: set[asyncio.Task[None]] = set()
    installed: list[int] = []

    def control(action: str) -> None:
        if action == "stop":
            stopped.set()
        else:
            task = asyncio.create_task(getattr(client, action)())
            controls.add(task)
            task.add_done_callback(controls.discard)

    handlers = {"SIGINT": "stop", "SIGTERM": "stop", "SIGUSR1": "pause", "SIGUSR2": "resume"}
    for name, action in handlers.items():
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            loop.add_signal_handler(number, control, action)
            installed.append(number)
        except (NotImplementedError, RuntimeError):
            # Windows: only SIGINT, delivered as KeyboardInterrupt below.
            pass

    waiters: list[asyncio.Task[None]] = []
    try:
        await client.start()
        print(f"Capture started for session {client.config.session_id}. Press Ctrl-C to stop.", flush=True)
        waiters = [asyncio.create_task(stopped.wait()), asyncio.create_task(client.wait_failed())]
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    except KeyboardInterrupt:
        pass
    finally:
        pending = [*waiters, *controls]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        try:
            await client.stop()
        finally:
            for number in installed:
                loop.remove_signal_handler(number)
    print("Capture stopped.", flush=True)
    return 1 if client.failure is not None else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_sources:
        for source in SOURCES:
            print(source)
        return 0
    if args.list_apps:
        print(SYSTEM_AUDIO_HINT, file=sys.stderr)
        return 2
    try:
        if args.list_devices:
            for uid, name, is_default in list_input_devices():
                print(f"{uid}\t{name}" + ("\tdefault" if is_default else ""))
            return 0
        client = MicrophoneCaptureClient(capture_config(args))
        return asyncio.run(run(client))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except MissingDependency as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # the helper reports the last stderr line
        print(f"capture_failed: {exc}", file=sys.stderr)
        return 1
