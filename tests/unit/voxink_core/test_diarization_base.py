from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from voxink_core.local_models import MODEL_MANIFEST
from voxink_core.diarization.base import DiarizationProvider, require_local_model
from voxink_core.diarization.models import (
    DiarizationError,
    DiarizationErrorCode,
    DiarizationRequest,
    DiarizationResult,
)


def _write_package(root: Path, *, weights: bytes = b"weights", **overrides) -> Path:
    model_id = "pyannote-speaker-diarization-community-1"
    path = root / model_id
    path.mkdir()
    (path / "config.yaml").write_bytes(weights)
    manifest = {
        "schema_version": 1,
        "model": model_id,
        "display_name": "pyannote Community-1",
        "profile": "Diarization",
        "description": "Local speaker diarization",
        "provider": "pyannote-local",
        "source_model_id": "pyannote/speaker-diarization-community-1",
        "revision": "3533c8cf",
        "license": "cc-by-4.0",
        "files": [
            {
                "path": "config.yaml",
                "size": len(weights),
                # require_local_model does not verify checksums, so this stays
                # a placeholder; the size check is what it actually relies on.
                "sha256": "0" * 64,
            }
        ],
    }
    manifest.update(overrides)
    for key, value in list(manifest.items()):
        if value is None:
            del manifest[key]
    (path / MODEL_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    return path


class RequireLocalModelTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def test_returns_provenance_from_a_complete_package(self):
        path = _write_package(self.root)
        model = require_local_model(path)
        self.assertEqual(model.model_id, "pyannote-speaker-diarization-community-1")
        self.assertEqual(model.source_model_id, "pyannote/speaker-diarization-community-1")
        self.assertEqual(model.revision, "3533c8cf")
        self.assertEqual(model.license, "cc-by-4.0")

    def test_missing_directory_is_model_unavailable(self):
        with self.assertRaises(DiarizationError) as caught:
            require_local_model(self.root / "absent")
        self.assertIs(caught.exception.code, DiarizationErrorCode.MODEL_UNAVAILABLE)

    def test_missing_weight_file_is_model_unavailable(self):
        path = _write_package(self.root)
        (path / "config.yaml").unlink()
        with self.assertRaises(DiarizationError) as caught:
            require_local_model(path)
        self.assertIs(caught.exception.code, DiarizationErrorCode.MODEL_UNAVAILABLE)

    def test_truncated_weight_file_is_model_unavailable(self):
        """A half-finished download must not be loaded as if it were complete."""
        path = _write_package(self.root, weights=b"weights")
        (path / "config.yaml").write_bytes(b"trunc")
        with self.assertRaises(DiarizationError) as caught:
            require_local_model(path)
        self.assertIs(caught.exception.code, DiarizationErrorCode.MODEL_UNAVAILABLE)

    def test_manifest_without_provenance_is_rejected(self):
        for field in ("revision", "source_model_id"):
            with self.subTest(field=field):
                root = Path(tempfile.mkdtemp(dir=self.root))
                path = _write_package(root, **{field: None})
                with self.assertRaises(DiarizationError) as caught:
                    require_local_model(path)
                self.assertIs(
                    caught.exception.code, DiarizationErrorCode.MODEL_UNAVAILABLE
                )
                self.assertIn("source_model_id or revision", caught.exception.message)

    def test_corrupt_manifest_is_model_unavailable(self):
        path = _write_package(self.root)
        (path / MODEL_MANIFEST).write_text("{not json", encoding="utf-8")
        with self.assertRaises(DiarizationError) as caught:
            require_local_model(path)
        self.assertIs(caught.exception.code, DiarizationErrorCode.MODEL_UNAVAILABLE)

    def test_error_metadata_never_leaks_an_absolute_path(self):
        """Artifacts and logs record a name, not the user's directory layout."""
        with self.assertRaises(DiarizationError) as caught:
            require_local_model(self.root / "absent")
        for value in caught.exception.metadata.values():
            self.assertNotIn("/", str(value))


class ProviderProtocolTests(unittest.TestCase):
    def test_a_fake_satisfies_the_provider_contract(self):
        """Callers can be tested against a fake, with no model involved."""

        class FakeProvider:
            name = "fake"

            def diarize(self, request: DiarizationRequest) -> DiarizationResult:
                return DiarizationResult("fake", "rev", (), ())

        self.assertIsInstance(FakeProvider(), DiarizationProvider)

    def test_an_object_without_diarize_does_not_satisfy_it(self):
        class NotAProvider:
            name = "nope"

        self.assertNotIsInstance(NotAProvider(), DiarizationProvider)


if __name__ == "__main__":
    unittest.main()
