"""Level behaviour of the speech gate.

`test_vad.py` covers chunking mechanics against a pinned gate. This file covers
the question that broke a real session: at what input level does speech reach
the model at all.

The failure being guarded against: a microphone-only session peaked at
-19.8 dBFS, only 6.2% of its frames cleared a gate fixed at RMS 500, and the
live transcript lost words that the same audio produced when transcribed as one
file. RMS 500 is -36.3 dBFS, which is above much of ordinary speech once no
automatic gain control is applied.
"""

from __future__ import annotations

import unittest

from voxink_core.vad import EnergyVadChunker, VadConfig
from tests.fixtures import audio

SAMPLES_PER_FRAME = audio.SAMPLE_RATE_HZ * audio.FRAME_MS // 1000

# The gate a client used to pin, kept as the regression baseline.
LEGACY_THRESHOLD_RMS = 500.0


def chunk_all(pcm: bytes, config: VadConfig | None = None):
    chunker = EnergyVadChunker(config or VadConfig())
    chunks = chunker.feed(pcm)
    chunks.extend(chunker.flush())
    return chunker, chunks


def speech_frame_recall(pcm: bytes, chunks, *, reference_rms: float) -> float:
    """Fraction of genuinely-speech frames that land inside some chunk.

    This is the measurement the transcript failure actually made. Energy
    coverage is not: it is dominated by loud vowels, and stays high even when
    every quiet word between them has been dropped.
    """
    frames = audio.frames_of(pcm)
    covered: set[int] = set()
    for chunk in chunks:
        first = chunk.start_sample // SAMPLES_PER_FRAME
        last = -(-chunk.end_sample // SAMPLES_PER_FRAME)
        covered.update(range(first, min(len(frames), last)))
    speech = [index for index, frame in enumerate(frames) if audio.rms_of(frame) >= reference_rms]
    if not speech:
        raise AssertionError("fixture contains no speech frames above the reference level")
    return sum(1 for index in speech if index in covered) / len(speech)


class BackgroundNeverOpensTheGate(unittest.TestCase):
    """Whatever the background level, it must not be transcribed as speech.

    Lowering the gate sends more near-silence to the model, and ASR models
    generally emit text for near-silent input rather than nothing, so this is
    the half of the fix that recall cannot measure.
    """

    def test_digital_silence_produces_nothing(self) -> None:
        _, chunks = chunk_all(audio.silence(6_000))
        self.assertEqual(chunks, [])

    def test_room_tone_produces_nothing_at_any_level(self) -> None:
        for level in (-70.0, -60.0, -45.0, -35.0):
            with self.subTest(room_tone_dbfs=level):
                _, chunks = chunk_all(audio.room_tone(6_000, rms_dbfs=level))
                self.assertEqual(chunks, [])

    def test_background_after_silence_does_not_open_the_gate(self) -> None:
        """A gap of true silence must not collapse the gate behind it.

        A running average would follow digital silence down and then let the
        room back through as speech while it recovered.
        """
        stream = audio.room_tone(2_000) + audio.silence(3_000) + audio.room_tone(3_000)
        _, chunks = chunk_all(stream)
        self.assertEqual(chunks, [])

    def test_silence_is_not_evidence_about_the_room(self) -> None:
        chunker, _ = chunk_all(audio.room_tone(2_000) + audio.silence(4_000))
        floor = chunker.noise_floor_rms
        self.assertIsNotNone(floor)
        # Still describing the room tone, not the silence that followed it.
        self.assertGreater(floor, audio.dbfs_to_amplitude(-70.0))


class QuietSpeechSurvives(unittest.TestCase):
    def setUp(self) -> None:
        self.speech = audio.load_speech_sample(peak_dbfs_level=-19.8)
        self.stream = audio.room_tone(500) + self.speech + audio.room_tone(800)

    def test_the_recorded_failure_reproduces_with_the_old_fixed_gate(self) -> None:
        """Guards the baseline: without this, the fix below proves nothing."""
        _, chunks = chunk_all(self.stream, VadConfig(threshold_rms=LEGACY_THRESHOLD_RMS))
        recall = speech_frame_recall(self.stream, chunks, reference_rms=100.0)
        self.assertLess(recall, 0.9, "the fixed gate no longer loses speech; baseline is stale")

    def test_the_adaptive_gate_keeps_the_speech_the_fixed_gate_lost(self) -> None:
        _, chunks = chunk_all(self.stream)
        recall = speech_frame_recall(self.stream, chunks, reference_rms=100.0)
        self.assertGreaterEqual(recall, 0.99)

    def test_the_utterance_is_not_fragmented(self) -> None:
        """Splitting one sentence into several chunks is how words go missing.

        Audio between an emitted chunk and the next activation is not carried
        anywhere: only `overlap_ms` is retained, and the rest rolls out of the
        pre-roll buffer.
        """
        _, fixed = chunk_all(self.stream, VadConfig(threshold_rms=LEGACY_THRESHOLD_RMS))
        _, adaptive = chunk_all(self.stream)
        self.assertLess(len(adaptive), len(fixed))

    def test_speech_survives_across_input_levels(self) -> None:
        for level in (-6.0, -12.0, -20.0, -30.0):
            with self.subTest(peak_dbfs=level):
                speech = audio.load_speech_sample(peak_dbfs_level=level)
                stream = audio.room_tone(500) + speech + audio.room_tone(800)
                _, chunks = chunk_all(stream)
                reference = max(60.0, audio.rms_of(speech) * 0.5)
                recall = speech_frame_recall(stream, chunks, reference_rms=reference)
                self.assertGreaterEqual(recall, 0.99)

    def test_speech_survives_without_a_lead_in(self) -> None:
        """A session that starts mid-sentence has no background to measure yet."""
        _, chunks = chunk_all(self.speech)
        recall = speech_frame_recall(self.speech, chunks, reference_rms=100.0)
        self.assertGreaterEqual(recall, 0.99)

    def test_a_long_utterance_cannot_raise_the_gate_behind_itself(self) -> None:
        """Speech is the top of the distribution, so it must not become the floor."""
        loud = audio.load_speech_sample(peak_dbfs_level=-6.0)
        chunker, _ = chunk_all(audio.room_tone(500) + loud)
        floor = chunker.noise_floor_rms
        self.assertIsNotNone(floor)
        self.assertLess(floor, audio.rms_of(loud) * 0.5)


class GateConfiguration(unittest.TestCase):
    def test_the_gate_is_not_a_fixed_absolute_level_by_default(self) -> None:
        """The regression this whole file exists to prevent."""
        self.assertIsNone(VadConfig().threshold_rms)

    def test_the_gate_tracks_the_background_it_is_given(self) -> None:
        quiet, _ = chunk_all(audio.room_tone(3_000, rms_dbfs=-60.0))
        loud, _ = chunk_all(audio.room_tone(3_000, rms_dbfs=-40.0))
        self.assertLess(quiet.threshold_rms, loud.threshold_rms)

    def test_an_active_utterance_is_held_by_a_lower_gate(self) -> None:
        """Quiet word endings must not be judged at the level that opened the gate."""
        chunker = EnergyVadChunker(VadConfig())
        chunker.feed(audio.room_tone(2_000))
        idle = chunker.threshold_rms
        chunker.feed(audio.speech_like(peak_dbfs_level=-12.0))
        self.assertTrue(chunker.active)
        self.assertLess(chunker.threshold_rms, idle)

    def test_a_pinned_threshold_disables_adaptation_entirely(self) -> None:
        chunker = EnergyVadChunker(VadConfig(threshold_rms=250.0))
        chunker.feed(audio.room_tone(3_000, rms_dbfs=-35.0))
        self.assertEqual(chunker.threshold_rms, 250.0)

    def test_rejects_incoherent_gate_settings(self) -> None:
        for kwargs in (
            {"threshold_rms": -1.0},
            {"noise_floor_ratio": 0.5},
            {"absolute_floor_rms": -1.0},
            {"release_ratio": 0.0},
            {"release_ratio": 1.5},
            {"noise_floor_percentile": 0.0},
            {"noise_floor_percentile": 100.0},
            {"noise_floor_window_ms": 5},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    VadConfig(**kwargs)


class BackgroundEstimateIsBounded(unittest.TestCase):
    def test_the_window_does_not_grow_with_session_length(self) -> None:
        """A session runs for hours; the estimator must not accumulate."""
        chunker = EnergyVadChunker(VadConfig())
        chunker.feed(audio.room_tone(30_000))
        expected = VadConfig().noise_floor_window_ms // VadConfig().frame_duration_ms
        self.assertLessEqual(len(chunker._background), expected)

    def test_pausing_keeps_what_was_learned_about_the_room(self) -> None:
        chunker = EnergyVadChunker(VadConfig())
        chunker.feed(audio.room_tone(3_000))
        before = chunker.noise_floor_rms
        chunker.reset()
        self.assertEqual(chunker.noise_floor_rms, before)


if __name__ == "__main__":
    unittest.main()
