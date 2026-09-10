from __future__ import annotations

from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from voxink_dev.cli import (
    _build_capture_plan,
    _built_menubar_app,
    _capture_backend,
    _resolve_interface,
    _run_web_ui,
    build_parser,
    main,
)


class CliParserTests(unittest.TestCase):
    def _interface(self, argv: list[str]) -> str:
        return _resolve_interface(build_parser().parse_args(argv))

    def test_menu_bar_interface_is_the_default(self) -> None:
        self.assertEqual("menubar", self._interface([]))

    def test_each_interface_can_be_selected_explicitly(self) -> None:
        self.assertEqual("web", self._interface(["--web"]))
        self.assertEqual("terminal", self._interface(["--terminal"]))
        self.assertEqual("menubar", self._interface(["--menubar"]))

    def test_named_sources_select_the_terminal_which_honours_them(self) -> None:
        self.assertEqual("terminal", self._interface(["--source", "microphone"]))
        self.assertEqual("terminal", self._interface(["--source", "system"]))
        self.assertEqual(
            "terminal",
            self._interface(
                ["--source", "applications", "--app", "com.apple.Safari"]
            ),
        )

    def test_an_interface_flag_wins_over_named_sources(self) -> None:
        self.assertEqual(
            "menubar", self._interface(["--menubar", "--source", "system"])
        )

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

    def test_microphone_is_the_default_capture_plan(self) -> None:
        plan = _build_capture_plan(None, [])
        self.assertEqual(("microphone",), plan.modes)

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
        parts = urlsplit(url)
        self.assertEqual("/ui/", parts.path)
        self.assertEqual({"launch"}, set(parse_qs(parts.query)))
        self.assertEqual(
            {"bootstrap": ["temporary-bootstrap"], "retain_audio": ["1"]},
            parse_qs(parts.fragment),
        )
        self.assertNotIn("temporary-bootstrap", parts.query)
        self.assertNotIn("persistent-service-secret", url)
        diagnostics.emit.assert_called_once_with("ui.opened", component="ui", port=43210)

    def test_each_launch_bypasses_previously_cached_html(self) -> None:
        supervisor = MagicMock()
        supervisor.ui_bootstrap_secret = "temporary-bootstrap"
        supervisor.base_url = "http://127.0.0.1:43210"
        supervisor.port = 43210
        supervisor.process.poll.return_value = None

        with (
            patch("voxink_dev.cli.webbrowser.open", return_value=True) as opened,
            patch("voxink_dev.cli.time.time_ns", side_effect=[100, 101]),
            patch("voxink_dev.cli.time.sleep", side_effect=KeyboardInterrupt),
            patch("builtins.print"),
        ):
            for _ in range(2):
                _run_web_ui(
                    supervisor, {"ui_available": True}, MagicMock(),
                    Path("/tmp/voxink-test-output"),
                )

        first, second = (urlsplit(call.args[0]) for call in opened.call_args_list)
        self.assertNotEqual(first.query, second.query)
        self.assertEqual(first.fragment, second.fragment)

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


class MenuBarLauncherTests(unittest.TestCase):
    def test_only_a_built_application_is_reported(self) -> None:
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "voxink-menubar"
            self.assertIsNone(_built_menubar_app(missing))
            missing.write_bytes(b"")
            self.assertIsNone(_built_menubar_app(missing))
            missing.chmod(0o755)
            self.assertEqual(missing.resolve(), _built_menubar_app(missing))

    def _main(self, argv: list[str], menubar_app: Path):
        with (
            patch("voxink_dev.cli.run_preflight") as preflight,
            patch("voxink_dev.cli.DiagnosticLog"),
            patch("voxink_dev.cli.HelperSupervisor") as supervisor,
            patch("voxink_dev.cli._run_menubar", return_value=0) as menubar,
            patch("voxink_dev.cli._run_web_ui", return_value=0) as web,
            patch("voxink_dev.cli._run_terminal", return_value=0) as terminal,
            patch("builtins.print"),
        ):
            preflight.return_value = MagicMock(checked_files=1, free_bytes=0)
            supervisor.return_value.start.return_value = {
                "protocol_version": "1.0",
                "provider": "local",
            }
            code = main([*argv, "--menubar-app", str(menubar_app)])
        self.terminal = terminal
        self.preflight_options = preflight.call_args.args[0]
        self.helper_config = supervisor.call_args.args[0]
        return code, menubar, web

    def test_the_default_runs_the_menu_bar_application(self) -> None:
        with TemporaryDirectory() as directory:
            application = Path(directory) / "voxink-menubar"
            application.write_bytes(b"")
            application.chmod(0o755)
            code, menubar, web = self._main([], application)
        self.assertEqual(0, code)
        menubar.assert_called_once()
        web.assert_not_called()

    def test_an_unbuilt_application_falls_back_to_the_browser(self) -> None:
        with TemporaryDirectory() as directory:
            code, menubar, web = self._main([], Path(directory) / "voxink-menubar")
        self.assertEqual(0, code)
        web.assert_called_once()
        menubar.assert_not_called()

    def test_an_explicit_request_reports_the_missing_build(self) -> None:
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "voxink-menubar"
            with patch("sys.stderr", new=StringIO()) as errors:
                code = main(["--menubar", "--menubar-app", str(missing)])
        self.assertEqual(2, code)
        self.assertIn("build-macos-menubar", errors.getvalue())


