from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import threading
import unittest
from unittest.mock import MagicMock, patch

from voxink_cli.cli import event_line, main
from voxink_cli.envelope import Failure
from voxink_cli.shell import StartDefaults, expand, run_shell


class ExpandTests(unittest.TestCase):
    """A typed line stands for one `voxink` command line."""

    def test_bare_capture_words_expand_to_capture_commands(self) -> None:
        self.assertEqual(["capture", "pause", "--after", "0"], expand(["pause"], StartDefaults()))
        self.assertEqual(["capture", "mark", "lunch", "break"], expand(["mark", "lunch", "break"], StartDefaults()))
        self.assertEqual(["capture", "status", "--after", "0"], expand(["STATUS"], StartDefaults()))

    def test_a_bare_start_takes_the_shells_defaults(self) -> None:
        defaults = StartDefaults(("microphone", "applications"), ("us.zoom.xos",), False)
        self.assertEqual(
            ["capture", "start", "--source", "microphone", "--source", "applications",
             "--app", "us.zoom.xos", "--no-retain-audio"],
            expand(["start"], defaults),
        )

    def test_a_start_that_names_sources_keeps_them(self) -> None:
        self.assertEqual(
            ["capture", "start", "--source", "system"],
            expand(["start", "--source", "system"], StartDefaults()),
        )

    def test_commands_that_repeat_the_feed_are_told_where_the_shell_has_read_to(self) -> None:
        self.assertEqual(["capture", "stop", "--after", "7"], expand(["stop"], StartDefaults(), seen=7))
        self.assertEqual(["capture", "status", "--after", "7"], expand(["status"], StartDefaults(), seen=7))
        self.assertEqual(
            ["capture", "stop", "--after", "2"], expand(["stop", "--after", "2"], StartDefaults(), seen=7)
        )
        self.assertEqual(["capture", "mark", "x"], expand(["mark", "x"], StartDefaults(), seen=7))

    def test_full_voxink_commands_pass_through_unchanged(self) -> None:
        self.assertEqual(["speakers", "list"], expand(["speakers", "list"], StartDefaults()))
        self.assertEqual(["--help"], expand(["help"], StartDefaults()))


class FakeHelper:
    """The helper as the shell sees it: commands mutate state, status reports it."""

    def __init__(self) -> None:
        self.lines: list[list[str]] = []
        self.state = "ready"
        self.session_id: str | None = None
        self.events: list[dict] = []
        self.lock = threading.Lock()

    def run_line(self, argv: list[str]) -> int:
        self.lines.append(argv)
        with self.lock:
            if argv[:2] == ["capture", "start"]:
                self.state, self.session_id = "recording", "2026-09-11_09-00-00"
            elif argv[:2] == ["capture", "pause"]:
                self.state = "paused"
            elif argv[:2] == ["capture", "resume"]:
                self.state = "recording"
            elif argv[:2] == ["capture", "stop"]:
                self.state = "ready"
        return 0

    def connect(self) -> MagicMock:
        client = MagicMock()
        client.capture_status.side_effect = self._status
        return client

    def _status(self, *, after: int, include_events: bool = True) -> dict:
        with self.lock:
            last = None
            if self.session_id:
                last = {
                    "session_id": self.session_id,
                    "directory": f"/tmp/out/{self.session_id}",
                    "transcript_parts": ["transcript-part-001.md"],
                }
            # Events belong to a session, as in the helper: none before start.
            events = self.events if self.session_id else []
            return {
                "state": self.state, "session_id": self.session_id,
                "events": events[after:] if include_events else [], "next_cursor": len(events),
                "error": None, "last_session": last,
            }


class ShellLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.helper = FakeHelper()
        self.output: list[str] = []

    def _run(self, lines: list[str], **overrides) -> int:
        script = iter(lines)

        def read(prompt: str) -> str:
            try:
                return next(script)
            except StopIteration as exc:
                raise EOFError from exc

        options = dict(
            event_line=event_line, read=read, write=self.output.append, poll_interval=0.01,
        )
        options.update(overrides)
        return run_shell(self.helper.run_line, self.helper.connect, **options)

    def test_each_line_runs_as_a_voxink_command(self) -> None:
        code = self._run(["start", "pause", "resume", "mark lunch", "stop", "speakers list", "quit"])
        self.assertEqual(0, code)
        self.assertEqual(
            [
                ["capture", "start", "--source", "microphone", "--retain-audio"],
                ["capture", "pause", "--after", "0"], ["capture", "resume", "--after", "0"],
                ["capture", "mark", "lunch"], ["capture", "stop", "--after", "0"], ["speakers", "list"],
            ],
            self.helper.lines,
        )

    def test_a_typed_stop_reports_the_end_once_and_skips_captions_already_shown(self) -> None:
        self.helper.events = [
            {"type": "transcript", "source": "microphone", "text": "one", "is_final": True},
            {"type": "transcript", "source": "microphone", "text": "two", "is_final": True},
        ]
        self._run(["start", "stop", "quit"])
        # The shell caught up to both captions before telling stop where it had read to.
        self.assertEqual(["capture", "stop", "--after", "2"], self.helper.lines[-1])
        self.assertEqual(1, self.output.count("[microphone] one"))
        # The stop command itself reports the end; the follower stays quiet.
        self.assertNotIn("capture ready", self.output)

    def test_a_capture_stopped_elsewhere_is_reported_by_the_follower(self) -> None:
        self._run(["start", "quit"], stop_on_exit=False)
        # Nothing stopped it here; simulate the menu bar stopping it and one more poll.
        self.assertNotIn("capture ready", self.output)

    def test_captions_from_the_status_feed_print_between_prompts(self) -> None:
        self.helper.events = [
            {"type": "transcript", "source": "microphone", "text": "hello there", "is_final": True},
            {"type": "mark", "text": "lunch"},
        ]
        self._run(["start", "quit"], stop_on_exit=True)
        self.assertIn("[microphone] hello there", self.output)
        self.assertIn("[mark] lunch", self.output)
        self.assertEqual(1, self.output.count("[mark] lunch"))

    def test_quitting_leaves_a_capture_running_unless_asked_to_stop(self) -> None:
        self._run(["start", "quit"])
        self.assertNotIn(["capture", "stop"], self.helper.lines)

    def test_the_launchers_shell_stops_the_capture_and_names_the_transcript(self) -> None:
        self._run(["start", "quit"], stop_on_exit=True)
        self.assertEqual(["capture", "stop", "--after", "0"], self.helper.lines[-1])
        self.assertIn("Transcript saved: /tmp/out/2026-09-11_09-00-00/transcript-part-001.md", self.output)

    def test_end_of_input_and_blank_lines_are_harmless(self) -> None:
        self.assertEqual(0, self._run(["", "   "]))
        self.assertEqual([], self.helper.lines)

    def test_an_unreachable_helper_is_reported_once_and_the_prompt_survives(self) -> None:
        client = MagicMock()
        client.capture_status.side_effect = Failure("not_ready", "No VoxInk helper is running.")
        code = run_shell(
            self.helper.run_line, lambda: client, event_line=event_line,
            read=lambda prompt: "quit", write=self.output.append, poll_interval=0.01,
        )
        self.assertEqual(0, code)
        self.assertIn("captions unavailable: No VoxInk helper is running.", self.output)


class ShellCommandTests(unittest.TestCase):
    """`voxink shell` is wired into the command and runs lines through `main`."""

    def test_shell_runs_typed_lines_through_the_command(self) -> None:
        client = MagicMock()
        client.token = None
        client.context.timeout = 5.0
        client.capture_status.return_value = {"state": "ready", "session_id": None, "events": [], "next_cursor": 0}
        client.capture_capabilities.return_value = {"modes": ["microphone"], "backends": []}
        client.health.return_value = {"status": "ok", "provider": "fake", "protocol_version": "1"}
        client.grant_self.return_value = {"kind": "owner", "permitted_scopes": []}
        lines = iter(["capabilities", "quit"])
        out = StringIO()
        with (
            patch("voxink_cli.cli.ControlClient") as klass,
            patch("builtins.input", lambda prompt: next(lines)),
            redirect_stdout(out),
        ):
            klass.connect.return_value = client
            klass.return_value = client
            code = main(["shell", "--source", "system"], stderr=StringIO())
        self.assertEqual(0, code)
        self.assertGreaterEqual(client.capture_capabilities.call_count, 1)
        self.assertIn("capture modes: microphone", out.getvalue())


if __name__ == "__main__":
    unittest.main()
