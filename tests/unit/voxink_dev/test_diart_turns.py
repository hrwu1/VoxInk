from __future__ import annotations

import itertools
import random
import unittest

from voxink_dev.diart_turns import Span, SpanSmoother, TurnBuffer, context_window, partition


class PartitionTests(unittest.TestCase):
    def test_overlap_is_one_interval_with_both_labels_not_two_audio_clips(self):
        spans = partition(0, 2_000, [Span(0, 1_200, ("A",)), Span(1_000, 2_000, ("B",))])
        self.assertEqual(spans, [
            Span(0, 1_000, ("A",)),
            Span(1_000, 1_200, ("A", "B")),
            Span(1_200, 2_000, ("B",)),
        ])
        self.assertEqual(sum(s.end_ms - s.start_ms for s in spans), 2_000)

    def test_unassigned_audio_before_between_and_after_tracks_is_preserved(self):
        self.assertEqual(partition(0, 1_000, [Span(100, 200, ("A",)), Span(300, 900, ("B",))]), [
            Span(0, 100, ()), Span(100, 200, ("A",)), Span(200, 300, ()),
            Span(300, 900, ("B",)), Span(900, 1_000, ()),
        ])
        self.assertEqual(partition(0, 1_000, []), [Span(0, 1_000, ())])

    def test_clips_tracks_at_window_edges_without_leaking_outside_speakers(self):
        self.assertEqual(partition(100, 300, [
            Span(0, 100, ("outside-left",)), Span(50, 200, ("A",)),
            Span(200, 350, ("B",)), Span(300, 400, ("outside-right",)),
        ]), [Span(100, 200, ("A",)), Span(200, 300, ("B",))])

    def test_track_order_and_duplicate_labels_do_not_change_partition(self):
        tracks = [Span(0, 300, ("B",)), Span(100, 200, ("A",)), Span(150, 250, ("B",))]
        expected = [Span(0, 100, ("B",)), Span(100, 200, ("A", "B")), Span(200, 300, ("B",))]
        for permutation in itertools.permutations(tracks):
            with self.subTest(tracks=permutation):
                self.assertEqual(partition(0, 300, iter(permutation)), expected)

    def test_random_overlaps_cover_every_millisecond_exactly_once(self):
        rng = random.Random(46)
        for _ in range(100):
            tracks = []
            for _ in range(rng.randrange(12)):
                start = rng.randrange(100)
                tracks.append(Span(start, rng.randrange(start + 1, 101), (rng.choice("ABC"),)))
            spans = partition(0, 100, tracks)
            for ms in range(100):
                actual = [s.speakers for s in spans if s.start_ms <= ms < s.end_ms]
                expected = tuple(sorted({label for t in tracks if t.start_ms <= ms < t.end_ms for label in t.speakers}))
                self.assertEqual(actual, [expected], (ms, tracks, spans))

    def test_zero_width_window_emits_no_audio(self):
        self.assertEqual(partition(100, 100, [Span(0, 200, ("A",))]), [])

    def test_step_edge_rounding_gaps_merge_into_one_contiguous_speaker_turn(self):
        buffer = TurnBuffer()
        ready = []
        for start in (0, 500, 1_000):
            spans = partition(start, start + 500, [Span(start + 8, start + 492, ("A",))], edge_tolerance_ms=10)
            for span in spans:
                ready.extend(buffer.feed(span))
        self.assertEqual(ready + buffer.flush(), [Span(0, 1_500, ("A",))])

    def test_edge_snapping_preserves_internal_eighty_millisecond_interjection(self):
        spans = partition(0, 500, [
            Span(8, 220, ("A",)), Span(220, 300, ("B",)), Span(300, 492, ("A",)),
        ], edge_tolerance_ms=10)
        self.assertEqual(spans, [Span(0, 220, ("A",)), Span(220, 300, ("B",)), Span(300, 500, ("A",))])

    def test_edge_tolerance_does_not_fill_internal_small_unknown_gaps(self):
        self.assertEqual(partition(0, 500, [Span(8, 200, ("A",)), Span(208, 492, ("A",))], edge_tolerance_ms=10), [
            Span(0, 200, ("A",)), Span(200, 208, ()), Span(208, 500, ("A",)),
        ])

    def test_tolerance_never_extends_beyond_step_or_imports_outside_speech(self):
        self.assertEqual(partition(100, 600, [
            Span(90, 100, ("outside-left",)), Span(95, 200, ("A",)),
            Span(400, 608, ("B",)), Span(600, 610, ("outside-right",)),
        ], edge_tolerance_ms=10), [Span(100, 200, ("A",)), Span(200, 400, ()), Span(400, 600, ("B",))])

    def test_default_zero_tolerance_preserves_original_edge_gaps(self):
        self.assertEqual(partition(0, 500, [Span(8, 492, ("A",))]), [
            Span(0, 8, ()), Span(8, 492, ("A",)), Span(492, 500, ()),
        ])

    def test_negative_edge_tolerance_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            partition(0, 500, [Span(8, 492, ("A",))], edge_tolerance_ms=-1)


