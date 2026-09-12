"""`voxink shell`: the command as an interactive Terminal interface.

Each line typed at the prompt is a `voxink` command line, run through the same
parser and handlers as a one-shot call, so the shell has no vocabulary of its
own. Bare `start`, `pause`, `resume`, `stop`, `mark`, and `status` expand to
their `capture` commands. Captions from the helper's status feed print between
prompts, whichever surface started the capture.

The shell never starts a helper. `voxink-dev --cli` starts one and opens the
shell with `--stop-on-exit`, so quitting ends the capture along with the
helper; a shell opened by hand against a running helper leaves the capture
running when it exits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import shlex
import threading
import time
from typing import Any, Callable, Sequence

from .client import ControlClient
from .envelope import Failure

PROMPT = "voxink> "
POLL_INTERVAL_SECONDS = 0.5
CAPTURE_SETTLED_STATES = frozenset({"ready", "failed"})
SHORTHAND = {
    "start": ("capture", "start"),
    "pause": ("capture", "pause"),
    "resume": ("capture", "resume"),
    "stop": ("capture", "stop"),
    "mark": ("capture", "mark"),
    "status": ("capture", "status"),
}
QUIT = frozenset({"quit", "exit"})


@dataclass(frozen=True, slots=True)
class StartDefaults:
    """What a bare `start` asks for; the launcher fills these from its flags."""

    sources: tuple[str, ...] = ("microphone",)
    applications: tuple[str, ...] = ()
    retain_audio: bool = True

    def argv(self) -> list[str]:
        argv: list[str] = []
        for source in self.sources:
            argv += ["--source", source]
        for application in self.applications:
            argv += ["--app", application]
        argv.append("--retain-audio" if self.retain_audio else "--no-retain-audio")
        return argv


CURSOR_COMMANDS = frozenset({"pause", "resume", "stop", "status"})


def expand(words: Sequence[str], defaults: StartDefaults, *, seen: int = 0) -> list[str]:
    """The `voxink` argv a typed line stands for.

    `seen` is the caption cursor the shell has already printed up to; the
    commands that would otherwise repeat the feed are told to start there.
    """
    if not words:
        return []
    head, rest = words[0].lower(), list(words[1:])
    if head in SHORTHAND:
        argv = [*SHORTHAND[head], *rest]
        if head == "start" and not any(word in {"--source", "--app"} for word in rest):
            argv += defaults.argv()
        if head in CURSOR_COMMANDS and "--after" not in rest:
            argv += ["--after", str(seen)]
        return argv
    if head in {"help", "?"}:
        return ["--help", *rest]
    return list(words)


@dataclass
class _Follower:
    """Print capture events as they arrive, on a background thread."""

    connect: Callable[[], ControlClient]
    write: Callable[[str], None]
    event_line: Callable[[dict[str, Any]], str]
    interval: float
    state: str | None = None
    session_id: str | None = None
    last_session: dict[str, Any] | None = None
    settled_session: str | None = None
    cursor: int = 0
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="voxink-shell-captions", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def catch_up(self) -> int:
        """Poll once now and return the cursor the shell has printed up to.

        Called before a command that would otherwise repeat the feed, so the
        command is told where the shell's captions already reach.
        """
        with self._lock:
            try:
                self._poll()
            except Failure:
                pass
            return self.cursor

    def expect_stop(self) -> None:
        """The shell is stopping the capture itself; the command reports the end."""
        with self._lock:
            self.settled_session = self.session_id

    def _run(self) -> None:
        final = False
        while True:
            with self._lock:
                try:
                    self._poll()
                except Failure as exc:
                    self.write(f"captions unavailable: {exc.message}")
                    return
            if final:
                return
            # A stop request gets one more poll so trailing captions print.
            final = self._stop.wait(self.interval)

    def _poll(self) -> None:
        view = self.connect().capture_status(after=self.cursor)
        session_id = view.get("session_id")
        if session_id != self.session_id:
            # A new capture, started here or from another surface; its feed
            # starts over. `capture start` already announced it.
            self.session_id = session_id
            self.cursor = 0
            view = self.connect().capture_status(after=0)
        self.cursor = view.get("next_cursor", self.cursor)
        for event in view.get("events", []):
            self.write(self.event_line(event))
        self.last_session = view.get("last_session") or self.last_session
        state = view.get("state")
        # Report each session's end once, even if no poll saw it running.
        if session_id and state in CAPTURE_SETTLED_STATES and self.settled_session != session_id:
            self.settled_session = session_id
            detail = ""
            if state == "failed":
                detail = view.get("error") or ""
                if view.get("exit_code") is not None:
                    detail = f"{detail} (backend exit code {view['exit_code']})".strip()
            self.write(f"capture {state}" + (f": {detail}" if detail else ""))
        self.state = state


def run_shell(
    run_line: Callable[[list[str]], int],
    connect: Callable[[], ControlClient],
    *,
    event_line: Callable[[dict[str, Any]], str],
    defaults: StartDefaults = StartDefaults(),
    stop_on_exit: bool = False,
    read: Callable[[str], str] | None = None,
    write: Callable[[str], None] = print,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> int:
    """Run the prompt until quit, Ctrl-C, or end of input."""
    read = read or input
    follower = _Follower(connect, write, event_line, poll_interval)
    follower.start()
    write(
        "Type any voxink command, or start, pause, resume, stop, mark [note], "
        "status, help, quit. A bare start records "
        + ", ".join(defaults.sources)
        + "."
    )
    try:
        while True:
            try:
                line = read(PROMPT)
            except (EOFError, KeyboardInterrupt):
                write("")
                return 0
            try:
                words = shlex.split(line)
            except ValueError as exc:
                write(f"cannot parse that line: {exc}")
                continue
            if not words:
                continue
            head = words[0].lower()
            if head in QUIT:
                return 0
            seen = follower.catch_up() if head in CURSOR_COMMANDS else 0
            if head == "stop":
                follower.expect_stop()
            run_line(expand(words, defaults, seen=seen))
    finally:
        _finish(run_line, connect, follower, write, stop_on_exit)


def _capture_running(connect: Callable[[], ControlClient]) -> bool:
    try:
        return connect().capture_status(after=0, include_events=False).get("state") not in CAPTURE_SETTLED_STATES
    except Failure:
        return False


def _finish(
    run_line: Callable[[list[str]], int],
    connect: Callable[[], ControlClient],
    follower: _Follower,
    write: Callable[[str], None],
    stop_on_exit: bool,
) -> None:
    if stop_on_exit and _capture_running(connect):
        seen = follower.catch_up()
        follower.expect_stop()
        run_line(["capture", "stop", "--after", str(seen)])
        deadline = time.monotonic() + 10
        while follower.state not in CAPTURE_SETTLED_STATES and time.monotonic() < deadline:
            time.sleep(0.05)
    follower.stop()
    last = follower.last_session
    if stop_on_exit and last and last.get("transcript_parts"):
        write(f"Transcript saved: {last['directory']}/{last['transcript_parts'][-1]}")
