from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import MagicMock

from voxink_cli.envelope import Failure
from voxink_dev.terminal import run_terminal


class FakeHelper:
    """The helper's capture routes as the Terminal loop sees them."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.state = "ready"
        self.events: list[dict] = []
        self.session_id = "2026-09-09_10-00-00"
        self.start_failure: Failure | None = None
        self.lock = threading.Lock()

    def client(self) -> MagicMock:
        client = MagicMock()
        client.start_capture.side_effect = self._start
        client.control_capture.side_effect = self._control
        client.mark_capture.side_effect = self._mark
        client.capture_status.side_effect = self._status
        return client

    def _start(self, **kwargs):
        if self.start_failure is not None:
            raise self.start_failure
        self.calls.append(("start", kwargs))
        with self.lock:
            self.state = "recording"
        return {"session_id": self.session_id, "state": "recording", "next_cursor": 0}

    def _control(self, action: str):
        self.calls.append((action,))
        with self.lock:
            self.state = {"pause": "paused", "resume": "recording", "stop": "ready"}[action]
        return {"state": self.state, "session_id": self.session_id}

    def _mark(self, label: str):
        self.calls.append(("mark", label))
        return {"ok": True}

    def _status(self, *, after: int):
        with self.lock:
            events = self.events[after:]
            return {
                "state": self.state,
                "session_id": self.session_id,
                "events": events,
                "next_cursor": len(self.events),
                "error": None,
            }


class TerminalLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.helper = FakeHelper()
        self.output: list[str] = []
        self.temporary = TemporaryDirectory()
        self.output_root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def _run(self, lines: list[str], **overrides) -> int:
        script = iter(lines)

        def read(prompt: str) -> str:
            try:
                return next(script)
            except StopIteration as exc:
                raise EOFError from exc

        options = dict(
            sources=("microphone",), applications=(), retain_audio=True,
            output_root=self.output_root, read=read, write=self.output.append,
            poll_interval=0.01,
        )
        options.update(overrides)
        return run_terminal(self.helper.client, **options)

    def test_commands_reach_the_helpers_capture_routes(self) -> None:
        code = self._run(["start", "pause", "resume", "mark lunch", "stop", "quit"])
        self.assertEqual(0, code)
        self.assertEqual(
            [
                ("start", {"sources": ["microphone"], "applications": [], "retain_audio": True}),
                ("pause",), ("resume",), ("mark", "lunch"), ("stop",),
            ],
            self.helper.calls,
        )
        self.assertIn("capturing (session 2026-09-09_10-00-00)", self.output)
        self.assertIn("paused", self.output)
        self.assertIn("marked", self.output)

    def test_the_launchers_sources_and_retention_are_what_start_sends(self) -> None:
        self._run(
            ["start", "quit"],
            sources=("microphone", "system"), applications=("us.zoom.xos",), retain_audio=False,
        )
        self.assertEqual(
            {"sources": ["microphone", "system"], "applications": ["us.zoom.xos"], "retain_audio": False},
            self.helper.calls[0][1],
        )

    def test_quitting_mid_capture_stops_it_and_names_the_transcript(self) -> None:
        session = self.output_root / self.helper.session_id
        session.mkdir()
        (session / "transcript-part-001.md").write_text("# hi\n")
        self._run(["start", "quit"])
        self.assertEqual([("start", {"sources": ["microphone"], "applications": [], "retain_audio": True}), ("stop",)], self.helper.calls)
        self.assertIn(f"Transcript saved: {session / 'transcript-part-001.md'}", self.output)

    def test_end_of_input_is_a_quit(self) -> None:
        self.assertEqual(0, self._run([]))
        self.assertEqual([], self.helper.calls)

    def test_captions_from_the_status_feed_are_printed(self) -> None:
        self.helper.events = [
            {"type": "transcript", "source": "microphone", "text": "hello there", "is_final": True},
            {"type": "mark", "text": "lunch"},
        ]
        self._run(["start", "stop", "quit"])
        self.assertIn("[microphone] hello there", self.output)
        self.assertIn("[mark] lunch", self.output)
        self.assertIn("capture ready", self.output)

    def test_a_refused_start_is_reported_and_the_loop_continues(self) -> None:
        self.helper.start_failure = Failure(
            "invalid_request", "system audio needs the native macOS capture helper"
        )
        self._run(["start", "quit"])
        self.assertIn("error: system audio needs the native macOS capture helper", self.output)
        self.assertEqual([], self.helper.calls)

    def test_unknown_commands_list_the_valid_ones(self) -> None:
        self._run(["dance", "quit"])
        self.assertTrue(any(line.startswith("unknown command; use start") for line in self.output))


if __name__ == "__main__":
    unittest.main()