class TurnBufferTests(unittest.TestCase):
    def test_eighty_millisecond_interjection_survives_between_longer_turns(self):
        buffer = TurnBuffer()
        self.assertEqual(buffer.feed(Span(0, 2_000, ("A",))), [])
        first = buffer.feed(Span(2_000, 2_080, ("B",)))
        second = buffer.feed(Span(2_080, 4_000, ("A",)))
        self.assertEqual(first + second + buffer.flush(), [
            Span(0, 2_000, ("A",)), Span(2_000, 2_080, ("B",)), Span(2_080, 4_000, ("A",)),
        ])

    def test_unknown_and_overlap_are_distinct_buffers_without_losing_audio(self):
        inputs = [Span(0, 100, ()), Span(100, 500, ("A",)), Span(500, 580, ("A", "B")), Span(580, 900, ("A",))]
        buffer = TurnBuffer()
        output = [ready for span in inputs for ready in buffer.feed(span)] + buffer.flush()
        self.assertEqual(output, inputs)
        self.assertEqual(sum(s.end_ms - s.start_ms for s in output), 900)

    def test_adjacent_windows_of_one_speaker_merge_until_maximum(self):
        buffer = TurnBuffer(max_ms=1_000)
        self.assertEqual(buffer.feed(Span(0, 700, ("A",))), [])
        self.assertEqual(buffer.feed(Span(700, 1_500, ("A",))), [Span(0, 1_000, ("A",))])
        self.assertEqual(buffer.flush(), [Span(1_000, 1_500, ("A",))])

    def test_long_input_is_bounded_and_exact_boundary_has_no_empty_tail(self):
        for duration in (999, 1_000, 1_001, 4_000, 4_080):
            with self.subTest(duration=duration):
                buffer = TurnBuffer(max_ms=1_000)
                output = buffer.feed(Span(0, duration, ("A",)))
                if buffer.pending:
                    self.assertLess(buffer.pending.end_ms - buffer.pending.start_ms, 1_000)
                output += buffer.flush()
                self.assertTrue(all(0 < s.end_ms - s.start_ms <= 1_000 for s in output))
                self.assertEqual(output[0].start_ms, 0)
                self.assertEqual(output[-1].end_ms, duration)
                self.assertTrue(all(a.end_ms == b.start_ms for a, b in zip(output, output[1:])))
                self.assertEqual(sum(s.end_ms - s.start_ms for s in output), duration)

    def test_replayed_reordered_or_missing_input_fails_without_mutating_buffer(self):
        buffer = TurnBuffer()
        buffer.feed(Span(0, 1_000, ("A",)))
        for invalid in (Span(0, 1_000, ("A",)), Span(900, 1_100, ("B",)), Span(1_001, 1_100, ("B",))):
            with self.subTest(span=invalid), self.assertRaisesRegex(ValueError, "contiguous and monotonic"):
                buffer.feed(invalid)
            self.assertEqual(buffer.cursor_ms, 1_000)
            self.assertEqual(buffer.pending, Span(0, 1_000, ("A",)))
        self.assertEqual(buffer.feed(Span(1_000, 1_080, ("B",))), [Span(0, 1_000, ("A",))])
        self.assertEqual(buffer.flush(), [Span(1_000, 1_080, ("B",))])

    def test_nonzero_initial_time_requires_caller_to_rebase_input(self):
        with self.assertRaisesRegex(ValueError, "contiguous and monotonic"):
            TurnBuffer().feed(Span(8_700_000, 8_701_000, ("A",)))

    def test_eof_flush_retains_final_interjection_and_is_idempotent(self):
        buffer = TurnBuffer()
        buffer.feed(Span(0, 80, ("B",)))
        self.assertEqual(buffer.flush(), [Span(0, 80, ("B",))])
        self.assertEqual(buffer.flush(), [])
        self.assertEqual(buffer.cursor_ms, 80)

    def test_invalid_maximum_rejected(self):
        for value in (0, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TurnBuffer(max_ms=value)


class ContextWindowTests(unittest.TestCase):
    def test_minimum_context_expands_even_with_zero_explicit_padding(self):
        # Expanded audio may contain neighbours; caller must compare these
        # bounds with the turn and mark attribution ambiguous even at pad=0.
        span = Span(1_000, 1_080, ("B",))
        start, end = context_window(span, 3_000, pad_ms=0, min_ms=2_000)
        self.assertLess(start, span.start_ms)
        self.assertGreater(end, span.end_ms)
        self.assertEqual(end - start, 2_000)

    def test_short_recording_uses_only_available_samples(self):
        self.assertEqual(context_window(Span(0, 80, ("B",)), 80), (0, 80))

    def test_padding_cannot_read_before_start_or_after_eof(self):
        for span in (Span(0, 80, ("A",)), Span(2_920, 3_000, ("A",))):
            with self.subTest(span=span):
                start, end = context_window(span, 3_000)
                self.assertGreaterEqual(start, 0)
                self.assertLessEqual(end, 3_000)
                self.assertLessEqual(start, span.start_ms)
                self.assertGreaterEqual(end, span.end_ms)
                self.assertEqual(end - start, 2_000)

    def test_context_never_shrinks_or_relabels_the_original_turn(self):
        span = Span(1_000, 5_000, ("A", "B"))
        self.assertEqual(context_window(span, 8_000, pad_ms=250, min_ms=2_000), (750, 5_250))
        self.assertEqual(span, Span(1_000, 5_000, ("A", "B")))
        self.assertEqual(context_window(span, 8_000, pad_ms=0, min_ms=0), (1_000, 5_000))

    def test_unavailable_audio_and_negative_context_settings_are_rejected(self):
        span = Span(500, 1_000, ("A",))
        for kwargs in ({"available_ms": 999}, {"available_ms": 1_000, "pad_ms": -1},
                       {"available_ms": 1_000, "min_ms": -1}, {"available_ms": 1_000, "floor_ms": -1},
                       {"available_ms": 1_000, "floor_ms": 1_000}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                context_window(span, **kwargs)

    def test_a_floor_keeps_the_window_off_audio_a_previous_turn_already_carried(self):
        span = Span(1_000, 1_200, ("A",))
        self.assertEqual(context_window(span, 8_000, floor_ms=1_000), (1_000, 3_000))
        self.assertEqual(context_window(span, 8_000, min_ms=0, floor_ms=1_000), (1_000, 1_450))
        # Without a floor the same short turn reaches back over its predecessor.
        self.assertEqual(context_window(span, 8_000, min_ms=0), (750, 1_450))
        self.assertEqual(context_window(span, 8_000, floor_ms=1_150), (1_150, 3_150))


class SpanSmootherTests(unittest.TestCase):
    def smooth(self, spans, **kwargs):
        smoother = SpanSmoother(**kwargs)
        result = [out for span in spans for out in smoother.feed(span)]
        return result + smoother.flush()

    def test_a_brief_gap_inside_one_speakers_turn_becomes_part_of_that_turn(self):
        self.assertEqual(self.smooth([
            Span(0, 1_000, ("A",)), Span(1_000, 1_400, ()), Span(1_400, 2_000, ("A",)),
        ]), [Span(0, 1_000, ("A",)), Span(1_000, 1_400, ("A",)), Span(1_400, 2_000, ("A",))])

    def test_a_brief_gap_between_two_speakers_ends_the_earlier_turn(self):
        self.assertEqual(self.smooth([
            Span(0, 1_000, ("A",)), Span(1_000, 1_400, ()), Span(1_400, 2_000, ("B",)),
        ]), [Span(0, 1_000, ("A",)), Span(1_000, 1_400, ("A",)), Span(1_400, 2_000, ("B",))])

    def test_silence_longer_than_a_pause_stays_unassigned_and_only_its_start_waits(self):
        gap = [Span(1_000 + step * 500, 1_500 + step * 500, ()) for step in range(4)]
        self.assertEqual(self.smooth([Span(0, 1_000, ("A",)), *gap, Span(3_000, 4_000, ("B",))]), [
            Span(0, 1_000, ("A",)), Span(1_000, 2_000, ()), Span(2_000, 2_500, ()),
            Span(2_500, 3_000, ()), Span(3_000, 4_000, ("B",)),
        ])

    def test_leading_silence_and_overlap_labels_pass_through_unchanged(self):
        spans = [Span(0, 900, ()), Span(900, 1_200, ("A", "B")), Span(1_200, 1_400, ("B",))]
        self.assertEqual(self.smooth(spans), spans)

    def test_output_stays_a_contiguous_partition_of_the_input(self):
        spans = [Span(0, 400, ("A",)), Span(400, 700, ()), Span(700, 900, ("A",)), Span(900, 1_000, ()),
                 Span(1_000, 1_100, ("B",)), Span(1_100, 2_000, ()), Span(2_000, 2_400, ("B",))]
        result = self.smooth(spans)
        self.assertEqual((result[0].start_ms, result[-1].end_ms), (0, 2_400))
        self.assertTrue(all(a.end_ms == b.start_ms for a, b in zip(result, result[1:])))
        self.assertEqual(sum(s.end_ms - s.start_ms for s in result), 2_400)

    def test_input_must_be_contiguous_and_the_pause_bound_positive(self):
        smoother = SpanSmoother()
        smoother.feed(Span(0, 500, ("A",)))
        with self.assertRaisesRegex(ValueError, "contiguous"):
            smoother.feed(Span(600, 900, ("A",)))
        with self.assertRaises(ValueError):
            SpanSmoother(max_pause_ms=0)

    def test_a_pending_gap_is_emitted_with_its_turn_when_the_source_ends(self):
        smoother = SpanSmoother()
        self.assertEqual(smoother.feed(Span(0, 1_000, ("A",))), [Span(0, 1_000, ("A",))])
        self.assertEqual(smoother.feed(Span(1_000, 1_200, ())), [])
        self.assertEqual(smoother.flush(), [Span(1_000, 1_200, ("A",))])
        self.assertEqual(smoother.flush(), [])


if __name__ == "__main__":
    unittest.main()
