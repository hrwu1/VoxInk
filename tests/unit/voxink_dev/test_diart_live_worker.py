from __future__ import annotations

import base64
import io
import json
import unittest

from voxink_dev.diart_live_worker import MAX_REQUEST_BYTES, RING_BYTES, STEP_BYTES, StreamingTurns, serve
from voxink_dev.diart_turns import Span


class Predictor:
    def __init__(self, tracks=()):
        self.tracks = tracks
        self.blocks = []
        self.resets = 0

    def __call__(self, pcm):
        self.blocks.append(pcm)
        return self.tracks

    def reset(self):
        self.resets += 1


class StreamingTurnsTests(unittest.TestCase):
    def stream(self, tracks=()):
        predictor = Predictor(tracks)
        return StreamingTurns(predictor, predictor.reset), predictor

    def test_final_partial_block_and_nonzero_session_offset_preserve_every_sample(self):
        stream, predictor = self.stream([Span(0, 10_000, ("microphone:speaker0",))])
        pcm = b"\x11\x22" * 8_000 + b"\x33\x44" * 1_280
        self.assertEqual(stream.feed(pcm[:STEP_BYTES], 8_700_000), [])
        self.assertEqual(stream.feed(pcm[STEP_BYTES:], 8_700_500), [])
        result, = stream.flush()
        self.assertEqual((result["start_ms"], result["end_ms"]), (8_700_000, 8_700_580))
        self.assertEqual((result["audio_start_ms"], result["audio_end_ms"]), (8_700_000, 8_700_580))
        self.assertEqual(base64.b64decode(result["pcm"]), pcm)
        self.assertEqual(len(predictor.blocks), 5)
        self.assertEqual(predictor.resets, 1)
        self.assertEqual(stream.flush(), [])

    def test_interjections_overlap_and_unknown_audio_are_retained_once_with_context(self):
        tracks = [Span(0, 1_000, ("microphone:A",)), Span(1_000, 1_080, ("microphone:B",)),
                  Span(1_080, 1_800, ("microphone:A",)), Span(1_400, 1_480, ("microphone:B",)),
                  Span(1_880, 3_000, ("microphone:A",))]
        stream, _ = self.stream(tracks)
        audio = b"".join(bytes([number, 0]) * 8_000 for number in range(6))
        result = []
        for number in range(6):
            result += stream.feed(audio[number * STEP_BYTES:(number + 1) * STEP_BYTES], 20_000 + number * 500)
        result += stream.flush()
        self.assertEqual([(r["start_ms"] - 20_000, r["end_ms"] - 20_000, r["speakers"]) for r in result], [
            (0, 1_000, ["microphone:A"]), (1_000, 1_080, ["microphone:B"]),
            (1_080, 1_400, ["microphone:A"]), (1_400, 1_480, ["microphone:A", "microphone:B"]),
            (1_480, 1_800, ["microphone:A"]), (1_800, 1_880, []), (1_880, 3_000, ["microphone:A"]),
        ])
        for item in result:
            lo, hi = item["audio_start_ms"] - 20_000, item["audio_end_ms"] - 20_000
            self.assertEqual(base64.b64decode(item["pcm"]), audio[lo * 32:hi * 32])
            self.assertGreaterEqual(hi - lo, min(2_000, item["end_ms"] - 20_000))

    def test_gaps_flush_previous_segment_and_never_include_other_segment_context(self):
        stream, predictor = self.stream([Span(0, 10_000, ("microphone:stable",))])
        before = b"\x11\x22" * 1_280
        after = b"\x33\x44" * 8_000
        stream.feed(before, 500)
        first, = stream.feed(after, 10_000)
        second, = stream.flush()
        self.assertEqual((first["start_ms"], first["end_ms"]), (500, 580))
        self.assertEqual((second["start_ms"], second["end_ms"]), (10_000, 10_500))
        self.assertEqual(base64.b64decode(first["pcm"]), before)
        self.assertEqual(base64.b64decode(second["pcm"]), after)
        self.assertEqual(first["speakers"], second["speakers"])
        self.assertEqual(predictor.resets, 2)

    def test_flush_allows_resuming_with_the_same_predictor_and_no_old_audio(self):
        stream, predictor = self.stream()
        stream.feed(bytes(STEP_BYTES), 0)
        stream.flush()
        stream.feed(b"\x01\x02" * 160, 500)
        final, = stream.flush()
        self.assertIs(stream.predictor, predictor)
        self.assertEqual((final["start_ms"], final["end_ms"]), (500, 510))
        self.assertEqual(base64.b64decode(final["pcm"]), b"\x01\x02" * 160)

    def test_long_stream_has_bounded_pcm_and_four_second_contiguous_turns(self):
        stream, predictor = self.stream([Span(0, 120_000, ("microphone:A",))])
        result = []
        for tick in range(240):
            result.extend(stream.feed(bytes(STEP_BYTES), tick * 500))
            self.assertLessEqual(len(stream.ring), RING_BYTES)
            self.assertLess(len(stream.pending), STEP_BYTES)
            predictor.blocks.clear()
        result.extend(stream.flush())
        self.assertEqual([(r["start_ms"], r["end_ms"]) for r in result], [(i, i + 4_000) for i in range(0, 120_000, 4_000)])
        self.assertEqual(stream.ring, b"")

    def test_half_frame_edge_snapping_prevents_phantom_unknown_turns(self):
        stream, predictor = self.stream()
        result = []
        for tick in range(7):
            end = (tick + 1) * 500 - 1_500
            predictor.tracks = [Span(max(0, end - 492), max(1, end - 8), ("microphone:A",))] if end > 0 else []
            result.extend(stream.feed(bytes(STEP_BYTES), tick * 500))
        predictor.tracks = [Span(0, 5_000, ("microphone:A",))]
        result.extend(stream.flush())
        self.assertEqual([(r["start_ms"], r["end_ms"], r["speakers"]) for r in result], [(0, 3_500, ["microphone:A"])])

    def test_invalid_pcm_and_replayed_timestamps_fail_before_mutating_audio(self):
        stream, _ = self.stream()
        for pcm, start in ((b"", 0), (b"x", 0), (bytes(34), 0), (bytes(STEP_BYTES + 32), 0), (bytes(32), -1), (bytes(32), True)):
            with self.subTest(pcm=len(pcm), start=start), self.assertRaises(ValueError):
                stream.feed(pcm, start)
        stream.feed(bytes(640), 100)
        with self.assertRaisesRegex(ValueError, "overlap"):
            stream.feed(bytes(640), 100)
        self.assertEqual(stream.received_ms, 20)
        stream.flush()
        with self.assertRaisesRegex(ValueError, "overlap"):
            stream.feed(bytes(640), 0)


class ProtocolTests(unittest.TestCase):
    def test_request_reply_order_and_errors(self):
        predictor = Predictor()
        source = io.BytesIO(b"\n".join(json.dumps(item).encode() for item in [
            {"id": 1, "op": "feed", "start_ms": 5_000, "pcm": base64.b64encode(bytes(640)).decode()},
            {"id": 2, "op": "flush"}, {"id": 3, "op": "invalid"}, {"id": 4, "op": "flush"},
        ]) + b"\n")
        output = io.StringIO()
        serve(StreamingTurns(predictor, predictor.reset), source, output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertEqual(replies[1]["turns"][0]["start_ms"], 5_000)
        self.assertIn("error", replies[2])

    def test_request_length_and_invalid_base64_are_bounded_failures(self):
        for source in (b"x" * (MAX_REQUEST_BYTES + 1), b'{"id":1,"op":"feed","start_ms":0,"pcm":"!!"}\n'):
            predictor, output = Predictor(), io.StringIO()
            serve(StreamingTurns(predictor, predictor.reset), io.BytesIO(source), output)
            self.assertIn("error", json.loads(output.getvalue()))
            self.assertEqual(predictor.blocks, [])


if __name__ == "__main__":
    unittest.main()
