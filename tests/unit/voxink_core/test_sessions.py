import tempfile
import unittest
from pathlib import Path

from voxink_core.models import SessionConfig, Source, TranscriptEvent
from voxink_core.sessions import InvalidSessionTransition, SessionManager, SessionState


class SessionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.manager = SessionManager(Path(self.temp.name))
        self.config = SessionConfig("session-1", (Source.MICROPHONE,))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_manual_lifecycle_and_persistence(self) -> None:
        session = self.manager.create(self.config)
        self.assertEqual(session.state, SessionState.CREATED)
        path = self.manager.start("session-1")
        self.manager.append(
            TranscriptEvent(
                "session-1", "seg-1", "mic-1", Source.MICROPHONE,
                0, 500, "Hello.", True,
            )
        )
        self.manager.pause("session-1")
        self.assertEqual(session.state, SessionState.PAUSED)
        with self.assertRaises(InvalidSessionTransition):
            self.manager.append(
                TranscriptEvent(
                    "session-1", "seg-2", "mic-1", Source.MICROPHONE,
                    500, 800, "No.", True,
                )
            )
        self.manager.resume("session-1")
        self.manager.stop("session-1")
        self.assertEqual(session.state, SessionState.STOPPED)
        self.assertIn("Hello.", path.read_text())

    def test_invalid_transitions_are_rejected(self) -> None:
        self.manager.create(self.config)
        with self.assertRaises(InvalidSessionTransition):
            self.manager.pause("session-1")
        self.manager.start("session-1")
        with self.assertRaises(InvalidSessionTransition):
            self.manager.start("session-1")
        self.manager.stop("session-1")

    def test_duplicate_and_unknown_sessions_are_clear_errors(self) -> None:
        self.manager.create(self.config)
        with self.assertRaises(ValueError):
            self.manager.create(self.config)
        with self.assertRaisesRegex(KeyError, "unknown session"):
            self.manager.get("missing")


if __name__ == "__main__":
    unittest.main()
