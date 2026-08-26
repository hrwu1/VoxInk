from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from voxink_dev.preflight import MODEL_MANIFEST, PreflightError, PreflightOptions, run_preflight


class PreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.model = self.root / "model"
        self.model.mkdir()
        payload = b"offline-model"
        (self.model / "weights.bin").write_bytes(payload)
        (self.model / MODEL_MANIFEST).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "files": [
                        {
                            "path": "weights.bin",
                            "size": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                    ],
                }
            )
        )
        self.options = PreflightOptions(
            model_path=self.model,
            output_root=self.root / "output",
            state_root=self.root / "state",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @patch("voxink_dev.preflight.sys.version_info", (3, 12, 0))
    def test_valid_checkout_passes(self) -> None:
        report = run_preflight(self.options, import_available=lambda _: True)
        self.assertEqual(1, report.checked_files)
        self.assertTrue(report.output_root.is_dir())
        self.assertTrue(report.log_root.is_dir())

    @patch("voxink_dev.preflight.sys.version_info", (3, 12, 0))
    def test_reports_missing_dependency_and_corrupt_model_together(self) -> None:
        (self.model / "weights.bin").write_bytes(b"broken")
        with self.assertRaises(PreflightError) as raised:
            run_preflight(self.options, import_available=lambda name: name != "sounddevice")
        message = str(raised.exception)
        self.assertIn("sounddevice", message)
        self.assertIn("wrong size", message)

    @patch("voxink_dev.preflight.sys.version_info", (3, 12, 0))
    def test_system_audio_requires_native_executable(self) -> None:
        options = PreflightOptions(
            model_path=self.model,
            output_root=self.root / "output",
            state_root=self.root / "state",
            sources=("system",),
        )
        with self.assertRaisesRegex(PreflightError, "native capture helper"):
            run_preflight(options, import_available=lambda _: True)


if __name__ == "__main__":
    unittest.main()
