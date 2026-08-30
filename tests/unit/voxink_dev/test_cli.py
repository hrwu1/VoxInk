from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from voxink_dev.cli import (
    _build_capture_plan,
    _native_capture_command,
    _run_web_ui,
    build_parser,
)


class CliParserTests(unittest.TestCase):
    def test_browser_interface_is_the_default(self) -> None:
        args = build_parser().parse_args([])
        self.assertTrue(args.web)

    def test_terminal_interface_can_be_selected_explicitly(self) -> None:
        args = build_parser().parse_args(["--terminal"])
        self.assertFalse(args.web)

    def test_audio_retention_is_enabled_by_default_with_an_opt_out(self) -> None:
        self.assertTrue(build_parser().parse_args([]).retain_audio)
        self.assertFalse(
            build_parser().parse_args(["--no-retain-audio"]).retain_audio
        )

    def test_sources_can_be_repeated_for_combined_capture(self) -> None:
        args = build_parser().parse_args(
            ["--source", "microphone", "--source", "system"]
        )
        plan = _build_capture_plan(args.sources, args.applications)
        self.assertEqual(("microphone", "system"), plan.modes)
        self.assertEqual(("microphone", "system"), plan.session_sources)
        self.assertTrue(plan.uses_native_capture)

    def test_microphone_is_the_default_capture_plan(self) -> None:
        plan = _build_capture_plan(None, [])
        self.assertEqual(("microphone",), plan.modes)
        self.assertFalse(plan.uses_native_capture)

    def test_selected_applications_require_bundle_identifiers(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one --app"):
            _build_capture_plan(["applications"], [])

    def test_application_ids_require_application_capture(self) -> None:
        with self.assertRaisesRegex(ValueError, "--app requires"):
            _build_capture_plan(["microphone"], ["com.apple.Safari"])

    def test_all_system_and_selected_applications_are_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(ValueError, "not both"):
            _build_capture_plan(
                ["system", "applications"], ["com.apple.Safari"]
            )

    def test_native_command_carries_endpoint_sources_and_applications(self) -> None:
        plan = _build_capture_plan(
            ["microphone", "applications"],
            ["com.apple.Safari", "us.zoom.xos"],
        )
        command = _native_capture_command(
            Path("/tmp/voxink-capture"),
            "ws://127.0.0.1:4321/v1/sessions/demo/stream",
            "demo",
            plan,
        )
        self.assertEqual(
            [
                str(Path("/tmp/voxink-capture").resolve()),
                "--endpoint",
                "ws://127.0.0.1:4321/v1/sessions/demo/stream",
                "--session-id",
                "demo",
                "--retain-audio",
                "--mic",
                "--app",
                "com.apple.Safari",
                "--app",
                "us.zoom.xos",
            ],
            command,
        )

    def test_native_command_forwards_audio_retention(self) -> None:
        plan = _build_capture_plan(["system"], [])
        endpoint = "ws://127.0.0.1:4321/v1/sessions/demo/stream"
        self.assertIn(
            "--retain-audio",
            _native_capture_command(Path("/tmp/voxink-capture"), endpoint, "demo", plan),
        )
        self.assertNotIn(
            "--retain-audio",
            _native_capture_command(
                Path("/tmp/voxink-capture"), endpoint, "demo", plan, False
            ),
        )

    def test_all_system_audio_maps_to_native_system_flag(self) -> None:
        plan = _build_capture_plan(["system"], [])
        command = _native_capture_command(
            Path("/tmp/voxink-capture"),
            "ws://127.0.0.1:4321/v1/sessions/demo/stream",
            "demo",
            plan,
        )
        self.assertIn("--system-all", command)
        self.assertNotIn("--mic", command)


class WebUiLauncherTests(unittest.TestCase):
    def test_opens_one_time_bootstrap_without_exposing_service_credential(self) -> None:
        process = MagicMock()
        process.poll.return_value = None
        supervisor = MagicMock()
        supervisor.ui_bootstrap_secret = "temporary-bootstrap"
        supervisor.auth_secret = "persistent-service-secret"
        supervisor.base_url = "http://127.0.0.1:43210"
        supervisor.port = 43210
        supervisor.process = process
        diagnostics = MagicMock()

        with (
            patch("voxink_dev.cli.webbrowser.open", return_value=True) as opened,
            patch("voxink_dev.cli.time.sleep", side_effect=KeyboardInterrupt),
            patch("builtins.print"),
        ):
            result = _run_web_ui(
                supervisor,
                {"ui_available": True},
                diagnostics,
                Path("/tmp/voxink-test-output"),
            )

        self.assertEqual(0, result)
        url = opened.call_args.args[0]
        self.assertIn("bootstrap=temporary-bootstrap", url)
        self.assertIn("retain_audio=1", url)
        self.assertNotIn("persistent-service-secret", url)
        diagnostics.emit.assert_called_once_with("ui.opened", component="ui", port=43210)

    def test_audio_retention_reaches_the_browser_recorder(self) -> None:
        process = MagicMock()
        process.poll.return_value = None
        supervisor = MagicMock()
        supervisor.ui_bootstrap_secret = "temporary-bootstrap"
        supervisor.base_url = "http://127.0.0.1:43210"
        supervisor.port = 43210
        supervisor.process = process

        with (
            patch("voxink_dev.cli.webbrowser.open", return_value=True) as opened,
            patch("voxink_dev.cli.time.sleep", side_effect=KeyboardInterrupt),
            patch("builtins.print"),
        ):
            _run_web_ui(
                supervisor,
                {"ui_available": True},
                MagicMock(),
                Path("/tmp/voxink-test-output"),
                False,
            )

        self.assertIn("retain_audio=0", opened.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
