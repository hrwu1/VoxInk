from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from voxink_dev.cli import _run_web_ui


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
        self.assertNotIn("persistent-service-secret", url)
        diagnostics.emit.assert_called_once_with("ui.opened", component="ui", port=43210)


if __name__ == "__main__":
    unittest.main()
