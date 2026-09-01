from __future__ import annotations

import unittest

from voxink_core.models import Source
from voxink_core.diarization.alignment import ReconciliationPolicy, reconcile
from voxink_core.diarization.models import SpeakerTurn
from voxink_core.diarization.transcript import TranscriptBlock, block_digest


def block(start_ms: int, end_ms: int, *, source: Source = Source.MICROPHONE, index: int = 1):
    return TranscriptBlock(
        part="transcript-part-001.md",
        block=index,
        source=source,
        start_ms=start_ms,
        end_ms=end_ms,
        text="words",
        speaker=None,
        sha256=block_digest(f"{start_ms}-{end_ms}"),
        header=f"**[{start_ms}-{end_ms}]**",
    )


def turn(speaker: str, start_ms: int, end_ms: int, *, exclusive=True, source=Source.MICROPHONE):
    return SpeakerTurn(speaker, start_ms, end_ms, source, exclusive)


def label_one(block_, exclusive=(), raw=(), policy=None, source=Source.MICROPHONE):
    return reconcile(
        [block_],
        exclusive_turns=list(exclusive),
        raw_turns=list(raw),
        source=source,
        policy=policy,
    )[0]


class SingleSpeakerTests(unittest.TestCase):
    def test_one_speaker_covering_the_segment_is_labeled(self):
        result = label_one(block(0, 1000), exclusive=[turn("MIC_SPEAKER_00", 0, 1000)])
        self.assertEqual(result.speaker_id, "MIC_SPEAKER_00")
        self.assertEqual(result.speaker_coverage, 1.0)
        self.assertIsNone(result.reason)

    def test_vad_padding_does_not_prevent_a_confident_label(self):
        """The case the wall-clock rule would have thrown away.

        A five-second VAD segment holding three seconds of speech, almost all
        of it one person. Measured against attributed speech this is 0.967 and
        labeled; measured against wall-clock duration it is 0.58 and would have
        been discarded.
        """
        result = label_one(
            block(0, 5000),
            exclusive=[
                turn("MIC_SPEAKER_00", 0, 2900),
                turn("MIC_SPEAKER_01", 2900, 3000),
            ],
        )
        self.assertEqual(result.speaker_id, "MIC_SPEAKER_00")
        self.assertAlmostEqual(result.speaker_coverage, 2900 / 3000)
        self.assertAlmostEqual(result.segment_coverage, 2900 / 5000)
        self.assertLess(result.segment_coverage, 0.70)


class CoverageBoundaryTests(unittest.TestCase):
    def test_exactly_the_threshold_is_labeled(self):
        result = label_one(
            block(0, 1000),
            exclusive=[
                turn("MIC_SPEAKER_00", 0, 700),
                turn("MIC_SPEAKER_01", 700, 1000),
            ],
        )
        self.assertEqual(result.speaker_coverage, 0.70)
        self.assertEqual(result.speaker_id, "MIC_SPEAKER_00")

    def test_just_below_the_threshold_is_unlabeled(self):
        result = label_one(
            block(0, 1000),
            exclusive=[
                turn("MIC_SPEAKER_00", 0, 699),
                turn("MIC_SPEAKER_01", 699, 1000),
            ],
        )
        self.assertAlmostEqual(result.speaker_coverage, 0.699)
        self.assertIsNone(result.speaker_id)
        self.assertEqual(result.reason, "low_coverage")

    def test_threshold_is_policy_not_a_constant(self):
        blocks = block(0, 1000)
        turns = [turn("MIC_SPEAKER_00", 0, 600), turn("MIC_SPEAKER_01", 600, 1000)]
        strict = label_one(blocks, exclusive=turns)
        lenient = label_one(
            blocks, exclusive=turns, policy=ReconciliationPolicy(min_coverage=0.55)
        )
        self.assertIsNone(strict.speaker_id)
        self.assertEqual(lenient.speaker_id, "MIC_SPEAKER_00")

    def test_a_tie_is_never_labeled(self):
        result = label_one(
            block(0, 1000),
            exclusive=[
                turn("MIC_SPEAKER_00", 0, 500),
                turn("MIC_SPEAKER_01", 500, 1000),
            ],
        )
        self.assertIsNone(result.speaker_id)
        self.assertEqual(result.reason, "tie")


