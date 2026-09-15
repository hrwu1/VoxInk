from __future__ import annotations

import asyncio
from io import StringIO
import os
from pathlib import Path
import signal
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from clients.windows_capture.cli import build_parser, capture_configuration, main, print_transcript, run, supported_sources
from clients.windows_capture.models import CapturableApplication, CaptureError, InputDevice, SystemAudioBackend


class CommandLineContractTests(unittest.TestCase):
    """The helper builds one command line for every backend."""

    def _config(self, argv: list[str], platform: str = "win32"):
        with patch("clients.windows_capture.cli.sys.platform", platform):
            return capture_configuration(build_parser().parse_args(argv))

    def test_the_helpers_command_line_is_accepted(self) -> None:
        config = self._config([
            "--endpoint", "ws://127.0.0.1:4321/v1/sessions/demo/stream", "--session-id", "demo",
            "--spool-dir", "/tmp/demo", "--retain-audio", "--mic-device", "USB Mic", "--language", "en",
            "--backend", "tap", "--mic", "--app", "Zoom.exe", "--app", "chrome.exe",
        ])
        self.assertEqual("ws://127.0.0.1:4321/v1/sessions/demo/stream", config.service_url)
        self.assertEqual("demo", config.session_id)
        self.assertEqual(Path("/tmp/demo"), config.spool_directory)
        self.assertTrue(config.retain_audio)
        self.assertEqual("USB Mic", config.microphone_device_uid)
        self.assertEqual("en", config.language)
        self.assertIs(SystemAudioBackend.PROCESS_LOOPBACK, config.backend)
        self.assertTrue(config.microphone_enabled)
        self.assertEqual(["chrome.exe", "zoom.exe"], config.selected_applications)

    def test_system_all_and_app_are_exclusive(self) -> None:
        with self.assertRaisesRegex(ValueError, "not both"):
            self._config(["--system-all", "--app", "zoom.exe"])

    def test_at_least_one_source_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "Enable microphone capture"):
            self._config([])

    def test_system_audio_is_refused_off_windows(self) -> None:
        with self.assertRaisesRegex(ValueError, "needs Windows"):
            self._config(["--system-all"], platform="darwin")
        self.assertIsNone(self._config(["--mic"], platform="darwin").system_audio)

    def test_the_credential_comes_from_the_environment(self) -> None:
        with patch.dict(os.environ, {"VOXINK_AUTH_SECRET": "per-launch"}):
            self.assertEqual("per-launch", self._config(["--mic"]).auth_token)

    def test_a_session_id_is_generated_when_the_helper_gives_none(self) -> None:
        self.assertRegex(self._config(["--mic"]).session_id, r"^\d{4}-\d{2}-\d{2}_")

    def test_list_sources_depends_on_the_platform(self) -> None:
        with patch("clients.windows_capture.cli.sys.platform", "win32"):
            self.assertEqual(["microphone", "system"], supported_sources())
        with patch("clients.windows_capture.cli.sys.platform", "darwin"):
            self.assertEqual(["microphone"], supported_sources())
        with patch("sys.stdout", new=StringIO()) as out:
            self.assertEqual(0, main(["--list-sources"]))
        self.assertEqual(supported_sources(), out.getvalue().split())

    def test_list_devices_and_apps_use_the_native_tab_separated_forms(self) -> None:
        devices = [InputDevice("Built-in Microphone", "Built-in Microphone", True), InputDevice("USB Mic #2", "USB Mic", False)]
        with patch("clients.windows_capture.cli.input_devices", return_value=devices), patch("sys.stdout", new=StringIO()) as out:
            self.assertEqual(0, main(["--list-devices"]))
        self.assertEqual(["Built-in Microphone\tBuilt-in Microphone\tdefault", "USB Mic #2\tUSB Mic"], out.getvalue().splitlines())
        applications = [CapturableApplication("Zoom", "zoom.exe", 4242)]
        with patch("clients.windows_capture.cli.running_applications", return_value=applications), patch("sys.stdout", new=StringIO()) as out:
            self.assertEqual(0, main(["--list-apps"]))
        self.assertEqual(["zoom.exe\tZoom\tpid=4242"], out.getvalue().splitlines())

    def test_a_refused_command_line_exits_two_with_the_reason_on_stderr(self) -> None:
        with patch("sys.stderr", new=StringIO()) as err:
            self.assertEqual(2, main(["--system-all", "--app", "x.exe"]))
        self.assertIn("not both", err.getvalue())

    def test_a_capture_failure_exits_one(self) -> None:
        with (
            patch("clients.windows_capture.cli.running_applications", side_effect=CaptureError.process_loopback_unavailable("no")),
            patch("sys.stderr", new=StringIO()) as err,
        ):
            self.assertEqual(1, main(["--list-apps"]))
        self.assertIn("process loopback", err.getvalue())

    def test_finals_print_with_the_channel_name_and_translation(self) -> None:
        with patch("sys.stdout", new=StringIO()) as out:
            print_transcript({"is_final": False, "text": "partial", "source": "microphone"})
            print_transcript({"is_final": True, "text": "hello", "source": "microphone"})
            print_transcript({"is_final": True, "text": "hi", "source": "system", "channel_name": "Zoom", "translation": "你好"})
        self.assertEqual(["[MICROPHONE] hello", "[Zoom] hi", "    你好"], out.getvalue().splitlines())


