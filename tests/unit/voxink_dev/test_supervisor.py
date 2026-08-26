from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from voxink_dev.supervisor import HelperConfig, HelperSupervisor


class SupervisorIntegrationTests(unittest.TestCase):
    def test_starts_on_private_port_negotiates_protocol_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = HelperSupervisor(
                HelperConfig(
                    model_path=Path(temporary) / "unused-model",
                    output_root=Path(temporary) / "output",
                    readiness_timeout=10,
                )
            )
            health = supervisor.start()
            try:
                self.assertEqual(1, health["protocol_version"])
                self.assertIsNotNone(supervisor.process)
                self.assertIsNone(supervisor.process.poll())
                self.assertNotIn(supervisor.auth_secret, " ".join(supervisor.process.args))
            finally:
                process = supervisor.process
                supervisor.stop()
            self.assertIsNotNone(process)
            self.assertIsNotNone(process.returncode)


if __name__ == "__main__":
    unittest.main()