class CaptureBackendTests(unittest.TestCase):
    def test_a_built_swift_helper_is_preferred(self) -> None:
        with TemporaryDirectory() as directory:
            native = Path(directory) / "voxink-capture"
            native.write_bytes(b"")
            native.chmod(0o755)
            self.assertEqual((native.resolve(), False), _capture_backend(native))

    def test_the_portable_python_backend_stands_in_for_a_missing_build(self) -> None:
        with TemporaryDirectory() as directory:
            backend, portable = _capture_backend(Path(directory) / "voxink-capture")
        self.assertTrue(portable)
        self.assertEqual("voxink-mic-capture", backend.name)
        self.assertTrue(backend.is_file(), backend)

    def _main(self, argv: list[str], native: Path):
        with (
            patch("voxink_dev.cli.run_preflight") as preflight,
            patch("voxink_dev.cli.DiagnosticLog"),
            patch("voxink_dev.cli.HelperSupervisor") as supervisor,
            patch("voxink_dev.cli._run_menubar", return_value=0),
            patch("voxink_dev.cli._run_web_ui", return_value=0),
            patch("voxink_dev.cli._run_terminal", return_value=0) as terminal,
            patch("builtins.print") as printed,
        ):
            preflight.return_value = MagicMock(checked_files=1, free_bytes=0)
            supervisor.return_value.start.return_value = {
                "protocol_version": "1.0",
                "provider": "local",
            }
            code = main([*argv, "--native-capture", str(native)])
        return code, preflight.call_args.args[0], supervisor.call_args.args[0], terminal, printed

    def test_the_terminal_interface_drives_the_helper_which_launches_the_backend(self) -> None:
        with TemporaryDirectory() as directory:
            native = Path(directory) / "voxink-capture"
            native.write_bytes(b"")
            native.chmod(0o755)
            code, preflight, helper, terminal, printed = self._main(["--terminal"], native)
        self.assertEqual(0, code)
        terminal.assert_called_once()
        self.assertEqual(native.resolve(), helper.native_capture_path)
        self.assertEqual(native.resolve(), preflight.native_capture)
        self.assertFalse(preflight.python_capture)
        plan = terminal.call_args.args[3]
        self.assertEqual(("microphone",), plan.session_sources)
        self.assertFalse(any("portable" in str(call) for call in printed.call_args_list))

    def test_without_the_swift_build_the_helper_gets_the_python_backend(self) -> None:
        with TemporaryDirectory() as directory:
            code, preflight, helper, terminal, printed = self._main(
                ["--terminal"], Path(directory) / "voxink-capture"
            )
        self.assertEqual(0, code)
        self.assertEqual("voxink-mic-capture", helper.native_capture_path.name)
        self.assertTrue(preflight.python_capture)
        self.assertTrue(any("portable Python backend" in str(call) for call in printed.call_args_list))

    def test_named_sources_reach_the_terminal_loop(self) -> None:
        with TemporaryDirectory() as directory:
            native = Path(directory) / "voxink-capture"
            native.write_bytes(b"")
            native.chmod(0o755)
            _, _, _, terminal, _ = self._main(
                ["--source", "microphone", "--source", "applications", "--app", "us.zoom.xos"],
                native,
            )
        plan = terminal.call_args.args[3]
        self.assertEqual(("microphone", "system"), plan.session_sources)
        self.assertEqual(("us.zoom.xos",), plan.applications)


if __name__ == "__main__":
    unittest.main()
