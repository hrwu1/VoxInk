from __future__ import annotations

import unittest

import numpy as np

from clients.windows_capture.models import CaptureError
from clients.windows_capture.pcm_normalizer import PCMNormalizer


def sine(rate: int, seconds: float, frequency: float = 440.0, channels: int = 1, amplitude: float = 0.5) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    mono = amplitude * np.sin(2 * np.pi * frequency * t)
    if channels == 1:
        return mono.astype(np.float32)
    return np.stack([mono] * channels, axis=1).astype(np.float32)


def decode(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0


class NormalizerTests(unittest.TestCase):
    def test_passthrough_at_the_output_rate_keeps_every_sample(self) -> None:
        normalizer = PCMNormalizer(16_000)
        samples = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=np.float32)
        self.assertEqual([0, 16384, -16384, 32767, -32768], list(np.frombuffer(normalizer.normalize(samples, 16_000), "<i2")))

    def test_stereo_is_mixed_to_mono_and_int16_input_is_scaled(self) -> None:
        normalizer = PCMNormalizer(16_000)
        stereo = np.array([[16384, -16384], [8192, 8192]], dtype=np.int16)
        self.assertEqual([0, 8192], list(np.frombuffer(normalizer.normalize(stereo, 16_000), "<i2")))

    def test_48k_stereo_resamples_to_16k_mono_with_a_third_of_the_samples(self) -> None:
        normalizer = PCMNormalizer(16_000)
        out = decode(normalizer.normalize(sine(48_000, 1.0, channels=2), 48_000))
        self.assertAlmostEqual(16_000, out.size, delta=2)
        # The tone survives: peak near the source amplitude, once the filter has settled.
        self.assertGreater(np.abs(out[200:]).max(), 0.45)
        self.assertLess(np.abs(out[200:]).max(), 0.55)

    def test_state_carries_across_buffers_without_discontinuities(self) -> None:
        whole = PCMNormalizer(16_000)
        chunked = PCMNormalizer(16_000)
        signal = sine(44_100, 0.5, frequency=300.0)
        expected = decode(whole.normalize(signal, 44_100))
        pieces = []
        for start in range(0, signal.size, 441):
            pieces.append(chunked.normalize(signal[start:start + 441], 44_100))
        actual = decode(b"".join(pieces))
        self.assertEqual(expected.size, actual.size)
        np.testing.assert_allclose(actual, expected, atol=2.0 / 32768.0)
        # No sample-to-sample jump larger than the tone's own slope allows.
        self.assertLess(np.abs(np.diff(actual)).max(), 0.2)

    def test_a_rate_change_resets_the_filter_state(self) -> None:
        normalizer = PCMNormalizer(16_000)
        normalizer.normalize(sine(48_000, 0.1), 48_000)
        self.assertEqual(48_000, normalizer._input_rate)
        normalizer.normalize(sine(44_100, 0.1), 44_100)
        self.assertEqual(44_100, normalizer._input_rate)
        self.assertEqual(PCMNormalizer.FILTER_TAPS - 1, normalizer._history.size)

    def test_upsampling_and_empty_buffers_are_handled(self) -> None:
        normalizer = PCMNormalizer(16_000)
        self.assertEqual(b"", normalizer.normalize(np.zeros((0, 2), dtype=np.float32), 8_000))
        out = decode(normalizer.normalize(sine(8_000, 0.5), 8_000))
        self.assertAlmostEqual(8_000, out.size, delta=2)

    def test_unusable_formats_are_refused(self) -> None:
        normalizer = PCMNormalizer(16_000)
        with self.assertRaises(CaptureError):
            normalizer.normalize(np.zeros(4, dtype=np.uint8), 16_000)
        with self.assertRaises(CaptureError):
            normalizer.normalize(np.zeros(4, dtype=np.float32), 0)


if __name__ == "__main__":
    unittest.main()
