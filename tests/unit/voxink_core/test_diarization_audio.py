from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import wave

from voxink_core.models import Source
from voxink_core.diarization.audio import assemble, source_parts
from voxink_core.diarization.models import DiarizationError, DiarizationErrorCode


def write_wav(
    path: Path,
    frames: int,
    *,
    rate: int = 16_000,
    channels: int = 1,
    width: int = 2,
    value: int = 0,
) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        sample = value.to_bytes(width, "little", signed=True) * channels
        writer.writeframes(sample * frames)


class AssembleTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.destination = self.directory / "joined.wav"

    def part(self, number: int) -> Path:
        return self.directory / f"microphone-part-{number:03d}.wav"

    def assemble(self, **kwargs):
        return assemble(self.directory, self.destination, **kwargs)

    def assertCode(self, context, code: DiarizationErrorCode):
        self.assertIs(context.exception.code, code)


class HappyPathTests(AssembleTestCase):
    def test_joins_one_part(self):
        write_wav(self.part(1), 16_000)
        self.assertEqual(self.assemble(), 1000)
        with wave.open(str(self.destination)) as reader:
            self.assertEqual(reader.getnframes(), 16_000)
            self.assertEqual(reader.getframerate(), 16_000)
            self.assertEqual(reader.getnchannels(), 1)

    def test_joins_rotated_parts_into_one_timeline(self):
        write_wav(self.part(1), 16_000, value=1)
        write_wav(self.part(2), 16_000, value=2)
        write_wav(self.part(3), 8_000, value=3)
        self.assertEqual(self.assemble(), 2500)
        with wave.open(str(self.destination)) as reader:
            self.assertEqual(reader.getnframes(), 40_000)
            payload = reader.readframes(40_000)
        # Part order is preserved, so the marker values appear in sequence.
        self.assertEqual(payload[0:2], (1).to_bytes(2, "little"))
        self.assertEqual(payload[32_000:32_002], (2).to_bytes(2, "little"))
        self.assertEqual(payload[64_000:64_002], (3).to_bytes(2, "little"))

    def test_leaves_the_retained_parts_untouched(self):
        write_wav(self.part(1), 16_000, value=7)
        before = self.part(1).read_bytes()
        self.assemble()
        self.assertEqual(self.part(1).read_bytes(), before)

    def test_source_parts_are_ordered_numerically(self):
        for number in (10, 2, 1):
            write_wav(self.part(number), 160)
        self.assertEqual(
            [p.name for p in source_parts(self.directory)],
            [
                "microphone-part-001.wav",
                "microphone-part-002.wav",
                "microphone-part-010.wav",
            ],
        )


class SourceIsolationTests(AssembleTestCase):
    def test_system_audio_is_refused(self):
        write_wav(self.directory / "system-part-001.wav", 16_000)
        with self.assertRaises(DiarizationError) as caught:
            self.assemble(source=Source.SYSTEM)
        self.assertCode(caught, DiarizationErrorCode.INVALID_AUDIO)

    def test_combined_audio_is_never_picked_up(self):
        """Mixing sources would invent echoes and duplicate speakers."""
        write_wav(self.directory / "combined-part-001.wav", 16_000)
        with self.assertRaises(DiarizationError) as caught:
            self.assemble()
        self.assertCode(caught, DiarizationErrorCode.AUDIO_UNAVAILABLE)

    def test_system_parts_are_ignored_when_joining_microphone(self):
        write_wav(self.part(1), 16_000)
        write_wav(self.directory / "system-part-001.wav", 32_000)
        self.assertEqual(self.assemble(), 1000)


class RejectionTests(AssembleTestCase):
    def test_no_retained_audio_is_audio_unavailable(self):
        with self.assertRaises(DiarizationError) as caught:
            self.assemble()
        self.assertCode(caught, DiarizationErrorCode.AUDIO_UNAVAILABLE)

    def test_numbering_that_does_not_start_at_001_is_refused(self):
        """A second run's parts sit above the first's and carry no clock origin."""
        write_wav(self.part(4), 16_000)
        write_wav(self.part(5), 16_000)
        with self.assertRaises(DiarizationError) as caught:
            self.assemble()
        self.assertCode(caught, DiarizationErrorCode.INVALID_AUDIO)
        self.assertIn("001", caught.exception.message)

    def test_a_missing_part_is_refused(self):
        write_wav(self.part(1), 16_000)
        write_wav(self.part(3), 16_000)
        with self.assertRaises(DiarizationError) as caught:
            self.assemble()
        self.assertCode(caught, DiarizationErrorCode.INVALID_AUDIO)
        self.assertIn("002", caught.exception.message)

    def test_a_short_interior_part_is_refused(self):
        """Audio missing from the middle would slide every later block earlier."""
        write_wav(self.part(1), 16_000)
        write_wav(self.part(2), 8_000)
        write_wav(self.part(3), 16_000)
        with self.assertRaises(DiarizationError) as caught:
            self.assemble()
        self.assertCode(caught, DiarizationErrorCode.INVALID_AUDIO)
        self.assertIn("microphone-part-002.wav", caught.exception.message)

    def test_a_short_final_part_is_accepted(self):
        write_wav(self.part(1), 16_000)
        write_wav(self.part(2), 4_000)
        self.assertEqual(self.assemble(), 1250)

    def test_the_wrong_sample_rate_is_refused(self):
        write_wav(self.part(1), 16_000, rate=48_000)
        with self.assertRaises(DiarizationError) as caught:
            self.assemble()
        self.assertCode(caught, DiarizationErrorCode.INVALID_AUDIO)

    def test_stereo_is_refused(self):
        write_wav(self.part(1), 16_000, channels=2)
        with self.assertRaises(DiarizationError) as caught:
            self.assemble()
        self.assertCode(caught, DiarizationErrorCode.INVALID_AUDIO)

    def test_a_corrupt_part_is_refused(self):
        self.part(1).write_bytes(b"not a wav file")
        with self.assertRaises(DiarizationError) as caught:
            self.assemble()
        self.assertCode(caught, DiarizationErrorCode.INVALID_AUDIO)

    def test_an_empty_part_is_refused(self):
        write_wav(self.part(1), 0)
        with self.assertRaises(DiarizationError) as caught:
            self.assemble()
        self.assertCode(caught, DiarizationErrorCode.INVALID_AUDIO)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
