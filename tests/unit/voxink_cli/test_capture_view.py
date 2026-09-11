from __future__ import annotations

import unittest

from voxink_cli.cli import _capture_view


def _view(**overrides) -> dict:
    view = {
        "state": "ready",
        "session_id": "s1",
        "events": [
            {"type": "transcript", "source": "microphone", "text": "one", "is_final": True},
            {"type": "transcript", "source": "microphone", "text": "two", "is_final": True},
            {"type": "mark", "text": "lunch"},
        ],
        "next_cursor": 3,
        "error": None,
    }
    view.update(overrides)
    return view


class CaptureViewTests(unittest.TestCase):
    def test_events_before_the_callers_cursor_are_not_repeated(self) -> None:
        result = _capture_view(_view(), after=2)
        self.assertEqual(["[mark] lunch"], [line for line in result.text.splitlines()[1:]])
        self.assertEqual(3, result.data["next_cursor"])

    def test_a_cursor_behind_a_trimmed_buffer_shows_what_is_left(self) -> None:
        # Absolute cursors: the buffer starts at index 10, the caller last saw 4.
        result = _capture_view(_view(next_cursor=13), after=4)
        self.assertEqual(3, len(result.data["events"]))

    def test_the_backends_closing_summary_is_not_an_error_after_a_clean_exit(self) -> None:
        clean = _capture_view(_view(error="tap_capture_summary: 2845 frames"))
        self.assertNotIn("error:", clean.text)
        failed = _capture_view(_view(state="failed", error="permission denied"))
        self.assertIn("error: permission denied", failed.text)


if __name__ == "__main__":
    unittest.main()
