"""The Terminal interface: typed commands over the helper's control API.

Like the menu bar and the browser, it never touches audio. `start` asks the
helper to launch its capture backend, the other commands go to the same
`/v1/ui/capture/*` routes those surfaces use, and captions are read from the
status feed the public `voxink capture watch` follows.
"""

from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Callable, Sequence

from voxink_cli.cli import CAPTURE_SETTLED_STATES, event_line
from voxink_cli.client import ControlClient
from voxink_cli.envelope import Failure

COMMANDS = "start, pause, resume, stop, mark [note], quit"
POLL_INTERVAL_SECONDS = 0.5


class _CaptionFollower:
    """Print the helper's capture events as they arrive, on a background thread."""

    def __init__(
        self, connect: Callable[[], ControlClient], write: Callable[[str], None], interval: float
    ) -> None:
        self._connect = connect
        self._write = write
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.session_id: str | None = None
        self.state: str | None = None

    def follow(self, session_id: str, cursor: int) -> None:
        self.stop()
        self.session_id = session_id
        self.state = "recording"
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(cursor,), name="voxink-terminal-captions", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self, cursor: int) -> None:
        final = False
        while True:
            try:
                view = self._connect().capture_status(after=cursor)
            except Failure as exc:
                self._write(f"error: {exc.message}")
                return
            cursor = view.get("next_cursor", cursor)
            for event in view.get("events", []):
                self._write(event_line(event))
            self.state = view.get("state")
            if self.state in CAPTURE_SETTLED_STATES:
                error = view.get("error")
                self._write(f"capture {self.state}" + (f": {error}" if error else ""))
                return
            if final:
                return
            # A stop request is answered with one more poll, so captions
            # finalized after the last one are still printed.
            final = self._stop.wait(self._interval)


def run_terminal(
    connect: Callable[[], ControlClient],
    *,
    sources: Sequence[str],
    applications: Sequence[str],
    retain_audio: bool,
    output_root: Path,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> int:
    """Run the command loop until quit, Ctrl-C, or end of input."""
    follower = _CaptionFollower(connect, write, poll_interval)
    write(f"Sources: {', '.join(sources)}. Commands: {COMMANDS}")
    try:
        while True:
            try:
                line = read("voxink> ").strip()
            except (EOFError, KeyboardInterrupt):
                line = "quit"
            command, _, argument = line.partition(" ")
            command = command.lower()
            try:
                if command == "start":
                    view = connect().start_capture(
                        sources=list(sources),
                        applications=list(applications),
                        retain_audio=retain_audio,
                    )
                    follower.follow(view["session_id"], view.get("next_cursor", 0))
                    write(f"capturing (session {view['session_id']})")
                elif command in {"pause", "resume", "stop"}:
                    view = connect().control_capture(command)
                    # The response is authoritative; the follower may not have polled yet.
                    follower.state = str(view.get("state", command))
                    write(follower.state)
                elif command == "mark":
                    connect().mark_capture(argument.strip())
                    write("marked")
                elif command in {"quit", "exit"}:
                    return 0
                elif command:
                    write(f"unknown command; use {COMMANDS}")
            except Failure as exc:
                write(f"error: {exc.message}")
    finally:
        _finish(connect, follower, write, output_root)


def _finish(
    connect: Callable[[], ControlClient],
    follower: _CaptionFollower,
    write: Callable[[str], None],
    output_root: Path,
) -> None:
    if follower.session_id is not None and follower.state not in CAPTURE_SETTLED_STATES:
        try:
            connect().control_capture("stop")
        except Failure as exc:
            write(f"error: {exc.message}")
        else:
            # Let the final captions and the settled state print before leaving.
            deadline = time.monotonic() + 10
            while follower.state not in CAPTURE_SETTLED_STATES and time.monotonic() < deadline:
                time.sleep(0.05)
    follower.stop()
    if follower.session_id is None:
        return
    transcripts = sorted((output_root / follower.session_id).glob("transcript-part-*.md"))
    if transcripts:
        write(f"Transcript saved: {transcripts[-1]}")
    else:
        write("Session stopped before any transcript text was finalized.")
