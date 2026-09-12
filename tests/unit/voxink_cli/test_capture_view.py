from __future__ import annotations

import argparse
import unittest
from unittest.mock import MagicMock, call, patch

from voxink_cli.cli import _capture_status, _capture_view, _status, _wait_for_settled
from voxink_mcp.server import Session


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
    def test_summary_adapters_read_status_without_caption_permission(self) -> None:
        for adapter in ("cli", "mcp"):
            with self.subTest(adapter=adapter):
                client = MagicMock()
                client.health.return_value = {"status": "ok"}
                client.capture_status.return_value = _view(events=[])
                if adapter == "cli":
                    _status(argparse.Namespace(), client)
                else:
                    Session(token="test", owner=False, client_name="test", client_factory=lambda context, token: client).tool_status()
                client.capture_status.assert_called_once_with(include_events=False)

    def test_capture_status_keeps_its_explicit_event_read_contract(self) -> None:
        client = MagicMock()
        client.capture_status.return_value = _view()
        result = _capture_status(argparse.Namespace(after=0), client)
        self.assertEqual(len(result.data["events"]), 3)
        client.capture_status.assert_called_once_with(after=0)

    def test_capture_only_grant_can_wait_without_requesting_transcript_access(self) -> None:
        client = MagicMock()
        client.grant_self.return_value = {"credential": "grant", "scopes": ["capture.microphone"]}
        client.capture_status.side_effect = [_view(state="stopping", events=[]), _view(events=[])]
        with patch("voxink_cli.cli.time.sleep"):
            result = _wait_for_settled(client, argparse.Namespace(timeout=5))
        self.assertEqual(result["state"], "ready")
        self.assertEqual(client.capture_status.call_args_list, [call(include_events=False), call(include_events=False)])

    def test_wait_preserves_final_events_for_an_authorized_reader(self) -> None:
        for standing in ({"credential": "owner"}, {"credential": "grant", "scopes": ["transcripts.read"]}):
            with self.subTest(standing=standing):
                client = MagicMock()
                client.grant_self.return_value = standing
                client.capture_status.side_effect = [_view(events=[]), _view()]
                result = _wait_for_settled(client, argparse.Namespace(timeout=5))
                self.assertEqual(len(result["events"]), 3)
                self.assertEqual(client.capture_status.call_args_list, [call(include_events=False), call()])

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
