from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from voxink_core.local_models import (
    MODEL_MANIFEST,
    discover_local_models,
    inspect_model_package,
)


def _write_package(root: Path, model_id: str, payload: bytes = b"weights") -> Path:
    path = root / model_id
    path.mkdir()
    (path / "weights.bin").write_bytes(payload)
    (path / MODEL_MANIFEST).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": model_id,
                "display_name": f"Display {model_id}",
                "profile": "Efficient",
                "description": "Runs locally",
                "provider": "qwen-local",
                "files": [
                    {
                        "path": "weights.bin",
                        "size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


class LocalModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_discovers_complete_immediate_child_packages(self) -> None:
        _write_package(self.root, "Qwen3-ASR-0.6B")
        incomplete = _write_package(self.root, "Qwen3-ASR-broken")
        (incomplete / "weights.bin").unlink()
        (self.root / "notes").mkdir()

        packages = discover_local_models(self.root)

        self.assertEqual(["Qwen3-ASR-0.6B"], [item.model_id for item in packages])
        self.assertEqual("Display Qwen3-ASR-0.6B", packages[0].display_name)
        self.assertEqual(len(b"weights"), packages[0].size_bytes)

    def test_checksum_verification_detects_same_size_corruption(self) -> None:
        package = _write_package(self.root, "Qwen3-ASR-0.6B")
        (package / "weights.bin").write_bytes(b"garbage")

        inspection = inspect_model_package(package, verify_checksums=True)

        self.assertIn("checksum does not match", inspection.failures[0])

    def test_manifest_cannot_name_a_different_directory(self) -> None:
        package = _write_package(self.root, "actual")
        manifest = json.loads((package / MODEL_MANIFEST).read_text(encoding="utf-8"))
        manifest["model"] = "other"
        (package / MODEL_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")

        inspection = inspect_model_package(package)

        self.assertIsNone(inspection.package)
        self.assertIn("must match its directory", inspection.failures[0])

    def test_manifest_paths_cannot_escape_the_package(self) -> None:
        package = _write_package(self.root, "Qwen3-ASR-0.6B")
        manifest = json.loads((package / MODEL_MANIFEST).read_text(encoding="utf-8"))
        manifest["files"][0]["path"] = "../weights.bin"
        (package / MODEL_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")

        inspection = inspect_model_package(package)

        self.assertIsNone(inspection.package)
        self.assertIn("remain inside", inspection.failures[0])


if __name__ == "__main__":
    unittest.main()