class ControlContractTests(unittest.TestCase):
    """SIGUSR1 pauses, SIGUSR2 resumes, SIGINT stops; stdin lines do the same."""

    def coordinator(self) -> MagicMock:
        coordinator = MagicMock()
        coordinator.start = AsyncMock()
        coordinator.pause = AsyncMock()
        coordinator.resume = AsyncMock()
        coordinator.stop = AsyncMock()
        return coordinator

    @unittest.skipIf(sys.platform == "win32", "POSIX signals")
    def test_signals_drive_pause_resume_and_stop(self) -> None:
        coordinator = self.coordinator()

        async def scenario() -> int:
            task = asyncio.ensure_future(run(coordinator, stdin=None, session_id="demo"))
            await asyncio.sleep(0.05)
            signal.raise_signal(signal.SIGUSR1)
            await asyncio.sleep(0.05)
            signal.raise_signal(signal.SIGUSR2)
            await asyncio.sleep(0.05)
            signal.raise_signal(signal.SIGINT)
            return await asyncio.wait_for(task, timeout=2)

        with patch("builtins.print"):
            self.assertEqual(0, asyncio.run(scenario()))
        coordinator.start.assert_awaited_once()
        coordinator.pause.assert_awaited_once()
        coordinator.resume.assert_awaited_once()
        coordinator.stop.assert_awaited_once()

    def test_stdin_lines_drive_pause_resume_and_stop(self) -> None:
        coordinator = self.coordinator()
        read_end, write_end = os.pipe()
        reader = os.fdopen(read_end, "r")
        writer = os.fdopen(write_end, "w")

        async def scenario() -> int:
            task = asyncio.ensure_future(run(coordinator, stdin=reader, session_id="demo"))
            await asyncio.sleep(0.05)
            writer.write("PAUSE\n")
            writer.flush()
            await asyncio.sleep(0.05)
            writer.write("nonsense\nresume\n")
            writer.flush()
            await asyncio.sleep(0.05)
            writer.write("stop\n")
            writer.flush()
            return await asyncio.wait_for(task, timeout=2)

        try:
            with patch("builtins.print"):
                self.assertEqual(0, asyncio.run(scenario()))
        finally:
            writer.close()
            reader.close()
        coordinator.pause.assert_awaited_once()
        coordinator.resume.assert_awaited_once()
        coordinator.stop.assert_awaited_once()

    def test_a_closed_stdin_is_not_a_stop(self) -> None:
        coordinator = self.coordinator()
        read_end, write_end = os.pipe()
        os.close(write_end)
        reader = os.fdopen(read_end, "r")

        async def scenario() -> bool:
            task = asyncio.ensure_future(run(coordinator, stdin=reader, session_id="demo"))
            await asyncio.sleep(0.1)
            still_running = not task.done()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return still_running

        try:
            with patch("builtins.print"):
                self.assertTrue(asyncio.run(scenario()))
        finally:
            reader.close()
        coordinator.stop.assert_awaited_once()

    def test_a_start_failure_exits_one_and_still_releases(self) -> None:
        coordinator = self.coordinator()
        coordinator.start = AsyncMock(side_effect=CaptureError.microphone_unavailable("busy"))
        with patch("sys.stderr", new=StringIO()) as err, patch("builtins.print", side_effect=lambda *a, **k: None):
            code = asyncio.run(run(coordinator, stdin=None, session_id="demo"))
        self.assertEqual(1, code)
        coordinator.stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
