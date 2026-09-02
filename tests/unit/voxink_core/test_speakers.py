from __future__ import annotations

import math
from pathlib import Path
import stat
import tempfile
import unittest

from voxink_core.speakers import SpeakerRegistry, SpeakerRegistryError, cosine


def _vector(angle: float, dimension: int = 8) -> list[float]:
    """A unit vector rotated by `angle` in the first two dimensions."""
    values = [0.0] * dimension
    values[0] = math.cos(angle)
    values[1] = math.sin(angle)
    return values


class SpeakerRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.registry = SpeakerRegistry(Path(self.temporary.name) / "state" / "speakers.json")

    def test_enroll_match_and_forget(self) -> None:
        wei = self.registry.enroll("Wei", _vector(0.0), model_id="pyannote/x")
        self.assertEqual(stat.S_IMODE(self.registry.path.stat().st_mode), 0o600)
        close = self.registry.match(_vector(0.2), model_id="pyannote/x")
        self.assertIsNotNone(close)
        self.assertEqual(close.speaker.id, wei.id)
        self.assertGreater(close.similarity, 0.9)
        far = self.registry.match(_vector(1.4), model_id="pyannote/x")
        self.assertIsNone(far)
        # A different model's embeddings never match.
        self.assertIsNone(self.registry.match(_vector(0.0), model_id="other/model"))
        self.registry.forget(wei.id)
        self.assertEqual(self.registry.list(), [])
        with self.assertRaises(SpeakerRegistryError):
            self.registry.forget(wei.id)

    def test_enrolling_the_same_name_averages_samples(self) -> None:
        first = self.registry.enroll("Wei", _vector(0.0), model_id="m")
        second = self.registry.enroll("wei", _vector(0.4), model_id="m")
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.samples, 2)
        self.assertAlmostEqual(math.atan2(second.embedding[1], second.embedding[0]), 0.2, places=5)
        self.assertEqual(len(self.registry.list()), 1)

    def test_rename_and_validation(self) -> None:
        wei = self.registry.enroll("Wei", _vector(0.0), model_id="m")
        self.assertEqual(self.registry.rename(wei.id, "  Wei   Zhang ").name, "Wei Zhang")
        self.assertEqual(self.registry.find_by_name("wei zhang").id, wei.id)
        with self.assertRaises(SpeakerRegistryError):
            self.registry.rename(wei.id, "   ")
        with self.assertRaises(SpeakerRegistryError):
            self.registry.enroll("Nobody", [0.0, 0.0], model_id="m")
        with self.assertRaises(SpeakerRegistryError):
            self.registry.enroll("", _vector(0.0), model_id="m")
        self.assertEqual(cosine([1.0, 0.0], [0.0, 1.0]), 0.0)

    def test_listing_never_exposes_embeddings_by_default(self) -> None:
        self.registry.enroll("Wei", _vector(0.0), model_id="m")
        listed = self.registry.list()[0].to_dict()
        self.assertNotIn("embedding", listed)
        self.assertEqual(listed["dimension"], 8)


if __name__ == "__main__":
    unittest.main()
