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

    def test_service_does_not_import_developer_setup_or_optional_model_dependencies(self) -> None:
        self.assert_isolated_import(
            "services.transcription.app", ("voxink_dev", "torch", "qwen_asr", "diart", "pyannote"),
        )

    def test_diart_runtime_and_worker_are_independent_of_developer_and_model_imports(self) -> None:
        for name in ("diart_runtime", "diart_live_worker", "diart_turns"):
            with self.subTest(module=name):
                self.assert_isolated_import(
                    f"voxink_core.diarization.{name}",
                    ("voxink_dev", "services", "numpy", "torch", "diart", "pyannote"),
                )

    def test_legacy_developer_imports_reuse_the_core_implementation(self) -> None:
        from voxink_core.diarization import diart_live_worker, diart_runtime, diart_turns
        from voxink_dev import diart_live_worker as old_worker
        from voxink_dev import diart_runtime as old_runtime
        from voxink_dev import diart_turns as old_turns

        self.assertIs(old_worker.StreamingTurns, diart_live_worker.StreamingTurns)
        self.assertIs(old_worker.serve, diart_live_worker.serve)
        self.assertIs(old_turns.Span, diart_turns.Span)
        for name in ("validate_runtime", "runtime_environment", "model_paths", "record_action"):
            self.assertIs(getattr(old_runtime, name), getattr(diart_runtime, name))
        self.assertEqual(old_runtime.DEFAULT_RUNTIME, diart_runtime.DEFAULT_RUNTIME)

    def test_core_and_legacy_worker_commands_expose_the_same_options(self) -> None:
        for module in ("voxink_core.diarization.diart_live_worker", "voxink_dev.diart_live_worker"):
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-m", module, "--help"],
                    cwd=Path(__file__).resolve().parents[3], capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--runtime", result.stdout)
                self.assertIn("--device", result.stdout)


if __name__ == "__main__":
    unittest.main()
