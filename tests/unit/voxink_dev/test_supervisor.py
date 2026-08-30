from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from voxink_dev.diagnostics import DiagnosticLog
from voxink_dev.supervisor import (
    HelperConfig,
    HelperSupervisor,
    SupervisorError,
    _preferred_loopback_port,
)


class PortSelectionTests(unittest.TestCase):
    @staticmethod
    def _listener(port: int, *, bind_error: OSError | None = None) -> MagicMock:
        listener = MagicMock()
        listener.__enter__.return_value = listener
        listener.bind.side_effect = bind_error
        listener.getsockname.return_value = ("127.0.0.1", port)
        return listener

    def test_prefers_port_8765_when_available(self) -> None:
        listener = self._listener(8765)
        with patch("voxink_dev.supervisor.socket.socket", return_value=listener):
            self.assertEqual(8765, _preferred_loopback_port())
        listener.bind.assert_called_once_with(("127.0.0.1", 8765))

    def test_uses_an_os_assigned_port_when_8765_is_unavailable(self) -> None:
        preferred = self._listener(8765, bind_error=OSError("address in use"))
        fallback = self._listener(54321)
        with patch(
            "voxink_dev.supervisor.socket.socket",
            side_effect=(preferred, fallback),
        ):
            self.assertEqual(54321, _preferred_loopback_port())
        preferred.bind.assert_called_once_with(("127.0.0.1", 8765))
        fallback.bind.assert_called_once_with(("127.0.0.1", 0))


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

    def test_passes_a_configured_vocabulary_file_to_the_helper_command(self) -> None:
        process = MagicMock()
        process.pid = 123
        process.poll.return_value = 0
        factory = MagicMock(return_value=process)
        vocabulary = Path("/tmp/terms.txt")
        supervisor = HelperSupervisor(
            HelperConfig(
                model_path=Path("/tmp/model"),
                output_root=Path("/tmp/output"),
                vocabulary_path=vocabulary,
            ),
            process_factory=factory,
        )
        with patch.object(supervisor, "wait_until_ready", return_value={"protocol_version": 1}):
            supervisor.start()
        command = factory.call_args.args[0]
        self.assertIn("--vocabulary", command)
        self.assertEqual(str(vocabulary.resolve()), command[command.index("--vocabulary") + 1])

    def test_omits_the_vocabulary_flag_when_none_is_configured(self) -> None:
        process = MagicMock()
        process.pid = 123
        process.poll.return_value = 0
        factory = MagicMock(return_value=process)
        supervisor = HelperSupervisor(
            HelperConfig(model_path=Path("/tmp/model"), output_root=Path("/tmp/output")),
            process_factory=factory,
        )
        with patch.object(supervisor, "wait_until_ready", return_value={"protocol_version": 1}):
            supervisor.start()
        self.assertNotIn("--vocabulary", factory.call_args.args[0])

    def _supervisor_for(self, process: subprocess.Popen[str]) -> HelperSupervisor:
        return HelperSupervisor(
            HelperConfig(
                model_path=Path("/tmp/model"),
                output_root=Path("/tmp/output"),
                readiness_timeout=5,
            ),
            process_factory=MagicMock(return_value=process),
        )

    def test_drains_helper_stderr_so_a_flood_cannot_block_the_helper(self) -> None:
        """uvicorn logs every unhandled traceback to stderr; an undrained pipe wedges it."""
        script = (
            "import sys\n"
            "for index in range(20000):\n"
            "    sys.stderr.write(f'traceback line {index}\\n')\n"
            "sys.stderr.flush()\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        supervisor = self._supervisor_for(process)
        with patch.object(supervisor, "wait_until_ready", return_value={"protocol_version": 1}):
            supervisor.start()
        try:
            # ~400 KB of stderr far exceeds the pipe buffer; undrained, the child
            # blocks on write and never exits.
            process.wait(timeout=15)
        finally:
            supervisor.stop()
        self.assertEqual(0, process.returncode)
        self.assertIn("traceback line 19999", supervisor._stderr_detail())

    def test_startup_failure_still_reports_the_helper_stderr_detail(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stderr.write('model missing\\n'); sys.exit(3)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        process.wait(timeout=10)
        supervisor = self._supervisor_for(process)
        with self.assertRaises(SupervisorError) as caught:
            supervisor.start()
        self.assertIn("status 3", str(caught.exception))
        self.assertIn("model missing", str(caught.exception))

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
