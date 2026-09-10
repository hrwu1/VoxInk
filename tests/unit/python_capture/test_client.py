from __future__ import annotations

import asyncio
from io import StringIO
import signal
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from clients.python_capture.cli import build_parser, capture_config, main, run
from clients.python_capture.client import CaptureConfig, MicrophoneCaptureClient


def _start_message(**overrides) -> dict:
    config = CaptureConfig(
        websocket_url="ws://127.0.0.1:8300/v1/sessions/demo/stream",
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

    def test_frames_are_sent_and_chunking_is_left_to_the_helper(self) -> None:
        message = _start_message()
        self.assertEqual("frames", message["stream_mode"])
        self.assertEqual({}, message["vad"])

    def test_only_the_microphone_source_is_accepted(self) -> None:
        with self.assertRaisesRegex(ValueError, "microphone only"):
            CaptureConfig(websocket_url="ws://x", session_id="demo", source="system")


class CommandLineContractTests(unittest.TestCase):
    """The helper builds one command line for either backend."""

    def _config(self, argv: list[str]) -> CaptureConfig:
        return capture_config(build_parser().parse_args(argv))

    def test_the_helpers_command_line_is_accepted(self) -> None:
        config = self._config(
            [
                "--endpoint", "ws://127.0.0.1:4321/v1/sessions/demo/stream",
                "--session-id", "demo", "--retain-audio",
                "--mic-device", "3", "--language", "en", "--backend", "auto", "--mic",
            ]
        )
        self.assertEqual("ws://127.0.0.1:4321/v1/sessions/demo/stream", config.websocket_url)
        self.assertEqual("demo", config.session_id)
        self.assertTrue(config.retain_audio)
        self.assertEqual(3, config.device)
        self.assertEqual("en", config.language)

    def test_the_credential_comes_from_the_environment_not_the_command_line(self) -> None:
        with patch.dict("os.environ", {"VOXINK_AUTH_SECRET": "per-launch"}):
            self.assertEqual("per-launch", self._config(["--mic"]).auth_token)

    def test_system_sources_are_refused_with_the_native_helper_named(self) -> None:
        for argv in (["--system-all"], ["--mic", "--app", "us.zoom.xos"]):
            with self.assertRaisesRegex(ValueError, "build-macos-capture"):
                self._config(argv)

    def test_list_sources_reports_the_microphone_only(self) -> None:
        with patch("sys.stdout", new=StringIO()) as out:
            self.assertEqual(0, main(["--list-sources"]))
        self.assertEqual(["microphone"], out.getvalue().split())

    def test_list_apps_fails_like_a_system_source(self) -> None:
        with patch("sys.stderr", new=StringIO()) as err:
            self.assertEqual(2, main(["--list-apps"]))
        self.assertIn("native macOS", err.getvalue())

    def test_list_devices_uses_the_native_tab_separated_form(self) -> None:
        devices = [("0", "Built-in Microphone", True), ("2", "USB Mic", False)]
        with (
            patch("clients.python_capture.cli.list_input_devices", return_value=devices),
            patch("sys.stdout", new=StringIO()) as out,
        ):
            self.assertEqual(0, main(["--list-devices"]))
        self.assertEqual(
            ["0\tBuilt-in Microphone\tdefault", "2\tUSB Mic"], out.getvalue().splitlines()
        )

    def test_a_refused_command_line_exits_two_with_the_reason_on_stderr(self) -> None:
        with patch("sys.stderr", new=StringIO()) as err:
            self.assertEqual(2, main(["--system-all"]))
        self.assertIn("native macOS capture helper", err.getvalue())


class SignalContractTests(unittest.TestCase):
    """SIGUSR1 pauses, SIGUSR2 resumes, SIGINT stops: what the helper sends."""

    def test_signals_drive_pause_resume_and_stop(self) -> None:
        client = MagicMock()
        client.config.session_id = "demo"
        client.start = AsyncMock()
        client.pause = AsyncMock()
        client.resume = AsyncMock()
        client.stop = AsyncMock()

        async def scenario() -> int:
            task = asyncio.ensure_future(run(client))
            await asyncio.sleep(0.05)
            signal.raise_signal(signal.SIGUSR1)
            await asyncio.sleep(0.05)
            signal.raise_signal(signal.SIGUSR2)
            await asyncio.sleep(0.05)
            signal.raise_signal(signal.SIGINT)
            return await asyncio.wait_for(task, timeout=2)

        with patch("builtins.print"):
            code = asyncio.run(scenario())
        self.assertEqual(0, code)
        client.start.assert_awaited_once()
        client.pause.assert_awaited_once()
        client.resume.assert_awaited_once()
        client.stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
