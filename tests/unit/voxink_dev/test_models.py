from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest

from voxink_core.local_models import MODEL_MANIFEST
from voxink_dev import models
from voxink_dev.models import DEFAULT_MODEL, DownloadError, ModelSource, main


REPOSITORY_MODELS = Path(__file__).resolve().parents[3] / "models"
CONTENT = {"config.json": b"{}", "weights/model.bin": b"weights"}


def write_manifest(root: Path, name: str, *, gated: bool = False) -> Path:
    path = root / name
    path.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "model": name,
        "source_model_id": f"Publisher/{name}",
        "revision": "a" * 40,
        "license": "apache-2.0",
        "gated": gated,
        "files": [
            {"path": file, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for file, data in CONTENT.items()
        ],
    }
    (path / MODEL_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    return path


class FakeHub:
    def __init__(self, content: dict[str, bytes] = CONTENT, error: DownloadError | None = None) -> None:
        self.content = content
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, source: ModelSource, filename: str) -> None:
        self.calls.append((source.repo_id, source.revision, filename))
        if self.error is not None:
            raise self.error
        target = source.path / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.content[filename])


class DownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.model = write_manifest(self.root, DEFAULT_MODEL)

    def run_main(self, *argv: str, fetch: FakeHub, answer: str = "y", interactive: bool = True) -> tuple[int, str]:
        output = io.StringIO()
        original = models.sys.stdin
        models.sys.stdin = type("Stdin", (), {"isatty": staticmethod(lambda: interactive)})()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                code = main(["--models-root", str(self.root), *argv], fetch=fetch, ask=lambda _: answer)
        finally:
            models.sys.stdin = original
        return code, output.getvalue()

    def test_downloads_the_default_model_at_its_pinned_revision_and_verifies_it(self) -> None:
        hub = FakeHub()
        code, output = self.run_main("download", fetch=hub)
        self.assertEqual(code, 0, output)
        self.assertEqual(
            [(f"Publisher/{DEFAULT_MODEL}", "a" * 40, file) for file in CONTENT], hub.calls
        )
        self.assertIn("installed and verified (2 files)", output)
        self.assertIn("license: apache-2.0", output)
        self.assertIn("covered by its MIT license", output)

    def test_nothing_is_fetched_without_consent(self) -> None:
        hub = FakeHub()
        code, output = self.run_main("download", fetch=hub, answer="n")
        self.assertEqual((code, hub.calls), (1, []))
        self.assertIn("Nothing downloaded", output)

    def test_a_non_interactive_run_needs_an_explicit_yes(self) -> None:
        hub = FakeHub()
        code, output = self.run_main("download", fetch=hub, interactive=False)
        self.assertEqual((code, hub.calls), (2, []))
        self.assertIn("--yes", output)
        code, _ = self.run_main("download", "--yes", fetch=hub, interactive=False)
        self.assertEqual(code, 0)

    def test_a_file_that_differs_from_the_manifest_fails_verification(self) -> None:
        tampered = dict(CONTENT, **{"weights/model.bin": b"WEIGHTS"})
        code, output = self.run_main("download", fetch=FakeHub(tampered))
        self.assertEqual(code, 1)
        self.assertIn("checksum does not match: weights/model.bin", output)

    def test_an_installed_model_is_not_fetched_again(self) -> None:
        self.run_main("download", fetch=FakeHub())
        hub = FakeHub()
        code, output = self.run_main("download", fetch=hub)
        self.assertEqual((code, hub.calls), (0, []))
        self.assertIn("already installed", output)

    def test_a_gated_model_explains_how_to_get_access(self) -> None:
        write_manifest(self.root, "gated-model", gated=True)
        refusal = DownloadError("Publisher/gated-model is gated. Accept its conditions first.")
        code, output = self.run_main("download", "gated-model", fetch=FakeHub(error=refusal))
        self.assertEqual(code, 1)
        self.assertIn("hf auth login", output)
        self.assertIn("gated-model was not downloaded: Publisher/gated-model is gated", output)

    def test_an_unknown_model_lists_the_available_ones(self) -> None:
        code, output = self.run_main("download", "nonexistent", fetch=FakeHub())
        self.assertEqual(code, 2)
        self.assertIn(DEFAULT_MODEL, output)


class RepositoryManifestTests(unittest.TestCase):
    def test_every_tracked_model_names_its_source_revision_and_license(self) -> None:
        manifests = sorted(REPOSITORY_MODELS.glob(f"*/{MODEL_MANIFEST}"))
        self.assertTrue(manifests)
        for path in manifests:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(model=path.parent.name):
                self.assertRegex(manifest.get("source_model_id", ""), r"^[\w.-]+/[\w.-]+$")
                self.assertTrue(re.fullmatch(r"[0-9a-f]{40}", manifest.get("revision", "")))
                self.assertTrue(manifest.get("license"))

    def test_the_default_model_is_downloadable(self) -> None:
        names = [source.name for source in models.read_sources(REPOSITORY_MODELS)]
        self.assertIn(DEFAULT_MODEL, names)


if __name__ == "__main__":
    unittest.main()
