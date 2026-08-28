from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from voxink_dev.diagnostics import DiagnosticLog
from voxink_dev.supervisor import HelperConfig, HelperSupervisor


class SupervisorIntegrationTests(unittest.TestCase):
    def test_passes_configured_native_capture_path_only_in_the_helper_environment(self) -> None:
        process = MagicMock()
        process.pid = 123
        process.poll.return_value = 0
        executable = Path("/tmp/VoxInkCaptureDev.app/Contents/MacOS/voxink-capture")
        factory = MagicMock(return_value=process)
        supervisor = HelperSupervisor(
            HelperConfig(
                model_path=Path("/tmp/model"),
                output_root=Path("/tmp/output"),
                native_capture_path=executable,
            ),
            process_factory=factory,
        )
        with patch.object(supervisor, "wait_until_ready", return_value={"protocol_version": 1}):
            supervisor.start()
        environment = factory.call_args.kwargs["env"]
        self.assertEqual(str(executable.resolve()), environment["VOXINK_NATIVE_CAPTURE"])
        self.assertNotIn(str(executable), factory.call_args.args[0])

    def test_starts_on_private_port_negotiates_protocol_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            diagnostics = DiagnosticLog(Path(temporary) / "logs")
            supervisor = HelperSupervisor(
                HelperConfig(
                    model_path=Path(temporary) / "unused-model",
                    output_root=Path(temporary) / "output",
                    readiness_timeout=10,
                ),
                diagnostics=diagnostics,
            )
            health = supervisor.start()
            try:
                self.assertEqual(1, health["protocol_version"])
                self.assertIsNotNone(supervisor.process)
                self.assertIsNone(supervisor.process.poll())
                self.assertNotIn(supervisor.auth_secret, " ".join(supervisor.process.args))
                self.assertNotIn(supervisor.ui_bootstrap_secret, " ".join(supervisor.process.args))
            finally:
                process = supervisor.process
                supervisor.stop()
                diagnostics.close()
            self.assertIsNotNone(process)
            self.assertIsNotNone(process.returncode)
            content = diagnostics.path.read_text()
            events = [json.loads(line)["event"] for line in content.splitlines()]
            self.assertEqual(
                ["helper.starting", "helper.ready", "helper.stopping", "helper.stopped"],
                events,
            )
            self.assertNotIn(supervisor.auth_secret, content)


if __name__ == "__main__":
    unittest.main()
