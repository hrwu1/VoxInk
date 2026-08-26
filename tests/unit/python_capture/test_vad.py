from array import array
import unittest

from clients.python_capture.vad import EnergyVadChunker, VadConfig, pcm_s16le_rms


def pcm_frame(amplitude: int, samples: int) -> bytes:
    return array("h", [amplitude] * samples).tobytes()


class RmsTests(unittest.TestCase):
    def test_silence(self) -> None:
        self.assertEqual(pcm_s16le_rms(bytes(640)), 0.0)

    def test_constant_amplitude(self) -> None:
        self.assertAlmostEqual(pcm_s16le_rms(pcm_frame(-1000, 320)), 1000.0)

    def test_rejects_partial_sample(self) -> None:
        with self.assertRaises(ValueError):
            pcm_s16le_rms(b"\x00")


class ChunkerTests(unittest.TestCase):
    def config(self, **overrides: object) -> VadConfig:
        values = dict(
            sample_rate_hz=1000,
            frame_duration_ms=10,
            threshold_rms=100.0,
            silence_duration_ms=30,
            min_speech_duration_ms=20,
            max_chunk_duration_ms=100,
            pre_roll_ms=10,
            overlap_ms=10,
        )
        values.update(overrides)
        return VadConfig(**values)

    def test_silence_does_not_emit(self) -> None:
        config = self.config()
        chunker = EnergyVadChunker(config)
        silence = pcm_frame(0, config.samples_per_frame)
        self.assertEqual(chunker.feed(silence * 20), [])
        self.assertEqual(chunker.flush(), [])

    def test_emits_after_configured_silence_with_source_times(self) -> None:
        config = self.config()
        chunker = EnergyVadChunker(config)
        frame = config.samples_per_frame
        silence = pcm_frame(0, frame)
        speech = pcm_frame(1000, frame)

        chunks = chunker.feed(silence + speech * 2 + silence * 3)

        self.assertEqual(len(chunks), 1)
        chunk = chunks[0]
        self.assertEqual(chunk.reason, "silence")
        self.assertEqual(chunk.start_ms(config.sample_rate_hz), 0.0)
        self.assertEqual(chunk.end_ms(config.sample_rate_hz), 60.0)
        self.assertEqual(chunk.speech_samples, 20)

    def test_short_noise_is_discarded(self) -> None:
        config = self.config(min_speech_duration_ms=30)
        chunker = EnergyVadChunker(config)
        frame = config.samples_per_frame
        chunks = chunker.feed(pcm_frame(1000, frame) + pcm_frame(0, frame) * 3)
        self.assertEqual(chunks, [])

    def test_max_duration_forces_chunk(self) -> None:
        config = self.config(max_chunk_duration_ms=50, pre_roll_ms=0)
        chunker = EnergyVadChunker(config)
        speech = pcm_frame(1000, config.samples_per_frame)

        chunks = chunker.feed(speech * 5)

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].reason, "max_duration")
        self.assertEqual(chunks[0].end_ms(config.sample_rate_hz), 50.0)

    def test_flush_finalizes_active_speech(self) -> None:
        config = self.config(pre_roll_ms=0)
        chunker = EnergyVadChunker(config)
        speech = pcm_frame(1000, config.samples_per_frame)
        self.assertEqual(chunker.feed(speech * 2), [])

        chunks = chunker.flush()

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].reason, "stop")

    def test_overlap_can_be_larger_than_preroll(self) -> None:
        config = self.config(pre_roll_ms=0, overlap_ms=20)
        chunker = EnergyVadChunker(config)
        speech = pcm_frame(1000, config.samples_per_frame)
        silence = pcm_frame(0, config.samples_per_frame)
        first = chunker.feed(speech * 2 + silence * 3)
        second = chunker.feed(speech * 2 + silence * 3)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].start_sample, first[0].end_sample - 20)

    def test_reset_restarts_the_sample_clock(self) -> None:
        config = self.config()
        chunker = EnergyVadChunker(config)
        frame = config.samples_per_frame
        chunker.feed(pcm_frame(1000, frame) * 5)
        self.assertEqual(chunker.next_sample_ms, 50.0)

        chunker.reset()

        # Paused wall time is not audio: leaving the clock advanced made every
        # later chunk report a timestamp inflated by all pre-pause audio.
        self.assertEqual(chunker.next_sample, 0)
        self.assertEqual(chunker.next_sample_ms, 0.0)
        chunks = chunker.feed(
            pcm_frame(0, frame) + pcm_frame(1000, frame) * 2 + pcm_frame(0, frame) * 3
        )
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].start_ms(config.sample_rate_hz), 0.0)

    def test_sample_clock_tracks_fed_audio(self) -> None:
        config = self.config()
        chunker = EnergyVadChunker(config)
        self.assertEqual(chunker.next_sample_ms, 0.0)
        chunker.feed(pcm_frame(0, config.samples_per_frame) * 3)
        self.assertEqual(chunker.next_sample_ms, 30.0)

    def test_accepts_arbitrary_input_boundaries(self) -> None:
        config = self.config(pre_roll_ms=0)
        chunker = EnergyVadChunker(config)
        speech = pcm_frame(1000, config.samples_per_frame)
        silence = pcm_frame(0, config.samples_per_frame)
        payload = speech * 2 + silence * 3
        results = []
        for byte in payload:
            results.extend(chunker.feed(bytes([byte])))
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