class InsufficientEvidenceTests(unittest.TestCase):
    def test_a_segment_with_no_speech_is_unlabeled(self):
        result = label_one(block(0, 1000), exclusive=[turn("MIC_SPEAKER_00", 5000, 6000)])
        self.assertIsNone(result.speaker_id)
        self.assertEqual(result.reason, "no_speech")
        self.assertEqual(result.attributed_ms, 0)

    def test_a_sliver_of_speech_cannot_label_a_long_segment(self):
        """Unanimous but nearly silent: 150ms of speech in five seconds."""
        result = label_one(block(0, 5000), exclusive=[turn("MIC_SPEAKER_00", 0, 150)])
        self.assertIsNone(result.speaker_id)
        self.assertEqual(result.reason, "insufficient_speech")
        self.assertEqual(result.speaker_coverage, 1.0)

    def test_the_absolute_floor_applies_to_short_segments(self):
        result = label_one(block(0, 300), exclusive=[turn("MIC_SPEAKER_00", 0, 100)])
        self.assertEqual(result.reason, "insufficient_speech")

    def test_enough_speech_in_a_short_segment_is_labeled(self):
        result = label_one(block(0, 300), exclusive=[turn("MIC_SPEAKER_00", 0, 250)])
        self.assertEqual(result.speaker_id, "MIC_SPEAKER_00")


class IntersectionTests(unittest.TestCase):
    def test_a_turn_touching_the_boundary_contributes_nothing(self):
        result = label_one(
            block(1000, 2000),
            exclusive=[turn("MIC_SPEAKER_00", 0, 1000), turn("MIC_SPEAKER_01", 2000, 3000)],
        )
        self.assertEqual(result.attributed_ms, 0)
        self.assertEqual(result.reason, "no_speech")

    def test_only_the_intersecting_portion_counts(self):
        result = label_one(
            block(1000, 2000), exclusive=[turn("MIC_SPEAKER_00", 0, 10_000)]
        )
        self.assertEqual(result.attributed_ms, 1000)
        self.assertEqual(result.speaker_id, "MIC_SPEAKER_00")

    def test_gaps_between_turns_are_not_attributed(self):
        result = label_one(
            block(0, 4000),
            exclusive=[turn("MIC_SPEAKER_00", 0, 1000), turn("MIC_SPEAKER_00", 3000, 4000)],
        )
        self.assertEqual(result.attributed_ms, 2000)
        self.assertEqual(result.speaker_id, "MIC_SPEAKER_00")

    def test_multiple_turns_from_one_speaker_accumulate(self):
        result = label_one(
            block(0, 1000),
            exclusive=[
                turn("MIC_SPEAKER_00", 0, 300),
                turn("MIC_SPEAKER_00", 400, 800),
                turn("MIC_SPEAKER_01", 800, 900),
            ],
        )
        self.assertEqual(result.attributed_ms, 800)
        self.assertEqual(result.speaker_id, "MIC_SPEAKER_00")

    def test_invalid_block_range_is_unlabeled(self):
        result = label_one(block(1000, 1000), exclusive=[turn("MIC_SPEAKER_00", 0, 5000)])
        self.assertIsNone(result.speaker_id)
        self.assertEqual(result.reason, "invalid_range")


