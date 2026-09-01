from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest

from voxink_core.models import Source
from voxink_core.diarization.models import (
    DiarizationError,
    DiarizationErrorCode,
    DiarizationRequest,
    DiarizationResult,
    MAX_DIAGNOSTIC_CHARS,
    SpeakerTurn,
    normalize_turns,
    seconds_to_ms,
    sort_turns,
    speaker_label,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class SecondsToMillisecondsTests(unittest.TestCase):
    def test_rounds_halves_upward_not_to_even(self):
        # The contrast with Python's half-to-even `round` is the whole reason
        # this function exists, so it is asserted directly.
        self.assertEqual(seconds_to_ms(0.0005), 1)
        self.assertEqual(round(0.0005 * 1000), 0)
        self.assertEqual(seconds_to_ms(0.0025), 3)
        self.assertEqual(round(0.0025 * 1000), 2)
        self.assertEqual(seconds_to_ms(1.2345), 1235)
        self.assertEqual(round(1.2345 * 1000), 1234)

    def test_rounds_below_half_downward(self):
        self.assertEqual(seconds_to_ms(0.0004), 0)
        self.assertEqual(seconds_to_ms(0.4994), 499)

    def test_converts_exact_values(self):
        self.assertEqual(seconds_to_ms(0.0), 0)
        self.assertEqual(seconds_to_ms(1.0), 1000)
        self.assertEqual(seconds_to_ms(14.62784375), 14628)

    def test_rejects_non_finite_timestamps(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                seconds_to_ms(value)


class SpeakerLabelTests(unittest.TestCase):
    def test_labels_are_source_scoped_and_zero_padded(self):
        self.assertEqual(speaker_label(Source.MICROPHONE, 0), "MIC_SPEAKER_00")
        self.assertEqual(speaker_label(Source.MICROPHONE, 7), "MIC_SPEAKER_07")
        self.assertEqual(speaker_label(Source.SYSTEM, 1), "SYSTEM_SPEAKER_01")

    def test_padding_widens_rather_than_truncating(self):
        self.assertEqual(speaker_label(Source.MICROPHONE, 123), "MIC_SPEAKER_123")

    def test_rejects_negative_index(self):
        with self.assertRaises(ValueError):
            speaker_label(Source.MICROPHONE, -1)


class SpeakerTurnTests(unittest.TestCase):
    def _turn(self, **overrides):
        values = dict(
            speaker_id="MIC_SPEAKER_00",
            start_ms=100,
            end_ms=200,
            source=Source.MICROPHONE,
            exclusive=True,
        )
        values.update(overrides)
        return SpeakerTurn(**values)

    def test_rejects_zero_length_turn(self):
        with self.assertRaises(ValueError):
            self._turn(start_ms=100, end_ms=100)

    def test_rejects_reversed_and_negative_timestamps(self):
        with self.assertRaises(ValueError):
            self._turn(start_ms=200, end_ms=100)
        with self.assertRaises(ValueError):
            self._turn(start_ms=-1, end_ms=100)

    def test_rejects_boolean_timestamps(self):
        with self.assertRaises(TypeError):
            self._turn(start_ms=True)

    def test_requires_a_speaker_id(self):
        with self.assertRaises(ValueError):
            self._turn(speaker_id="")

    def test_duration_and_overlap(self):
        turn = self._turn(start_ms=1000, end_ms=4000)
        self.assertEqual(turn.duration_ms, 3000)
        self.assertEqual(turn.overlap_ms(0, 10_000), 3000)
        self.assertEqual(turn.overlap_ms(2000, 3000), 1000)
        self.assertEqual(turn.overlap_ms(3500, 9000), 500)

    def test_overlap_of_disjoint_and_touching_intervals_is_zero(self):
        turn = self._turn(start_ms=1000, end_ms=4000)
        self.assertEqual(turn.overlap_ms(5000, 6000), 0)
        # Intervals are half-open, so abutting ranges share nothing.
        self.assertEqual(turn.overlap_ms(4000, 5000), 0)
        self.assertEqual(turn.overlap_ms(0, 1000), 0)

    def test_source_string_is_coerced(self):
        self.assertIs(self._turn(source="microphone").source, Source.MICROPHONE)


class SortTurnsTests(unittest.TestCase):
    def test_orders_by_start_then_end_then_speaker(self):
        def turn(speaker, start, end):
            return SpeakerTurn(speaker, start, end, Source.MICROPHONE, True)

        unordered = [
            turn("MIC_SPEAKER_01", 1000, 2000),
            turn("MIC_SPEAKER_00", 1000, 2000),
            turn("MIC_SPEAKER_00", 1000, 1500),
            turn("MIC_SPEAKER_00", 500, 900),
        ]
        self.assertEqual(
            [(t.speaker_id, t.start_ms, t.end_ms) for t in sort_turns(unordered)],
            [
                ("MIC_SPEAKER_00", 500, 900),
                ("MIC_SPEAKER_00", 1000, 1500),
                ("MIC_SPEAKER_00", 1000, 2000),
                ("MIC_SPEAKER_01", 1000, 2000),
            ],
        )


class NormalizeTurnsTests(unittest.TestCase):
    def test_labels_follow_first_raw_appearance_not_pipeline_order(self):
        raw = [
            ("SPEAKER_09", 5.0, 6.0),
            ("SPEAKER_02", 1.0, 2.0),
            ("SPEAKER_09", 0.5, 0.9),
        ]
        raw_turns, _ = normalize_turns(raw, [], source=Source.MICROPHONE)
        # SPEAKER_09 speaks first (0.5s), so it becomes 00 even though
        # SPEAKER_02 sorts earlier by name and appears earlier in the list.
        by_start = {turn.start_ms: turn.speaker_id for turn in raw_turns}
        self.assertEqual(by_start[500], "MIC_SPEAKER_00")
        self.assertEqual(by_start[1000], "MIC_SPEAKER_01")
        self.assertEqual(by_start[5000], "MIC_SPEAKER_00")

    def test_ties_break_on_original_pipeline_label(self):
        raw = [("SPEAKER_09", 1.0, 2.0), ("SPEAKER_02", 1.0, 3.0)]
        raw_turns, _ = normalize_turns(raw, [], source=Source.MICROPHONE)
        mapping = {turn.end_ms: turn.speaker_id for turn in raw_turns}
        self.assertEqual(mapping[3000], "MIC_SPEAKER_00")  # SPEAKER_02
        self.assertEqual(mapping[2000], "MIC_SPEAKER_01")  # SPEAKER_09

    def test_is_deterministic_across_input_orderings(self):
        raw = [("B", 2.0, 3.0), ("A", 1.0, 4.0), ("B", 5.0, 6.0)]
        first, _ = normalize_turns(raw, [], source=Source.MICROPHONE)
        second, _ = normalize_turns(list(reversed(raw)), [], source=Source.MICROPHONE)
        self.assertEqual(first, second)

    def test_exclusive_turns_reuse_the_raw_speaker_numbering(self):
        raw = [("A", 0.0, 4.0), ("B", 1.0, 5.0)]
        # Overlap removal clipped B's opening; its label must not be renumbered.
        exclusive = [("A", 0.0, 1.0), ("B", 4.0, 5.0)]
        raw_turns, exclusive_turns = normalize_turns(
            raw, exclusive, source=Source.MICROPHONE
        )
        self.assertEqual(raw_turns[0].speaker_id, "MIC_SPEAKER_00")
        self.assertEqual(
            [turn.speaker_id for turn in exclusive_turns],
            ["MIC_SPEAKER_00", "MIC_SPEAKER_01"],
        )

    def test_speaker_only_in_exclusive_view_is_ordered_last(self):
        raw = [("A", 10.0, 11.0)]
        exclusive = [("GHOST", 0.0, 1.0), ("A", 10.0, 11.0)]
        _, exclusive_turns = normalize_turns(raw, exclusive, source=Source.MICROPHONE)
        labels = {turn.start_ms: turn.speaker_id for turn in exclusive_turns}
        self.assertEqual(labels[10_000], "MIC_SPEAKER_00")
        self.assertEqual(labels[0], "MIC_SPEAKER_01")

    def test_sub_millisecond_turns_are_dropped_not_fatal(self):
        raw = [("A", 1.0, 1.0004), ("A", 2.0, 3.0)]
        raw_turns, _ = normalize_turns(raw, [], source=Source.MICROPHONE)
        self.assertEqual(len(raw_turns), 1)
        self.assertEqual(raw_turns[0].start_ms, 2000)

    def test_exclusive_flag_records_which_view_a_turn_came_from(self):
        raw_turns, exclusive_turns = normalize_turns(
            [("A", 0.0, 1.0)], [("A", 0.0, 1.0)], source=Source.MICROPHONE
        )
        self.assertFalse(raw_turns[0].exclusive)
        self.assertTrue(exclusive_turns[0].exclusive)

    def test_system_source_uses_a_distinct_namespace(self):
        raw_turns, _ = normalize_turns([("A", 0.0, 1.0)], [], source=Source.SYSTEM)
        self.assertEqual(raw_turns[0].speaker_id, "SYSTEM_SPEAKER_00")

    def test_rejects_negative_and_empty_labels(self):
        with self.assertRaises(ValueError):
            normalize_turns([("A", -1.0, 1.0)], [], source=Source.MICROPHONE)
        with self.assertRaises(ValueError):
            normalize_turns([("", 0.0, 1.0)], [], source=Source.MICROPHONE)

    def test_empty_input_produces_empty_output(self):
        self.assertEqual(normalize_turns([], [], source=Source.MICROPHONE), ((), ()))


class DiarizationRequestTests(unittest.TestCase):
    def _request(self, **overrides):
        values = dict(
            session_id="2026-08-31_14-30-00-000000",
            source=Source.MICROPHONE,
            audio_path="mic.wav",
            model_path="models/x",
        )
        values.update(overrides)
        return DiarizationRequest(**values)

    def test_coerces_paths_and_source(self):
        request = self._request(source="microphone")
        self.assertIsInstance(request.audio_path, Path)
        self.assertIsInstance(request.model_path, Path)
        self.assertIs(request.source, Source.MICROPHONE)

    def test_rejects_empty_session_id(self):
        with self.assertRaises(ValueError):
            self._request(session_id="")

    def test_rejects_inconsistent_speaker_bounds(self):
        with self.assertRaises(ValueError):
            self._request(min_speakers=3, max_speakers=2)
        with self.assertRaises(ValueError):
            self._request(min_speakers=0)
        with self.assertRaises(TypeError):
            self._request(max_speakers=True)

    def test_accepts_consistent_speaker_bounds(self):
        request = self._request(min_speakers=2, max_speakers=4)
        self.assertEqual((request.min_speakers, request.max_speakers), (2, 4))


class DiarizationResultTests(unittest.TestCase):
    def test_requires_producer_provenance(self):
        with self.assertRaises(ValueError):
            DiarizationResult(model="", model_revision="abc", raw_turns=(), exclusive_turns=())
        with self.assertRaises(ValueError):
            DiarizationResult(model="m", model_revision="", raw_turns=(), exclusive_turns=())

    def test_speaker_ids_are_unique_and_sorted(self):
        turns = [
            SpeakerTurn("MIC_SPEAKER_01", 0, 1, Source.MICROPHONE, False),
            SpeakerTurn("MIC_SPEAKER_00", 1, 2, Source.MICROPHONE, False),
            SpeakerTurn("MIC_SPEAKER_01", 2, 3, Source.MICROPHONE, False),
        ]
        result = DiarizationResult("m", "rev", tuple(turns), ())
        self.assertEqual(result.speaker_ids, ("MIC_SPEAKER_00", "MIC_SPEAKER_01"))


class DiarizationErrorTests(unittest.TestCase):
    def test_carries_a_typed_code_and_metadata(self):
        error = DiarizationError(
            DiarizationErrorCode.AUDIO_UNAVAILABLE, "no audio", audio="mic-part-001.wav"
        )
        self.assertIs(error.code, DiarizationErrorCode.AUDIO_UNAVAILABLE)
        self.assertEqual(
            error.to_dict(),
            {
                "code": "audio_unavailable",
                "message": "no audio",
                "metadata": {"audio": "mic-part-001.wav"},
            },
        )

    def test_bounds_the_diagnostic_message(self):
        error = DiarizationError(DiarizationErrorCode.WORKER_CRASHED, "x" * 5000)
        self.assertEqual(len(error.message), MAX_DIAGNOSTIC_CHARS)


class CoreIsolationTests(unittest.TestCase):
    def test_importing_the_contract_does_not_load_pyannote_or_torch(self):
        """The exit gate for the provider-neutral core, asserted mechanically.

        A subprocess is used rather than inspecting `sys.modules` because an
        unrelated test in this process may already have imported Torch.
        """
        code = (
            "import sys;"
            "import voxink_core.diarization as d;"
            "d.normalize_turns([('A', 0.0, 1.0)], [], source='microphone');"
            "print(sorted(m for m in sys.modules "
            "if m.split('.')[0] in {'pyannote', 'torch', 'lightning'}))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            check=True,
        )
        self.assertEqual(completed.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
