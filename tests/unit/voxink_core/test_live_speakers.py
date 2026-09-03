"""Live speaker names from a rolling mean of segment embeddings."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import numpy as np

from voxink_core.diarization.live import LiveSpeakerIdentifier, LiveSpeakerPolicy
from voxink_core.speakers import SpeakerRegistry

MODEL = "pyannote/speaker-diarization-community-1"
ALICE = np.array([1.0, 0.0, 0.0, 0.0])
BOB = np.array([0.0, 1.0, 0.0, 0.0])


def _pcm(voice: np.ndarray, seconds: float, *, wobble: float = 0.0) -> bytes:
    """One segment; the first sample encodes which voice the fake embedder hears."""
    count = int(16_000 * seconds)
    samples = np.zeros(count, dtype="<i2")
    samples[0] = int(np.argmax(voice)) + 1
    samples[1] = int(wobble * 1000)
    return samples.tobytes()


def _fake_embedder(samples: np.ndarray) -> np.ndarray:
    index = int(round(samples[0] * 32768.0)) - 1
    wobble = samples[1] * 32768.0 / 1000.0
    vector = np.zeros(4)
    vector[index] = 1.0
    vector[2] = wobble
    return vector


class LiveSpeakerIdentifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.registry = SpeakerRegistry(Path(self.temporary.name) / "speakers.json")
        self.loads = 0

        def factory():
            self.loads += 1
            return _fake_embedder, MODEL

        self.identifier = LiveSpeakerIdentifier(
            self.registry, model_path=Path(self.temporary.name), embedder_factory=factory,
            policy=LiveSpeakerPolicy(window_s=10.0, min_segment_s=1.0, min_speech_s=4.0, segment_guard=0.3),
        )

    def test_nothing_is_named_and_nothing_loads_without_a_remembered_voice(self) -> None:
        self.assertIsNone(self.identifier.identify("s", "microphone-1", _pcm(ALICE, 5.0), 16_000))
        self.assertEqual(self.loads, 0)

    def test_a_name_appears_once_enough_of_one_voice_has_accumulated(self) -> None:
        self.registry.enroll("Alice", ALICE, model_id=MODEL)
        first = self.identifier.identify("s", "microphone-1", _pcm(ALICE, 2.0), 16_000)
        second = self.identifier.identify("s", "microphone-1", _pcm(ALICE, 2.5), 16_000)
        self.assertIsNone(first, "two seconds is not yet enough to trust")
        self.assertEqual(second, "Alice")
        self.assertEqual(self.loads, 1)

    def test_short_segments_and_other_formats_are_ignored(self) -> None:
        self.registry.enroll("Alice", ALICE, model_id=MODEL)
        for _ in range(10):
            self.assertIsNone(self.identifier.identify("s", "microphone-1", _pcm(ALICE, 0.5), 16_000))
        self.assertIsNone(self.identifier.identify("s", "microphone-1", _pcm(ALICE, 5.0), 48_000))
        self.assertIsNone(self.identifier.identify("s", "microphone-1", _pcm(ALICE, 5.0), 16_000, channels=2))
        self.assertEqual(self.loads, 0)

    def test_a_brief_other_voice_is_not_given_the_dominant_name(self) -> None:
        self.registry.enroll("Alice", ALICE, model_id=MODEL)
        for _ in range(3):
            self.identifier.identify("s", "system-1", _pcm(ALICE, 3.0), 16_000)
        self.assertEqual(self.identifier.identify("s", "system-1", _pcm(ALICE, 1.0), 16_000), "Alice")
        # Bob's utterance shifts the mean a little but not past Alice; the
        # utterance itself does not resemble her, so it stays anonymous.
        self.assertIsNone(self.identifier.identify("s", "system-1", _pcm(BOB, 1.5), 16_000))

    def test_streams_are_independent_and_forgotten_with_their_session(self) -> None:
        self.registry.enroll("Alice", ALICE, model_id=MODEL)
        self.assertEqual(self.identifier.identify("s", "microphone-1", _pcm(ALICE, 5.0), 16_000), "Alice")
        self.assertIsNone(self.identifier.identify("s", "system-1", _pcm(ALICE, 2.0), 16_000))
        self.identifier.forget("s")
        self.assertIsNone(self.identifier.identify("s", "microphone-1", _pcm(ALICE, 2.0), 16_000))

    def test_a_loader_failure_switches_naming_off_quietly(self) -> None:
        self.registry.enroll("Alice", ALICE, model_id=MODEL)

        def broken():
            raise RuntimeError("no torch here")

        identifier = LiveSpeakerIdentifier(
            self.registry, model_path=Path(self.temporary.name), embedder_factory=broken
        )
        self.assertIsNone(identifier.identify("s", "microphone-1", _pcm(ALICE, 5.0), 16_000))
        self.assertIn("no torch here", identifier.error or "")
        self.assertIsNone(identifier.identify("s", "microphone-1", _pcm(ALICE, 5.0), 16_000))

    def test_a_different_models_prints_are_never_compared(self) -> None:
        self.registry.enroll("Alice", ALICE, model_id="some/other-model")
        self.assertIsNone(self.identifier.identify("s", "microphone-1", _pcm(ALICE, 5.0), 16_000))


if __name__ == "__main__":
    unittest.main()
