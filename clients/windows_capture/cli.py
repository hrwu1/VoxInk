"""The native Windows capture backend's command line.

The counterpart of `main.swift`. It speaks the same command line as the Swift
`voxink-capture`, so the transcription helper can launch either without
knowing which it has, and it is headless: the helper launches it and the
interfaces draw captions from the helper's status feed.

Controls: SIGUSR1 pauses, SIGUSR2 resumes, SIGINT or SIGTERM stops, where
those signals exist. On every platform `pause`, `resume`, and `stop` lines on
stdin do the same, which is how the helper drives this backend on Windows,
where none of those signals can be sent to another process.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from dataclasses import replace
import os
from pathlib import Path
import signal
import sys
import threading
from typing import Any, TextIO

from voxink_core.session_ids import new_session_id

from .coordinator import CaptureCoordinator
from .models import CaptureConfiguration, CaptureError, CaptureStatusEvent, SystemAudioBackend, SystemAudioSelection
from .service_client import MissingDependency as WebSocketsMissing
from .windows_audio import MissingDependency as AudioMissing
from .windows_audio import input_devices, running_applications

CONTROL_ACTIONS = ("pause", "resume", "stop")


def supported_sources() -> list[str]:
    """What `--list-sources` answers: the helper asks every backend this."""
    sources = ["microphone"]
    if sys.platform == "win32":
        sources.append("system")
    return sources


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voxink-windows-capture",
        description="Stream the microphone and system audio to a local VoxInk helper.",
        allow_abbrev=False,
    )
    parser.add_argument("--mic", action="store_true", help="capture the microphone")
    parser.add_argument("--mic-device", default=None, metavar="UID", help="input device from --list-devices")
    parser.add_argument("--system-all", action="store_true", help="capture every application's audio")
    parser.add_argument("--app", action="append", default=[], metavar="EXE", help="capture one application (repeatable)")
    parser.add_argument("--backend", default="auto", metavar="NAME", help="auto, process-loopback, or loopback")
    parser.add_argument("--language", choices=("en", "zh"), default=None)
    parser.add_argument("--endpoint", default=None, help="session WebSocket URL")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--retain-audio", action="store_true")
    parser.add_argument("--spool-dir", default=None, metavar="DIR", help="where audio the service never received is saved")
    # Holding the input volume is a macOS helper feature; accepted so the
    # helper's one command line fits this backend too.
    parser.add_argument("--hold-mic-gain", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--list-apps", action="store_true")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--list-sources", action="store_true")
    return parser


def capture_configuration(args: argparse.Namespace, application_names: dict[str, str] | None = None) -> CaptureConfiguration:
    """The capture this command line asks for, or ValueError when it cannot be served."""
    if args.system_all and args.app:
        raise ValueError("Choose --system-all or one or more --app values, not both.")
    if args.app:
        system_audio: SystemAudioSelection | None = SystemAudioSelection.for_applications(args.app)
    elif args.system_all:
        system_audio = SystemAudioSelection.all()
    else:
        system_audio = None
    if system_audio is not None and sys.platform != "win32":
        raise ValueError("system audio needs Windows; this backend serves only the microphone elsewhere")
    if not args.mic and system_audio is None:
        raise ValueError(CaptureError.no_capture_source().args[0])
    backend = SystemAudioBackend.parse(args.backend)
    session_id = args.session_id or new_session_id()
    return CaptureConfiguration(
        session_id=session_id,
        microphone_enabled=args.mic,
        system_audio=system_audio,
        service_url=args.endpoint,
        auth_token=os.environ.get("VOXINK_AUTH_SECRET"),
        retain_audio=args.retain_audio,
        backend=backend,
        microphone_device_uid=args.mic_device,
        language=args.language,
        application_names=dict(application_names or {}),
        spool_directory=Path(args.spool_dir) if args.spool_dir else None,
    )


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
        # The helper on Windows holds the pipe for as long as it lives, so its
        # end is the helper's end: stop rather than record into the void. On
        # macOS the helper gives no stdin at all, so EOF there means nothing.
        self._stop_on_eof = stop_on_eof
        self._installed: list[tuple[int, Any]] = []
        self._reader: threading.Thread | None = None

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
                previous = signal.signal(number, lambda *_args, action=action: self._loop.call_soon_threadsafe(self._on_action, action))
                self._installed.append((number, previous))
            except (OSError, RuntimeError, ValueError):
                pass
        if self._stdin is not None:
            self._reader = threading.Thread(target=self._read_stdin, name="voxink-control-stdin", daemon=True)
            self._reader.start()

    def remove(self) -> None:
        for number, previous in self._installed:
            if previous is None:
                try:
                    self._loop.remove_signal_handler(number)
                except (NotImplementedError, RuntimeError, ValueError):
                    pass
            else:
                try:
                    signal.signal(number, previous)
                except (OSError, RuntimeError, ValueError):
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


def stdin_is_control_pipe(stdin: TextIO | None) -> bool:
    """Whether stdin's end should end the capture: only on Windows, where the
    helper always drives this backend through a pipe it holds until it exits."""
    if stdin is None or sys.platform != "win32":
        return False
    try:
        return not stdin.isatty()
    except (AttributeError, ValueError):
        return False


async def run(
    coordinator: CaptureCoordinator,
    stdin: TextIO | None = None,
    session_id: str = "",
    stop_on_eof: bool | None = None,
) -> int:
    """Capture until stopped or failed; always release the sources and sockets."""
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    controls: set[asyncio.Task[None]] = set()

    def control(action: str) -> None:
        if action == "stop":
            stopped.set()
            return
        task = loop.create_task(getattr(coordinator, action)())
        controls.add(task)
        task.add_done_callback(controls.discard)

    if stop_on_eof is None:
        stop_on_eof = stdin_is_control_pipe(stdin)
    channel = ControlChannel(loop, control, stdin, stop_on_eof=stop_on_eof)
    channel.install()
    try:
        try:
            await coordinator.start()
        except CaptureError as exc:
            print(str(exc), file=sys.stderr, flush=True)
            return 1
        print(f"Capture started for session {session_id}. Press Ctrl-C to stop.", flush=True)
        try:
            await stopped.wait()
        except KeyboardInterrupt:
            pass
    finally:
        for task in list(controls):
            task.cancel()
        if controls:
            await asyncio.gather(*controls, return_exceptions=True)
        try:
            await coordinator.stop()
        finally:
            channel.remove()
    print("Capture stopped.", flush=True)
    return 0


def print_transcript(event: dict[str, Any]) -> None:
    """Finals as `[LABEL] text`, matching the Swift backend's stdout."""
    if not event.get("is_final"):
        return
    label = event.get("channel_name") or str(event.get("source", "microphone")).upper()
    print(f"[{label}] {event.get('text', '')}", flush=True)
    translation = event.get("translation")
    if translation:
        print(f"    {translation}", flush=True)


