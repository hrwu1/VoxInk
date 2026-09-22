"""The portable microphone capture backend.

Speaks the same command line and control contract as the native backends
(`voxink-capture` on macOS, `voxink-windows-capture` on Windows) so the
transcription helper can launch any of them without knowing which it has:

- `--endpoint`, `--session-id`, `--mic`, `--mic-device`, `--language`,
  `--retain-audio`, `--backend`, `--spool-dir`, `--hold-mic-gain` are accepted; `--system-all`
  and `--app` are refused because only a native backend serves system audio.
- `--list-devices` prints `uid<TAB>name[<TAB>default]`; `--list-sources`
  prints the sources this backend can capture, one per line.
- SIGUSR1 pauses, SIGUSR2 resumes, SIGINT or SIGTERM stops and exits 0, where
  those signals exist. On every platform `pause`, `resume`, and `stop` lines
  on stdin do the same; that is how the helper drives this backend on
  Windows, and there the end of the helper's pipe is also a stop.
- `VOXINK_AUTH_SECRET` authenticates the WebSocket.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
import os
import signal
import sys
import threading
from typing import Any, TextIO

from voxink_core.session_ids import new_session_id

from .client import CaptureConfig, MicrophoneCaptureClient, MissingDependency, list_input_devices

SOURCES = ("microphone",)
CONTROL_ACTIONS = ("pause", "resume", "stop")


def system_audio_hint() -> str:
    """Why system audio is refused, and what serves it on this platform."""
    if sys.platform == "win32":
        return (
            "system audio needs the native Windows capture backend; "
            "install its packages with: python -m pip install -r requirements-capture.txt"
        )
    return "system audio needs the native macOS capture helper; build it with ./scripts/build-macos-capture"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voxink-mic-capture",
        description="Stream the microphone to a local VoxInk helper.",
        allow_abbrev=False,
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
    # line fits every backend. This backend keeps no spool: a failed
    # transport ends the capture explicitly instead.
    parser.add_argument("--system-all", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--app", action="append", default=[], help=argparse.SUPPRESS)
    parser.add_argument("--list-apps", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--backend", default=None, help=argparse.SUPPRESS)
    # Holding the input volume is a native-helper feature; accepted so the
    # helper's one command line fits this backend too.
    parser.add_argument("--hold-mic-gain", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--spool-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--sample-rate", type=int, default=16_000, help=argparse.SUPPRESS)
    parser.add_argument("--frame-ms", type=int, default=20, help=argparse.SUPPRESS)
    return parser


def capture_config(args: argparse.Namespace) -> CaptureConfig:
    """The capture this command line asks for, or ValueError when it cannot be served."""
    if args.system_all or args.app:
        raise ValueError(system_audio_hint())
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


def stdin_is_control_pipe(stdin: TextIO | None) -> bool:
    """Whether stdin's end should end the capture: only on Windows, where the
    helper always drives this backend through a pipe it holds until it exits.
    On macOS the helper gives no stdin at all, so EOF there means nothing."""
    if stdin is None or sys.platform != "win32":
        return False
    try:
        return not stdin.isatty()
    except (AttributeError, ValueError):
        return False


class ControlChannel:
    """Delivers pause, resume, and stop to the event loop from signals where
    they exist and from stdin lines everywhere."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        on_action: Callable[[str], None],
        stdin: TextIO | None,
        stop_on_eof: bool = False,
    ) -> None:
        self._loop = loop
        self._on_action = on_action
        self._stdin = stdin
        self._stop_on_eof = stop_on_eof
        self._installed: list[tuple[int, Any]] = []

    def install(self) -> None:
        handlers = {"SIGINT": "stop", "SIGTERM": "stop", "SIGBREAK": "stop", "SIGUSR1": "pause", "SIGUSR2": "resume"}
        for name, action in handlers.items():
            number = getattr(signal, name, None)
            if number is None:
                continue
            try:
                self._loop.add_signal_handler(number, self._on_action, action)
                self._installed.append((number, None))
                continue
            except (NotImplementedError, RuntimeError, ValueError):
                pass
            # Windows: the loop cannot watch signals, but the interpreter still
            # delivers SIGINT and SIGBREAK to a plain handler.
            try:
                previous = signal.signal(
                    number, lambda *_args, action=action: self._loop.call_soon_threadsafe(self._on_action, action)
                )
                self._installed.append((number, previous))
            except (OSError, RuntimeError, ValueError):
                pass
        if self._stdin is not None:
            threading.Thread(target=self._read_stdin, name="voxink-control-stdin", daemon=True).start()

    def remove(self) -> None:
        for number, previous in self._installed:
            try:
                if previous is None:
                    self._loop.remove_signal_handler(number)
                else:
                    signal.signal(number, previous)
            except (NotImplementedError, OSError, RuntimeError, ValueError):
                pass
        self._installed.clear()

    def _read_stdin(self) -> None:
        stdin = self._stdin
        if stdin is None:
            return
        try:
            for line in stdin:
                action = line.strip().lower()
                if action in CONTROL_ACTIONS:
                    self._loop.call_soon_threadsafe(self._on_action, action)
        except (OSError, ValueError):
            return
        if self._stop_on_eof:
            try:
                self._loop.call_soon_threadsafe(self._on_action, "stop")
            except RuntimeError:
                pass


async def run(
    client: MicrophoneCaptureClient, stdin: TextIO | None = None, stop_on_eof: bool | None = None
) -> int:
    """Capture until stopped or failed; always release the microphone and socket."""
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    controls: set[asyncio.Task[None]] = set()

    def control(action: str) -> None:
        if action == "stop":
            stopped.set()
        else:
            task = asyncio.create_task(getattr(client, action)())
            controls.add(task)
            task.add_done_callback(controls.discard)

    if stop_on_eof is None:
        stop_on_eof = stdin_is_control_pipe(stdin)
    channel = ControlChannel(loop, control, stdin, stop_on_eof=stop_on_eof)
    channel.install()
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
            channel.remove()
    print("Capture stopped.", flush=True)
    return 1 if client.failure is not None else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_sources:
        for source in SOURCES:
            print(source)
        return 0
    if args.list_apps:
        print(system_audio_hint(), file=sys.stderr)
        return 2
    try:
        if args.list_devices:
            for uid, name, is_default in list_input_devices():
                print(f"{uid}\t{name}" + ("\tdefault" if is_default else ""))
            return 0
        client = MicrophoneCaptureClient(capture_config(args))
        return asyncio.run(run(client, stdin=sys.stdin))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except MissingDependency as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # the helper reports the last stderr line
        print(f"capture_failed: {exc}", file=sys.stderr)
        return 1
