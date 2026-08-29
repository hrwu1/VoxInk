import struct
import tempfile
import unittest
import wave
from pathlib import Path

from voxink_core.models import Source
from voxink_core.persistence.audio import AudioRetentionWriter


def pcm(*samples: int) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def samples(path: Path) -> tuple[int, ...]:
    with wave.open(str(path), "rb") as reader:
        data = reader.readframes(reader.getnframes())
    return struct.unpack(f"<{len(data) // 2}h", data)


class AudioRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "session"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def writer(self, **kwargs) -> AudioRetentionWriter:
        return AudioRetentionWriter(
            self.directory,
            sample_rate=10,
            durable_flush=False,
            **kwargs,
        )

    def test_gap_becomes_silence_and_overlap_prefix_is_trimmed(self) -> None:
        writer = self.writer()
        (path,) = writer.append_pcm(Source.MICROPHONE, pcm(1, 2), start_ms=200)
        writer.append_pcm(Source.MICROPHONE, pcm(9, 9, 3, 4), start_ms=200)
        writer.append_pcm(Source.MICROPHONE, pcm(5), start_ms=800)
        writer.close()
        self.assertEqual(samples(path), (0, 0, 1, 2, 3, 4, 0, 0, 5))

    def test_sources_are_separate_by_default(self) -> None:
        writer = self.writer()
        writer.append_pcm("microphone", pcm(1), start_ms=0)
        writer.append_pcm("system", pcm(2), start_ms=0)
        writer.close()
        self.assertEqual(samples(self.directory / "microphone-part-001.wav"), (1,))
        self.assertEqual(samples(self.directory / "system-part-001.wav"), (2,))
        self.assertFalse((self.directory / "combined-part-001.wav").exists())

    def test_rotation_is_time_aligned_and_chunks_can_cross_boundary(self) -> None:
        writer = self.writer(max_part_seconds=1)
        paths = writer.append_pcm(Source.SYSTEM, pcm(*range(12)), start_ms=0)
        writer.close()
        self.assertEqual([path.name for path in paths], ["system-part-001.wav", "system-part-002.wav"])
        self.assertEqual(samples(paths[0]), tuple(range(10)))
        self.assertEqual(samples(paths[1]), (10, 11))

    def test_byte_cap_also_rotates_on_aligned_frame_boundary(self) -> None:
        writer = self.writer(max_part_bytes=8)
        paths = writer.append_pcm(Source.MICROPHONE, pcm(1, 2, 3, 4, 5), start_ms=0)
        writer.close()
        self.assertEqual(samples(paths[0]), (1, 2, 3, 4))
        self.assertEqual(samples(paths[1]), (5,))

    def test_combined_mix_is_chunked_aligned_and_saturating(self) -> None:
        writer = self.writer()
        writer.append_pcm(Source.MICROPHONE, pcm(30_000, -30_000, 5), start_ms=0)
        writer.append_pcm(Source.SYSTEM, pcm(10_000, -10_000), start_ms=0)
        (combined,) = writer.generate_combined(chunk_frames=1)
        self.assertEqual(samples(combined), (32767, -32768, 5))

    def test_combined_treats_missing_track_as_silence(self) -> None:
        writer = self.writer()
        writer.append_pcm(Source.SYSTEM, pcm(7, 8), start_ms=0)
        (combined,) = writer.generate_combined()
        self.assertEqual(samples(combined), (7, 8))

    def test_reopens_committed_wav_and_continues_without_overwrite(self) -> None:
        first = self.writer()
        (path,) = first.append_pcm(Source.MICROPHONE, pcm(1, 2), start_ms=0)
        first.close()
        second = self.writer()
        second.append_pcm(Source.MICROPHONE, pcm(9, 3), start_ms=100)
        second.close()
        self.assertEqual(samples(path), (1, 2, 3))

    def test_a_rerun_of_the_same_session_never_overwrites_the_first(self) -> None:
        """A reused session id restarts the clock; its audio must still land."""
        for run in (1, 2):
            writer = self.writer()
            writer.append_pcm(Source.MICROPHONE, pcm(run, run, run), start_ms=0)
            writer.close()
        self.assertEqual(samples(self.directory / "microphone-part-001.wav"), (1, 1, 1))
        self.assertEqual(samples(self.directory / "microphone-part-002.wav"), (2, 2, 2))

    def test_a_rerun_keeps_its_sources_on_matching_part_numbers(self) -> None:
        """Combined mixing pairs parts by number, so both sources must roll together."""
        first = self.writer()
        first.append_pcm(Source.MICROPHONE, pcm(1, 1), start_ms=0)
        first.append_pcm(Source.SYSTEM, pcm(2, 2), start_ms=0)
        first.close()

        second = self.writer()
        second.append_pcm(Source.MICROPHONE, pcm(3, 3), start_ms=0)
        second.append_pcm(Source.SYSTEM, pcm(4, 4), start_ms=0)
        second.close()

        self.assertEqual(samples(self.directory / "microphone-part-002.wav"), (3, 3))
        self.assertEqual(samples(self.directory / "system-part-002.wav"), (4, 4))
        combined = second.generate_combined()
        self.assertEqual(
            [path.name for path in combined],
            ["combined-part-001.wav", "combined-part-002.wav"],
        )
        # Each part mixes only its own run: 1+2 and 3+4, never across runs.
        self.assertEqual(samples(self.directory / "combined-part-001.wav"), (3, 3))
        self.assertEqual(samples(self.directory / "combined-part-002.wav"), (7, 7))

    def test_a_rerun_aligns_sources_when_only_one_has_previous_parts(self) -> None:
        """A microphone-only run must not leave the next run's sources on split bases."""
        first = self.writer()
        first.append_pcm(Source.MICROPHONE, pcm(1, 1), start_ms=0)
        first.close()

        second = self.writer()
        # The system frame arrives first and has no previous parts of its own,
        # so it is the source that would keep the old base.
        second.append_pcm(Source.SYSTEM, pcm(4, 4), start_ms=0)
        second.append_pcm(Source.MICROPHONE, pcm(3, 3), start_ms=0)
        second.close()

        self.assertEqual(samples(self.directory / "system-part-002.wav"), (4, 4))
        self.assertEqual(samples(self.directory / "microphone-part-002.wav"), (3, 3))
        combined = second.generate_combined()
        self.assertEqual(
            [path.name for path in combined],
            ["combined-part-001.wav", "combined-part-002.wav"],
        )
        # Part 001 is the first run's microphone alone; the new run mixes 3+4.
        self.assertEqual(samples(self.directory / "combined-part-001.wav"), (1, 1))
        self.assertEqual(samples(self.directory / "combined-part-002.wav"), (7, 7))

    def test_a_rerun_rotates_from_its_own_part_base(self) -> None:
        first = self.writer(max_part_seconds=1)
        first.append_pcm(Source.SYSTEM, pcm(*range(25)), start_ms=0)
        first.close()
        self.assertEqual(
            sorted(path.name for path in self.directory.glob("system-*.wav")),
            ["system-part-001.wav", "system-part-002.wav", "system-part-003.wav"],
        )

        second = self.writer(max_part_seconds=1)
        paths = second.append_pcm(Source.SYSTEM, pcm(*range(30, 42)), start_ms=0)
        second.close()
        self.assertEqual(
            [path.name for path in paths], ["system-part-004.wav", "system-part-005.wav"]
        )
        self.assertEqual(samples(paths[0]), tuple(range(30, 40)))
        self.assertEqual(samples(paths[1]), (40, 41))

    def test_combined_skips_finished_parts_instead_of_aborting(self) -> None:
        first = self.writer()
        first.append_pcm(Source.MICROPHONE, pcm(1), start_ms=0)
        first.close()
        self.assertEqual(
            [path.name for path in first.generate_combined()], ["combined-part-001.wav"]
        )

        second = self.writer()
        second.append_pcm(Source.MICROPHONE, pcm(5), start_ms=0)
        second.close()
        # The first run's combined part is final and must survive untouched,
        # without preventing this run's part from being mixed.
        self.assertEqual(
            [path.name for path in second.generate_combined()], ["combined-part-002.wav"]
        )
        self.assertEqual(samples(self.directory / "combined-part-001.wav"), (1,))
        self.assertEqual(samples(self.directory / "combined-part-002.wav"), (5,))

    def test_an_unreadable_previous_part_does_not_swallow_live_audio(self) -> None:
        first = self.writer()
        first.append_pcm(Source.MICROPHONE, pcm(1, 2), start_ms=0)
        first.close()
        (self.directory / "microphone-part-001.wav").write_bytes(b"not a wav")

        second = self.writer()
        second.append_pcm(Source.MICROPHONE, pcm(7, 8), start_ms=0)
        second.close()
        self.assertEqual(samples(self.directory / "microphone-part-002.wav"), (7, 8))

    def test_validation_rejects_misaligned_or_negative_input(self) -> None:
        writer = self.writer()
        with self.assertRaises(ValueError):
            writer.append_pcm(Source.SYSTEM, b"x", start_ms=0)
        with self.assertRaises(ValueError):
            writer.append_pcm(Source.SYSTEM, pcm(1), start_ms=-1)


if __name__ == "__main__":
    unittest.main()