class OverlapDiagnosticTests(unittest.TestCase):
    def test_two_raw_speakers_in_a_segment_set_the_flag(self):
        result = label_one(
            block(0, 1000),
            exclusive=[turn("MIC_SPEAKER_00", 0, 1000)],
            raw=[
                turn("MIC_SPEAKER_00", 0, 1000, exclusive=False),
                turn("MIC_SPEAKER_01", 400, 700, exclusive=False),
            ],
        )
        self.assertTrue(result.overlap_detected)
        # Overlap is a diagnostic, not a veto: the exclusive view is still
        # unambiguous, so the label stands.
        self.assertEqual(result.speaker_id, "MIC_SPEAKER_00")

    def test_a_single_raw_speaker_does_not_set_the_flag(self):
        result = label_one(
            block(0, 1000),
            exclusive=[turn("MIC_SPEAKER_00", 0, 1000)],
            raw=[turn("MIC_SPEAKER_00", 0, 1000, exclusive=False)],
        )
        self.assertFalse(result.overlap_detected)

    def test_a_momentary_intrusion_is_below_the_threshold(self):
        result = label_one(
            block(0, 1000),
            exclusive=[turn("MIC_SPEAKER_00", 0, 1000)],
            raw=[
                turn("MIC_SPEAKER_00", 0, 1000, exclusive=False),
                turn("MIC_SPEAKER_01", 500, 550, exclusive=False),
            ],
        )
        self.assertFalse(result.overlap_detected)

    def test_nested_raw_turns_are_detected(self):
        result = label_one(
            block(0, 10_000),
            exclusive=[turn("MIC_SPEAKER_00", 0, 10_000)],
            raw=[
                turn("MIC_SPEAKER_00", 0, 10_000, exclusive=False),
                turn("MIC_SPEAKER_01", 2000, 3000, exclusive=False),
                turn("MIC_SPEAKER_02", 2200, 2800, exclusive=False),
            ],
        )
        self.assertTrue(result.overlap_detected)

    def test_overlap_is_reported_even_when_the_segment_is_unlabeled(self):
        result = label_one(
            block(0, 1000),
            exclusive=[
                turn("MIC_SPEAKER_00", 0, 500),
                turn("MIC_SPEAKER_01", 500, 1000),
            ],
            raw=[
                turn("MIC_SPEAKER_00", 0, 600, exclusive=False),
                turn("MIC_SPEAKER_01", 400, 1000, exclusive=False),
            ],
        )
        self.assertIsNone(result.speaker_id)
        self.assertTrue(result.overlap_detected)


class SourceIsolationTests(unittest.TestCase):
    def test_blocks_from_another_source_are_returned_unlabeled(self):
        result = label_one(
            block(0, 1000, source=Source.SYSTEM),
            exclusive=[turn("MIC_SPEAKER_00", 0, 1000)],
        )
        self.assertIsNone(result.speaker_id)
        self.assertEqual(result.reason, "other_source")

    def test_turns_from_another_source_are_ignored(self):
        result = label_one(
            block(0, 1000),
            exclusive=[
                turn("SYSTEM_SPEAKER_00", 0, 1000, source=Source.SYSTEM),
                turn("MIC_SPEAKER_00", 0, 400),
            ],
        )
        self.assertEqual(result.attributed_ms, 400)
        self.assertEqual(result.speaker_id, "MIC_SPEAKER_00")

    def test_every_block_is_returned_so_the_view_stays_complete(self):
        labels = reconcile(
            [block(0, 1000), block(1000, 2000, source=Source.SYSTEM, index=2)],
            exclusive_turns=[turn("MIC_SPEAKER_00", 0, 1000)],
            raw_turns=[],
            source=Source.MICROPHONE,
        )
        self.assertEqual(len(labels), 2)


class AlternatingSpeakerTests(unittest.TestCase):
    def test_each_segment_gets_its_own_speaker(self):
        blocks = [block(0, 1000, index=1), block(1000, 2000, index=2), block(2000, 3000, index=3)]
        labels = reconcile(
            blocks,
            exclusive_turns=[
                turn("MIC_SPEAKER_00", 0, 1000),
                turn("MIC_SPEAKER_01", 1000, 2000),
                turn("MIC_SPEAKER_00", 2000, 3000),
            ],
            raw_turns=[],
            source=Source.MICROPHONE,
        )
        self.assertEqual(
            [label.speaker_id for label in labels],
            ["MIC_SPEAKER_00", "MIC_SPEAKER_01", "MIC_SPEAKER_00"],
        )

    def test_reconciliation_is_deterministic(self):
        blocks = [block(0, 1000, index=1), block(1000, 2000, index=2)]
        turns = [turn("MIC_SPEAKER_01", 0, 900), turn("MIC_SPEAKER_00", 1000, 1900)]
        first = reconcile(blocks, exclusive_turns=turns, raw_turns=[], source=Source.MICROPHONE)
        second = reconcile(
            blocks, exclusive_turns=list(reversed(turns)), raw_turns=[], source=Source.MICROPHONE
        )
        self.assertEqual([l.speaker_id for l in first], [l.speaker_id for l in second])


class PolicyValidationTests(unittest.TestCase):
    def test_rejects_impossible_thresholds(self):
        for kwargs in (
            {"min_coverage": 0.0},
            {"min_coverage": 1.5},
            {"min_attributed_ratio": -0.1},
            {"min_attributed_ms": -1},
            {"overlap_min_ms": -1},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    ReconciliationPolicy(**kwargs)


if __name__ == "__main__":
    unittest.main()
