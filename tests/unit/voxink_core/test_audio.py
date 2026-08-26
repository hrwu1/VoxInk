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

    def test_validation_rejects_misaligned_or_negative_input(self) -> None:
        writer = self.writer()
        with self.assertRaises(ValueError):
            writer.append_pcm(Source.SYSTEM, b"x", start_ms=0)
        with self.assertRaises(ValueError):
            writer.append_pcm(Source.SYSTEM, pcm(1), start_ms=-1)


if __name__ == "__main__":
    unittest.main()
