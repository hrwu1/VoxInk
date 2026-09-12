from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from services.transcription.diart_bridge import DiartBridge, availability


ECHO = '''import sys,json
for line in sys.stdin:
 request=json.loads(line)
 print(json.dumps({'id':request['id'],'turns':[]}),flush=True)
'''


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def bridge(self, program=ECHO):
        bridge = DiartBridge(Path("/unused"), session_id="session", stream_id="microphone")
        bridge.process = subprocess.Popen([sys.executable, "-u", "-c", program],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                          bufsize=0)
        os.set_blocking(bridge.process.stdin.fileno(), False)
        os.set_blocking(bridge.process.stdout.fileno(), False)
        self.addCleanup(bridge.close)
        return bridge

    async def test_twenty_ms_frames_batch_and_partial_flush_remains_reusable(self):
        bridge = self.bridge()
        requests = []
        exchange = bridge._exchange

        def traced(request, timeout):
            requests.append(request)
            return exchange(request, timeout)

        with patch.object(bridge, "_exchange", traced):
            for frame in range(26):
                await bridge.feed(bytes(640), 10_000 + frame * 20)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["start_ms"], 10_000)
            self.assertEqual(len(base64.b64decode(requests[0]["pcm"])), 16_000)
            await bridge.flush()
            self.assertEqual([r["op"] for r in requests], ["feed", "feed", "flush"])
            self.assertEqual(requests[1]["start_ms"], 10_500)
            await bridge.feed(bytes(640), 20_000)
            await bridge.flush()
            self.assertEqual(requests[-2]["start_ms"], 20_000)
        self.assertIsNone(bridge.process.poll())

    async def test_gap_does_not_combine_pcm_from_different_source_segments(self):
        bridge = self.bridge()
        requests = []

        def exchange(request, timeout):
            requests.append(request)
            return {"id": request["id"], "turns": []}

        with patch.object(bridge, "_exchange", exchange):
            await bridge.feed(b"\x11\x22" * 160, 0)
            await bridge.feed(b"\x33\x44" * 160, 10_000)
            await bridge.flush()
        self.assertEqual([r["op"] for r in requests], ["feed", "flush", "feed", "flush"])
        self.assertEqual(base64.b64decode(requests[0]["pcm"]), b"\x11\x22" * 160)
        self.assertEqual(base64.b64decode(requests[2]["pcm"]), b"\x33\x44" * 160)
        self.assertEqual(requests[2]["start_ms"], 10_000)

    async def test_actual_native_callback_jitter_uses_one_contiguous_sample_clock(self):
        bridge, requests = self.bridge(), []

        def exchange(request, timeout):
            requests.append(request)
            return {"id": request["id"], "turns": []}

        # First seven timestamps from the actual native UI failure: 2048
        # samples per callback, with -12 to +4 ms sample-clock deviation.
        observed = [218, 338, 478, 598, 718, 858, 978]
        frames = [bytes([index, 0]) * 2_048 for index in range(len(observed))]
        with patch.object(bridge, "_exchange", exchange):
            for sequence, (start, pcm) in enumerate(zip(observed, frames), 1):
                await bridge.feed(pcm, start, sequence=sequence)
            await bridge.flush()
        self.assertEqual([r["op"] for r in requests], ["feed", "feed", "flush"])
        self.assertEqual([r["start_ms"] for r in requests if r["op"] == "feed"], [218, 718])
        self.assertEqual(b"".join(base64.b64decode(r["pcm"]) for r in requests if r["op"] == "feed"), b"".join(frames))
        self.assertEqual(bridge.padded_samples, 0)

    async def test_arbitrary_resampler_samples_are_carried_without_per_frame_rounding(self):
        bridge, requests = self.bridge(), []

        def exchange(request, timeout):
            requests.append(request)
            return {"id": request["id"], "turns": []}

        frames = [bytes([index, 0]) * count for index, count in enumerate([683, 682, 683] * 11)]
        samples = 0
        with patch.object(bridge, "_exchange", exchange):
            for index, pcm in enumerate(frames):
                jitter = [0, 12, -12][index % 3]
                await bridge.feed(pcm, 10_000 + round(samples / 16) + jitter, sequence=index)
                samples += len(pcm) // 2
                self.assertLess(len(bridge._pending), 16_000)
                self.assertEqual(bridge.padded_samples, 0)
            await bridge.flush()
        inputs = [r for r in requests if r["op"] == "feed"]
        self.assertEqual([r["start_ms"] for r in inputs], list(range(10_000, 10_000 + (samples + 15) // 16, 500)))
        self.assertEqual(b"".join(base64.b64decode(r["pcm"]) for r in inputs), b"".join(frames) + bytes((-samples % 16) * 2))
        self.assertEqual(bridge.padded_samples, -samples % 16)

    async def test_native_sample_adapter_and_real_worker_ipc_preserve_exact_context_audio(self):
        bridge = self.bridge(
            "import sys\nfrom voxink_dev.diart_live_worker import StreamingTurns, serve\n"
            "serve(StreamingTurns(lambda block: [], lambda: None), sys.stdin.buffer, sys.stdout)\n"
        )
        audio, turns, samples = bytearray(), [], 0
        for index in range(125):
            count = [683, 682, 683][index % 3]
            pcm = bytes([index, 16]) * count  # Audible: silent unknown audio is not returned.
            turns.extend(await bridge.feed(pcm, 10_000 + round(samples / 16) + [0, 12, -12][index % 3], sequence=index))
            samples += count
            audio.extend(pcm)
        turns.extend(await bridge.flush())
        audio.extend(bytes((-samples % 16) * 2))
        self.assertEqual(turns[0].start_ms, 10_000)
        self.assertEqual(turns[-1].end_ms, 10_000 + (samples + 15) // 16)
        self.assertTrue(all(a.end_ms == b.start_ms for a, b in zip(turns, turns[1:])))
        for turn in turns:
            self.assertEqual(turn.pcm, bytes(audio[(turn.audio_start_ms - 10_000) * 32:(turn.audio_end_ms - 10_000) * 32]))
        await bridge.feed(b"\x11\x22" * 17, 30_000, sequence=200)
        resumed, = await bridge.flush()
        self.assertEqual((resumed.start_ms, resumed.end_ms), (30_000, 30_002))
        self.assertEqual(resumed.pcm, b"\x11\x22" * 17 + bytes(30))

    async def test_every_submillisecond_tail_preserves_samples_and_pads_only_at_flush(self):
        for remainder in range(1, 16):
            bridge, requests = self.bridge(), []

            def exchange(request, timeout):
                requests.append(request)
                return {"id": request["id"], "turns": []}

            with self.subTest(remainder=remainder), patch.object(bridge, "_exchange", exchange):
                await bridge.feed(b"\x11\x22" * 8_000, 0)
                await bridge.feed(b"\x33\x44" * remainder, 500)
                self.assertEqual(bridge.padded_samples, 0)
                self.assertEqual(len(requests), 1)
                await bridge.flush()
                self.assertEqual(bridge.padded_samples, 16 - remainder)
                self.assertEqual(base64.b64decode(requests[1]["pcm"]), b"\x33\x44" * remainder + bytes((16 - remainder) * 2))
                self.assertEqual(requests[1]["start_ms"], 500)
                await bridge.flush()
                self.assertEqual(bridge.padded_samples, 16 - remainder)

    async def test_skipped_sequence_flushes_context_even_inside_clock_jitter_tolerance(self):
        bridge, requests = self.bridge(), []

        def exchange(request, timeout):
            requests.append(request)
            return {"id": request["id"], "turns": []}

        with patch.object(bridge, "_exchange", exchange):
            await bridge.feed(b"\x11\x22" * 17, 0, sequence=1)
            await bridge.feed(b"\x33\x44" * 16, 2, sequence=3)
            await bridge.flush()
        self.assertEqual([r["op"] for r in requests], ["feed", "flush", "feed", "flush"])
        self.assertEqual(base64.b64decode(requests[0]["pcm"]), b"\x11\x22" * 17 + bytes(30))
        self.assertEqual(base64.b64decode(requests[2]["pcm"]), b"\x33\x44" * 16)
        self.assertEqual(requests[2]["start_ms"], 2)
        self.assertEqual(bridge.padded_samples, 15)

    async def test_consecutive_sequence_with_significant_forward_clock_gap_resets_context(self):
        bridge, requests = self.bridge(), []

        def exchange(request, timeout):
            requests.append(request)
            return {"id": request["id"], "turns": []}

        with patch.object(bridge, "_exchange", exchange):
            await bridge.feed(bytes(640), 100, sequence=1)
            await bridge.feed(bytes(640), 1_000, sequence=2)
            await bridge.flush()
        self.assertEqual([r["op"] for r in requests], ["feed", "flush", "feed", "flush"])
        self.assertEqual(requests[2]["start_ms"], 1_000)

    async def test_duplicate_sequences_and_large_backwards_timestamps_still_fail_explicitly(self):
        for start, sequence, message in ((520, 1, "sequence"), (0, 2, "timestamps"), (518, None, "timestamps")):
            bridge = self.bridge()
            await bridge.feed(bytes(640), 500, sequence=1)
            with self.subTest(start=start, sequence=sequence), self.assertRaisesRegex(ValueError, message):
                await bridge.feed(bytes(640), start, sequence=sequence)
            self.assertIsNotNone(bridge.process.poll())

    async def test_pause_flush_submillisecond_padding_does_not_overlap_next_segment(self):
        bridge, requests = self.bridge(), []

        def exchange(request, timeout):
            requests.append(request)
            return {"id": request["id"], "turns": []}

        with patch.object(bridge, "_exchange", exchange):
            await bridge.feed(b"\x11\x22", 1_000, sequence=1)
            await bridge.flush()
            await bridge.feed(b"\x33\x44" * 16, 1_000, sequence=2)
            await bridge.flush()
        self.assertEqual(requests[2]["start_ms"], 1_001)
        self.assertEqual(bridge.padded_samples, 15)

    async def test_native_ingress_rejects_invalid_sample_sizes_and_sequences(self):
        bridge = self.bridge()
        for pcm, start, sequence in ((b"", 0, 1), (b"x", 0, 1), (bytes(16_002), 0, 1),
                                     (bytes(2), -1, 1), (bytes(2), 0, True), (bytes(2), 0, -1)):
            with self.subTest(samples=len(pcm), start=start, sequence=sequence), self.assertRaises(ValueError):
                await bridge.feed(pcm, start, sequence=sequence)

    async def test_valid_turn_metadata_and_pcm_are_returned_unchanged(self):
        bridge = self.bridge()
        data = {"pcm": base64.b64encode(bytes(64_000)).decode(), "start_ms": 11_000,
                "end_ms": 11_080, "audio_start_ms": 10_000, "audio_end_ms": 12_000,
                "speakers": ["microphone:A", "microphone:B"]}
        with patch.object(bridge, "_exchange", return_value={"turns": [data]}):
            turn, = await bridge._request("flush")
        self.assertEqual(turn.speakers, ("microphone:A", "microphone:B"))
        self.assertEqual((turn.start_ms, turn.end_ms, turn.audio_start_ms, turn.audio_end_ms), (11_000, 11_080, 10_000, 12_000))
        self.assertEqual(len(turn.pcm), 64_000)

    async def test_invalid_or_duplicate_worker_turns_are_rejected(self):
        valid = {"pcm": base64.b64encode(bytes(640)).decode(), "start_ms": 100,
                 "end_ms": 120, "audio_start_ms": 100, "audio_end_ms": 120, "speakers": []}
        for fields in ({"pcm": ""}, {"end_ms": 90}, {"end_ms": True}, {"speakers": ["system:A"]},
                       {"speakers": ["microphone:B", "microphone:A"]}, {"audio_end_ms": 10_000}):
            bridge = self.bridge()
            with self.subTest(fields=fields), patch.object(bridge, "_exchange", return_value={"turns": [{**valid, **fields}]}):
                with self.assertRaises(RuntimeError):
                    await bridge._request("flush")
        bridge = self.bridge()
        with patch.object(bridge, "_exchange", return_value={"turns": [valid, valid]}):
            with self.assertRaisesRegex(RuntimeError, "duplicated"):
                await bridge._request("flush")

    async def test_cancellation_terminates_blocked_child_and_pending_ipc(self):
        bridge = self.bridge("import sys,time\nfor line in sys.stdin: time.sleep(120)\n")
        task = asyncio.create_task(bridge.feed(bytes(16_000), 0))
        await asyncio.sleep(.03)
        task.cancel()
        before = time.monotonic()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertLess(time.monotonic() - before, 3)
        self.assertIsNotNone(bridge.process.poll())
        self.assertTrue(bridge.process.stdin.closed)
        self.assertTrue(bridge.process.stdout.closed)
        await asyncio.sleep(.02)

    async def test_concurrent_feed_is_rejected_without_an_unbounded_queue(self):
        bridge = self.bridge("import sys,time\nfor line in sys.stdin: time.sleep(120)\n")
        task = asyncio.create_task(bridge.feed(bytes(16_000), 0))
        await asyncio.sleep(.03)
        with self.assertRaisesRegex(RuntimeError, "sequentially"):
            await bridge.feed(bytes(640), 500)
        self.assertFalse(task.done())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_worker_error_and_wrong_reply_id_close_the_process(self):
        for response in ({"id": 1, "error": "broken model"}, {"id": 90, "turns": []}):
            bridge = self.bridge("import sys,json\nfor line in sys.stdin:\n print(" + repr(json.dumps(response)) + ", flush=True)\n")
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                await bridge.feed(bytes(16_000), 0)
            self.assertIsNotNone(bridge.process.poll())

    async def test_eof_incomplete_response_and_timeout_have_explicit_errors(self):
        for program, message in (("pass", "exited"), ("import sys;sys.stdout.write('{');sys.stdout.flush()", "exited"),
                                 ("import time;time.sleep(120)", "timed out")):
            bridge = self.bridge(program)
            with self.subTest(program=program), self.assertRaisesRegex((RuntimeError, TimeoutError), message):
                await asyncio.to_thread(bridge._exchange, None, .1)

    async def test_start_waits_for_ready_and_cancellation_closes_loading_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "logs").mkdir()
            original_popen = subprocess.Popen
            for program, cancel in (("print('{\"ready\":true,\"device\":\"cpu\"}',flush=True)\n" + ECHO, False),
                                    ("import time;time.sleep(120)", True)):
                bridge = DiartBridge(root, session_id="session", stream_id="mic")
                self.addCleanup(bridge.close)

                def spawn(*args, **kwargs):
                    self.assertEqual(args[0][4], "voxink_core.diarization.diart_live_worker")
                    return original_popen([sys.executable, "-u", "-c", program], **{**kwargs, "env": dict(os.environ)})

                with patch("services.transcription.diart_bridge.availability", return_value={"available": True}), \
                     patch("services.transcription.diart_bridge.runtime_environment", return_value=dict(os.environ)), \
                     patch("services.transcription.diart_bridge.record_action"), \
                     patch("services.transcription.diart_bridge.subprocess.Popen", side_effect=spawn):
                    task = asyncio.create_task(bridge.start())
                    if cancel:
                        await asyncio.sleep(.03)
                        self.assertFalse(task.done())
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        self.assertIsNotNone(bridge.process.poll())
                    else:
                        await task
                        self.assertEqual(bridge.device, "cpu")
                        await bridge.feed(bytes(16_000), 0)
                        bridge.close()

    def test_unavailable_runtime_is_explicit_and_does_not_create_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / ".experiments/diart-missing"
            result = availability(root)
            self.assertFalse(result["available"])
            self.assertIn("ownership metadata", result["error"])
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
