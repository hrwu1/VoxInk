from __future__ import annotations

import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from voxink_dev.benchmark import SAMPLE_RATE, ChunkResult, chunk_of, read_mono_16k


def _write(path: Path, samples: array, *, rate: int, width: int) -> None:
    if width == 2:
        payload = samples.tobytes()
    elif width == 3:
        payload = b"".join(
            (value << 8).to_bytes(3, "little", signed=True) for value in samples
        )
    else:
        payload = array("i", (value << 16 for value in samples)).tobytes()
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(payload)


class AudioReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def test_reads_16_bit_audio_unchanged(self) -> None:
        samples = array("h", [0, 1000, -1000, 32767, -32768])
        path = self.directory / "plain.wav"
        _write(path, samples, rate=SAMPLE_RATE, width=2)
        self.assertEqual(samples.tobytes(), read_mono_16k(path))

    def test_narrows_24_and_32_bit_audio(self) -> None:
        """The repository's own sample is 24-bit, so this is the default path."""
        samples = array("h", [0, 1000, -1000, 500])
        for width in (3, 4):
            path = self.directory / f"wide-{width}.wav"
            _write(path, samples, rate=SAMPLE_RATE, width=width)
            with self.subTest(width=width):
                self.assertEqual(samples.tobytes(), read_mono_16k(path))

    def test_decimates_a_whole_multiple_of_the_target_rate(self) -> None:
        samples = array("h", [300, 600, 900, 30, 60, 90])
        path = self.directory / "fast.wav"
        _write(path, samples, rate=SAMPLE_RATE * 3, width=2)
        decimated = array("h")
        decimated.frombytes(read_mono_16k(path))
        self.assertEqual([600, 60], list(decimated))

    def test_rejects_a_rate_it_cannot_decimate(self) -> None:
        path = self.directory / "odd.wav"
        _write(path, array("h", [1, 2, 3]), rate=44_100, width=2)
        with self.assertRaisesRegex(ValueError, "whole multiple"):
            read_mono_16k(path)

    def test_rejects_multichannel_audio(self) -> None:
        path = self.directory / "stereo.wav"
        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(2)
            writer.setsampwidth(2)
            writer.setframerate(SAMPLE_RATE)
            writer.writeframes(array("h", [1, 2, 3, 4]).tobytes())
        with self.assertRaisesRegex(ValueError, "mono"):
            read_mono_16k(path)


class ChunkTests(unittest.TestCase):
    def test_repeats_short_audio_to_fill_the_requested_length(self) -> None:
        one_second = array("h", [7]) * SAMPLE_RATE
        chunk = chunk_of(one_second.tobytes(), 2.5)
        self.assertEqual(int(2.5 * SAMPLE_RATE) * 2, len(chunk))

    def test_truncates_audio_longer_than_the_chunk(self) -> None:
        ten_seconds = array("h", [7]) * (SAMPLE_RATE * 10)
        self.assertEqual(SAMPLE_RATE * 2 * 2, len(chunk_of(ten_seconds.tobytes(), 2.0)))

    def test_empty_audio_is_a_clear_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "no samples"):
            chunk_of(b"", 1.0)


class VerdictTests(unittest.TestCase):
    def test_real_time_factor_decides_whether_captions_can_keep_up(self) -> None:
        fast = ChunkResult(5.0, 3, 1_000.0, 1_100.0, 1_200.0, 0.2)
        slow = ChunkResult(5.0, 3, 9_000.0, 9_500.0, 9_900.0, 1.8)
        self.assertTrue(fast.keeps_up)
        self.assertFalse(slow.keeps_up)


if __name__ == "__main__":
    unittest.main()
