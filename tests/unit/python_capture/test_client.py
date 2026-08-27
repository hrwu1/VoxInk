from __future__ import annotations

import unittest

from clients.python_capture.client import CaptureConfig, MicrophoneCaptureClient


def _start_message(**overrides) -> dict:
    config = CaptureConfig(
        websocket_url="ws://127.0.0.1:8765/v1/sessions/demo/stream",
        session_id="demo",
        **overrides,
    )
    return MicrophoneCaptureClient(config)._start_message()


class StartMessageTests(unittest.TestCase):
    def test_retention_is_declared_and_off_by_default(self) -> None:
        message = _start_message()
        self.assertEqual(
            {"audio": False, "separate": True, "combined": False}, message["retention"]
        )

    def test_retention_is_requested_when_the_user_asks_for_it(self) -> None:
        self.assertTrue(_start_message(retain_audio=True)["retention"]["audio"])

    def test_language_stays_absent_for_automatic_detection(self) -> None:
        self.assertNotIn("language", _start_message())
        self.assertEqual("zh", _start_message(language="zh")["language"])


if __name__ == "__main__":
    unittest.main()