def print_status(event: CaptureStatusEvent) -> None:
    """One `code: message` line on stderr, the form the helper reads as a notice."""
    print(f"{event.code}: {event.message}", file=sys.stderr, flush=True)


def _application_names(configuration: CaptureConfiguration) -> dict[str, str]:
    """Names let a per-application stream be labeled "Zoom" rather than
    "zoom.exe". Best effort: an application that is not running yet is
    labeled by its identifier until it is."""
    wanted = set(configuration.selected_applications)
    if not wanted:
        return {}
    try:
        return {
            application.identifier: application.name
            for application in running_applications()
            if application.identifier in wanted
        }
    except Exception:  # noqa: BLE001
        return {}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.list_sources:
            for source in supported_sources():
                print(source)
            return 0
        if args.list_apps:
            for application in running_applications():
                print(f"{application.identifier}\t{application.name}\tpid={application.process_id}")
            return 0
        if args.list_devices:
            for device in input_devices():
                print(f"{device.uid}\t{device.name}" + ("\tdefault" if device.is_default else ""))
            return 0
        configuration = capture_configuration(args)
        if configuration.selected_applications:
            configuration = replace(configuration, application_names=_application_names(configuration))
        coordinator = CaptureCoordinator(
            configuration, transcript_handler=print_transcript, status_handler=print_status,
        )
        return asyncio.run(run(coordinator, stdin=sys.stdin, session_id=configuration.session_id))
    except (ValueError, WebSocketsMissing, AudioMissing) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 2
    except CaptureError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - the helper reports the last stderr line
        print(f"capture_failed: {exc}", file=sys.stderr, flush=True)
        return 1
