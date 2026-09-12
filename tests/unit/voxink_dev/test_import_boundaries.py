"""Fresh-process checks for the runtime/developer dependency boundaries."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest


class ImportBoundaryTests(unittest.TestCase):
    def assert_isolated_import(self, module: str, forbidden: tuple[str, ...]) -> None:
        program = f"""
import importlib
import importlib.abc
import sys

class RejectImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in {forbidden!r}):
            raise AssertionError('Unexpected runtime dependency: ' + fullname)

sys.meta_path.insert(0, RejectImports())
importlib.import_module({module!r})
"""
        result = subprocess.run(
            [sys.executable, "-c", program], cwd=Path(__file__).resolve().parents[3],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_supervisor_does_not_import_the_service_application_or_model_stack(self) -> None:
        self.assert_isolated_import(
            "voxink_dev.supervisor", ("services.transcription", "fastapi", "numpy", "torch", "qwen_asr"),
        )

    def test_service_protocol_does_not_import_runtime_or_application_modules(self) -> None:
        self.assert_isolated_import(
            "services.protocol", ("services.transcription", "voxink_dev", "voxink_core", "fastapi"),
        )


if __name__ == "__main__":
    unittest.main()
