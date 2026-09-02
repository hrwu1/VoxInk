from __future__ import annotations

import unittest

import numpy as np

from voxink_core.echo import EchoDetector, EchoPolicy


RATE = 16_000


def _pcm(samples: np.ndarray) -> bytes:
    return np.clip(samples, -32768, 32767).astype("<i2").tobytes()


def _noise(seconds: float, seed: int, amplitude: float = 6000.0) -> np.ndarray:
    generator = np.random.default_rng(seed)
    return generator.normal(0.0, amplitude, int(seconds * RATE))


class EchoDetectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.detector = EchoDetector(EchoPolicy(max_lag_ms=300))

    def test_delayed_attenuated_copy_is_echo(self) -> None:
        played = _noise(2.0, seed=1)
        # The system channel played `played` starting at t=1000 ms.
        self.detector.observe("s", "system-1", 1000, _pcm(played))
        # The microphone heard it 120 ms later, quieter, with room noise.
        lag = int(0.120 * RATE)
        heard = 0.35 * played[: RATE] + _noise(1.0, seed=2, amplitude=400.0)
        verdict = self.detector.judge("s", 1000 + 120, 2000 + 120, _pcm(heard))
        self.assertTrue(verdict.echo, verdict)
        self.assertGreater(verdict.correlation, 0.8)
        self.assertAlmostEqual(verdict.lag_ms, 120, delta=5)
        self.assertEqual(verdict.channel, "system-1")
        self.assertEqual(lag, int(0.120 * RATE))

    def test_independent_speech_is_not_echo(self) -> None:
        self.detector.observe("s", "system-1", 1000, _pcm(_noise(2.0, seed=3)))
        own = _noise(1.0, seed=4)
        verdict = self.detector.judge("s", 1200, 2200, _pcm(own))
        self.assertFalse(verdict.echo, verdict)
        self.assertLess(verdict.correlation, 0.2)

    def test_silent_reference_never_explains_speech(self) -> None:
        self.detector.observe("s", "system-1", 0, bytes(2 * RATE * 2))
        verdict = self.detector.judge("s", 100, 1100, _pcm(_noise(1.0, seed=5)))
        self.assertFalse(verdict.echo)
        self.assertEqual(verdict.correlation, 0.0)

    def test_no_reference_means_no_echo(self) -> None:
        verdict = self.detector.judge("s", 0, 1000, _pcm(_noise(1.0, seed=6)))
        self.assertFalse(verdict.echo)
        self.assertFalse(self.detector.has_reference("s"))

    def test_history_is_bounded_and_per_session(self) -> None:
        detector = EchoDetector(EchoPolicy(history_ms=2_000))
        detector.observe("a", "system-1", 0, _pcm(_noise(0.5, seed=7)))
        detector.observe("a", "system-1", 10_000, _pcm(_noise(0.5, seed=8)))
        history = detector._history[("a", "system-1")]
        self.assertEqual([start for start, _ in history], [10_000])
        self.assertFalse(detector.has_reference("b"))
        detector.forget("a")
        self.assertFalse(detector.has_reference("a"))

    def test_best_channel_wins(self) -> None:
        played = _noise(1.5, seed=9)
        self.detector.observe("s", "system-a", 0, _pcm(_noise(1.5, seed=10)))
        self.detector.observe("s", "system-b", 0, _pcm(played))
        verdict = self.detector.judge("s", 50, 1050, _pcm(0.5 * played[: RATE]))
        self.assertTrue(verdict.echo)
        self.assertEqual(verdict.channel, "system-b")
        self.assertAlmostEqual(verdict.lag_ms, 50, delta=5)

    def test_policy_validation(self) -> None:
        with self.assertRaises(ValueError):
            EchoPolicy(min_correlation=0.0)
        with self.assertRaises(ValueError):
            EchoPolicy(history_ms=100, max_lag_ms=300)


if __name__ == "__main__":
    unittest.main()
