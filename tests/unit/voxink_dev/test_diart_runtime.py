from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from voxink_dev.diart_runtime import MARKER, cleanup, initialize, model_paths, runtime_environment, validate_runtime


class DiartRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / ".experiments/diart-test"

    def create(self):
        return initialize(self.root, python=Path("/fake/python"))

    def test_init_refuses_existing_directory_without_adopting_it(self):
        self.root.mkdir(parents=True)
        recording = self.root / "recording.wav"
        recording.write_bytes(b"original")
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(recording.read_bytes(), b"original")
        self.assertFalse((self.root / MARKER).exists())

    def test_cleanup_dry_run_then_delete_only_owned_root(self):
        self.create()
        foreign = self.base / "recording.wav"
        foreign.write_bytes(b"preserve")
        (self.root / "outputs/result.json").write_text("{}")
        preview = cleanup(self.root)
        self.assertFalse(preview["delete"])
        self.assertTrue(self.root.exists())
        cleanup(self.root, delete=True)
        self.assertFalse(self.root.exists())
        self.assertEqual(foreign.read_bytes(), b"preserve")

    def test_cleanup_does_not_follow_descendant_symlinks(self):
        self.create()
        foreign = self.base / "preserve"
        foreign.mkdir()
        (foreign / "keep.txt").write_text("keep")
        (self.root / "outputs/foreign").symlink_to(foreign, target_is_directory=True)
        cleanup(self.root, delete=True)
        self.assertEqual((foreign / "keep.txt").read_text(), "keep")

    def test_cleanup_refuses_moved_or_forged_metadata(self):
        self.create()
        path = self.root / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["runtime_root"] = str(self.base)
        path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            cleanup(self.root, delete=True)
        self.assertTrue(self.root.exists())

    def test_cleanup_refuses_symlink_root_or_marker(self):
        self.create()
        linked = self.root.parent / "diart-linked"
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            cleanup(linked, delete=True)
        marker = self.root / MARKER
        moved = self.base / "marker"
        marker.rename(moved)
        marker.symlink_to(moved)
        with self.assertRaises(ValueError):
            validate_runtime(self.root)

    def test_environment_redirects_caches_and_home_under_runtime(self):
        self.create()
        environment = runtime_environment(self.root)
        for key in ("HOME", "TMPDIR", "HF_HOME", "HF_HUB_CACHE", "TORCH_HOME", "PIP_CACHE_DIR", "PYTHONPYCACHEPREFIX", "MPLCONFIGDIR"):
            self.assertTrue(Path(environment[key]).is_relative_to(self.root), key)
        self.assertEqual(environment["HF_HUB_OFFLINE"], "1")
        self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
        self.assertNotIn("HF_TOKEN", environment)

    def test_foreign_directory_without_marker_cannot_be_deleted(self):
        self.root.mkdir(parents=True)
        (self.root / "keep.txt").write_text("keep")
        with self.assertRaises(ValueError):
            cleanup(self.root, delete=True)
        self.assertEqual((self.root / "keep.txt").read_text(), "keep")

    def test_environment_drops_inherited_tokens_and_pip_overrides(self):
        self.create()
        with patch.dict("os.environ", {"HF_TOKEN": "test-only-placeholder", "PIP_TARGET": "/foreign", "PIP_INDEX_URL": "https://example.invalid"}):
            environment = runtime_environment(self.root)
        for key in ("HF_TOKEN", "PIP_TARGET", "PIP_INDEX_URL"):
            self.assertNotIn(key, environment)

    def test_model_manifest_refuses_foreign_paths_and_changed_weights(self):
        manifest = self.create()
        checkpoint = self.root / "models/model.bin"
        checkpoint.write_bytes(b"fixture")
        entry = {"checkpoint": str(checkpoint), "sha256": hashlib.sha256(b"fixture").hexdigest()}
        manifest["models"] = {kind: entry.copy() for kind in ("segmentation", "embedding")}
        (self.root / "manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(model_paths(self.root)["segmentation"], checkpoint)
        checkpoint.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            model_paths(self.root)
        foreign = self.base / "foreign.bin"
        foreign.write_bytes(b"fixture")
        manifest["models"]["segmentation"]["checkpoint"] = str(foreign)
        (self.root / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            model_paths(self.root)


if __name__ == "__main__":
    unittest.main()
