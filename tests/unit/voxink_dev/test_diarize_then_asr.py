from __future__ import annotations

import unittest

from voxink_dev.diarize_then_asr import Clip, Turn, group_turns, load_turns


def turn(speaker: str, start_ms: int, end_ms: int) -> Turn:
    return Turn(speaker, start_ms, end_ms)


class GroupTurnsTests(unittest.TestCase):
    def test_consecutive_turns_of_one_speaker_within_the_gap_become_one_clip(self):
        clips = group_turns(
            [turn("A", 1_000, 2_000), turn("A", 2_300, 3_000), turn("A", 5_000, 6_000)],
            total_ms=10_000,
            merge_gap_ms=500,
            pad_ms=0,
        )
        self.assertEqual(clips, (Clip("A", 1_000, 3_000, 2), Clip("A", 5_000, 6_000, 1)))

    def test_a_speaker_change_always_starts_a_new_clip(self):
        clips = group_turns(
            [turn("A", 0, 1_000), turn("B", 1_000, 2_000), turn("A", 2_000, 3_000)],
            total_ms=3_000,
            pad_ms=0,
        )
        self.assertEqual([c.speaker_id for c in clips], ["A", "B", "A"])

    def test_padding_stops_at_the_midpoint_of_the_gap_to_a_neighbour(self):
        clips = group_turns(
            [turn("A", 1_000, 2_000), turn("B", 2_100, 3_000)],
            total_ms=10_000,
            pad_ms=150,
        )
        self.assertEqual(clips[0].end_ms, 2_050)
        self.assertEqual(clips[1].start_ms, 2_050)
        self.assertEqual(clips[0].start_ms, 850)
        self.assertEqual(clips[1].end_ms, 3_150)

    def test_padding_is_clamped_to_the_recording(self):
        clips = group_turns([turn("A", 50, 9_950)], total_ms=10_000, pad_ms=150)
        self.assertEqual(clips, (Clip("A", 0, 10_000, 1),))

    def test_overlapping_turns_never_produce_a_clip_that_starts_after_it_ends(self):
        clips = group_turns(
            [turn("A", 0, 1_200), turn("B", 1_000, 2_000)],
            total_ms=3_000,
            pad_ms=150,
        )
        self.assertEqual(clips[0].end_ms, 1_200)
        self.assertEqual(clips[1].start_ms, 1_000)

    def test_short_clips_are_dropped(self):
        clips = group_turns([turn("A", 0, 200), turn("B", 1_000, 2_000)], total_ms=3_000, pad_ms=0, min_ms=300)
        self.assertEqual([c.speaker_id for c in clips], ["B"])

    def test_a_long_stretch_is_cut_at_a_turn_boundary(self):
        turns = [turn("A", i * 1_000, i * 1_000 + 900) for i in range(40)]
        clips = group_turns(turns, total_ms=40_000, pad_ms=0, max_ms=30_000)
        self.assertGreater(len(clips), 1)
        for clip in clips:
            self.assertLessEqual(clip.end_ms - clip.start_ms, 30_000)
        self.assertEqual(sum(c.turn_count for c in clips), 40)

    def test_turns_are_ordered_before_grouping(self):
        clips = group_turns([turn("A", 2_000, 3_000), turn("A", 0, 1_000)], total_ms=3_000, pad_ms=0, merge_gap_ms=100)
        self.assertEqual([c.start_ms for c in clips], [0, 2_000])


class LoadTurnsTests(unittest.TestCase):
    def test_reads_the_exclusive_turns_of_a_speaker_document(self):
        document = {
            "exclusive_turns": [{"speaker_id": "MIC_SPEAKER_00", "start_ms": 5, "end_ms": 10, "exclusive": True}],
            "raw_turns": [{"speaker_id": "MIC_SPEAKER_00", "start_ms": 0, "end_ms": 20}],
        }
        self.assertEqual(load_turns(document), (Turn("MIC_SPEAKER_00", 5, 10),))


if __name__ == "__main__":
    unittest.main()
