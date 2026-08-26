from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from voxink_dev.diagnostics import DiagnosticLog


class DiagnosticLogTests(unittest.TestCase):
    def test_log_is_private_structured_and_allowlisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with DiagnosticLog(Path(temporary) / "logs") as diagnostics:
                diagnostics.emit(
                    "helper.ready",
                    component="helper",
                    pid=123,
                    port=4567,
                    protocol_version=1,
                )
                path = diagnostics.path
            record = json.loads(path.read_text())
            self.assertEqual("helper.ready", record["event"])
            self.assertIn("timestamp", record)
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_sensitive_or_arbitrary_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with DiagnosticLog(Path(temporary)) as diagnostics:
                for name in ("auth_secret", "transcript", "audio", "path"):
                    with self.subTest(name=name), self.assertRaises(ValueError):
                        diagnostics.emit("unsafe", **{name: "private"})


if __name__ == "__main__":
    unittest.main()
