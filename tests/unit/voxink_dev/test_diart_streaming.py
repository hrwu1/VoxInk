from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from voxink_dev.diart_streaming import CaptionQueue, RATE, STEP, qwen_worker, receive, wav_blocks
from voxink_dev.diart_turns import Span


def write_wav(path, frames, *, rate=RATE, channels=1, width=2):
    with wave.open(str(path), "wb") as writer:
        writer.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        writer.writeframes(frames)


class WavBlocksTests(unittest.TestCase):
    def test_part_offset_duration_and_partial_final_block_preserve_exact_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.wav"
            data = b"\x11\x22" * STEP + b"\x33\x44" * STEP + b"\x55\x66" * 1_600
            write_wav(path, data)
            args = SimpleNamespace(wav=path, file_offset_s=.5, duration_s=.6)
            blocks = list(wav_blocks(args))
            self.assertEqual([len(b) for b in blocks], [STEP * 2, 1_600 * 2])
            self.assertEqual(b"".join(blocks), data[STEP * 2:])
            self.assertEqual(path.read_bytes()[44:], data)

    def test_requested_duration_is_clamped_at_real_wav_eof(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.wav"
            data = b"\x11\x22" * 160
            write_wav(path, data)
            self.assertEqual(list(wav_blocks(SimpleNamespace(wav=path, file_offset_s=0, duration_s=60))), [data])

    def test_noncanonical_format_is_rejected_without_downmixing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.wav"
            for rate, channels, width in ((48_000, 1, 2), (16_000, 2, 2), (16_000, 1, 1)):
                with self.subTest(rate=rate, channels=channels, width=width):
                    write_wav(path, bytes(960), rate=rate, channels=channels, width=width)
                    with self.assertRaisesRegex(ValueError, "16000 Hz mono 16-bit PCM"):
                        list(wav_blocks(SimpleNamespace(wav=path, file_offset_s=0, duration_s=1)))

    def test_offset_at_eof_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.wav"
            write_wav(path, bytes(RATE * 2))
            with self.assertRaisesRegex(ValueError, "beyond WAV"):
                list(wav_blocks(SimpleNamespace(wav=path, file_offset_s=1, duration_s=1)))

    def test_truncated_payload_cannot_produce_endless_empty_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.wav"
            write_wav(path, bytes(RATE * 2))
            # Header promises one second; physical payload has only 100 samples.
            with path.open("r+b") as damaged:
                damaged.truncate(44 + 200)
            blocks = wav_blocks(SimpleNamespace(wav=path, file_offset_s=0, duration_s=1))
            try:
                with self.assertRaises((ValueError, RuntimeError, wave.Error)):
                    next(blocks)
                    next(blocks)
            finally:
                blocks.close()


class ReceiveTests(unittest.TestCase):
    def pipe_worker(self):
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        writer = os.fdopen(write_fd, "wb", buffering=0)
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        return SimpleNamespace(stdout=reader), writer

    def test_no_response_and_worker_eof_are_distinct_failures(self):
        worker, writer = self.pipe_worker()
        with self.assertRaises(TimeoutError):
            receive(worker, timeout=.01)
        writer.close()
        with self.assertRaisesRegex(RuntimeError, "worker exited"):
            receive(worker, timeout=.1)

    def test_partial_json_without_newline_still_obeys_response_deadline(self):
        worker, writer = self.pipe_worker()
        writer.write(b'{"id":"turn-00000","text":"unfinished')
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            receive(worker, timeout=.03)
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(writer.closed)

    def test_multiple_lines_in_one_read_return_individual_buffered_responses(self):
        worker, writer = self.pipe_worker()
        writer.write(b'{"id":"first"}\n{"id":"second"}\n')
        self.assertEqual(receive(worker, timeout=.1), {"id": "first"})
        # A cached second line requires no new bytes or readiness wait.
        with patch("voxink_dev.diart_streaming.select.select", side_effect=AssertionError("readiness wait for buffered line")):
            self.assertEqual(receive(worker, timeout=0), {"id": "second"})

    def test_partial_response_followed_by_eof_is_reported_as_worker_exit(self):
        worker, writer = self.pipe_worker()
        writer.write(b'{"id":')
        writer.close()
        with self.assertRaisesRegex(RuntimeError, "worker exited"):
            receive(worker, timeout=.1)


class CaptionQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.run = Path(self.directory.name)
        (self.run / "clips").mkdir()
        self.pcm = b"\x11\x22" * RATE + b"\x33\x44" * RATE + b"\x55\x66" * RATE
        (self.run / "input.s16le").write_bytes(self.pcm)
        self.args = SimpleNamespace(queue_size=1, session_id="real-session", stream_id="one-source", source="system", context="vocabulary")
        self.worker = SimpleNamespace(stdin=io.StringIO())

    def child_worker(self):
        # Actual pipes/process teardown; no model imports, recording, or service.
        process = subprocess.Popen([
            sys.executable, "-u", "-c",
            "import sys,time\nfor line in sys.stdin:\n time.sleep(120)\n",
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

        def cleanup():
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            process.stdin.close()
            process.stdout.close()

        self.addCleanup(cleanup)
        return process

    def reply(self, worker):
        request = json.loads(worker.stdin.getvalue().splitlines()[-1])
        return {"id": request["id"], "text": "retained speech", "language": "en", "asr_s": .1, "max_rss_bytes": 123}

    def test_source_offsets_overlap_and_actual_context_bounds_are_preserved(self):
        with patch("voxink_dev.diart_streaming.receive", side_effect=self.reply), redirect_stdout(io.StringIO()):
            captions = CaptionQueue(self.args, self.run, self.worker, time.monotonic() - 3, 8_700_000)
            captions.submit(Span(800, 880, ("system:A", "system:B")), 3_000)
            captions.finish()
        self.assertFalse(captions.thread.is_alive())
        self.assertEqual(len(captions.results), 1)
        result = captions.results[0]
        self.assertEqual((result["start_ms"], result["end_ms"]), (8_700_800, 8_700_880))
        self.assertEqual((result["audio_start_ms"], result["audio_end_ms"]), (8_700_000, 8_702_000))
        self.assertEqual(result["source"], "system")
        self.assertEqual(result["stream_id"], "one-source")
        self.assertEqual(result["session_id"], "real-session")
        self.assertTrue(result["overlap"])
        self.assertTrue(result["short_turn"])
        self.assertTrue(result["context_extends_turn"])
        self.assertTrue(result["provisional"])
        self.assertEqual(result["word_attribution"], "unverified")
        # Latency uses relative turn time, never the large session offset.
        self.assertGreaterEqual(result["caption_latency_s"], 2.12)
        request = json.loads(self.worker.stdin.getvalue())
        self.assertEqual(request["start_ms"], 8_700_000)
        self.assertEqual(request["source"], "system")
        with wave.open(request["path"], "rb") as clip:
            self.assertEqual(clip.readframes(clip.getnframes()), self.pcm[:2 * RATE * 2])

    def test_failed_worker_response_is_reported_without_successful_caption(self):
        for reply in ({"id": "turn-00000", "error": "inference failed"}, {"id": "wrong-job", "text": "wrong"}):
            with self.subTest(reply=reply), patch("voxink_dev.diart_streaming.receive", return_value=reply):
                captions = CaptionQueue(self.args, self.run, self.worker, time.monotonic(), 0)
                captions.submit(Span(0, 80, ("A",)), 80)
                with self.assertRaisesRegex(RuntimeError, "Qwen caption failed"):
                    captions.finish()
                self.assertFalse(captions.thread.is_alive())
                self.assertEqual(captions.results, [])

    def test_insufficient_spool_data_is_rejected_before_requesting_asr(self):
        (self.run / "input.s16le").write_bytes(b"\x00\x00" * 10)
        with patch("voxink_dev.diart_streaming.receive") as read_reply:
            captions = CaptionQueue(self.args, self.run, self.worker, time.monotonic(), 0)
            captions.submit(Span(0, 80, ("A",)), 80)
            with self.assertRaisesRegex(RuntimeError, "spool does not cover"):
                captions.finish()
        read_reply.assert_not_called()
        self.assertEqual(self.worker.stdin.getvalue(), "")
        self.assertFalse(captions.thread.is_alive())

    def test_full_queue_applies_backpressure_and_drains_all_turns_in_order(self):
        receiving = threading.Event()
        release = threading.Event()
        submitted = threading.Event()

        def slow_reply(worker):
            receiving.set()
            if not release.wait(2):
                raise TimeoutError("test did not release ASR")
            return self.reply(worker)

        with patch("voxink_dev.diart_streaming.receive", side_effect=slow_reply), redirect_stdout(io.StringIO()):
            captions = CaptionQueue(self.args, self.run, self.worker, time.monotonic(), 0)
            producer = None
            try:
                captions.submit(Span(0, 80, ("A",)), 3_000)
                self.assertTrue(receiving.wait(1))
                captions.submit(Span(80, 160, ("B",)), 3_000)

                def submit_third():
                    captions.submit(Span(160, 240, ("A",)), 3_000)
                    submitted.set()

                producer = threading.Thread(target=submit_third)
                producer.start()
                self.assertFalse(submitted.wait(.05))
                self.assertEqual(captions.pending.qsize(), 1)
            finally:
                release.set()
                if producer:
                    producer.join(2)
                captions.finish()
        self.assertTrue(submitted.is_set())
        self.assertFalse(captions.thread.is_alive())
        self.assertEqual([r["id"] for r in captions.results], ["turn-00000", "turn-00001", "turn-00002"])
        self.assertEqual([r["start_ms"] for r in captions.results], [0, 80, 160])
        self.assertLessEqual(captions.maximum_depth, 1)

    def test_abort_terminates_real_child_blocked_in_inference_and_joins_consumer(self):
        worker = self.child_worker()
        receiving = threading.Event()

        def wait_on_real_pipe(process):
            receiving.set()
            return receive(process, timeout=3)

        with patch("voxink_dev.diart_streaming.receive", side_effect=wait_on_real_pipe):
            captions = CaptionQueue(self.args, self.run, worker, time.monotonic(), 0)
            try:
                captions.submit(Span(0, 80, ("A",)), 80)
                self.assertTrue(receiving.wait(1))
            finally:
                captions.abort()
        self.assertIsNotNone(worker.poll())
        self.assertFalse(captions.thread.is_alive())
        self.assertEqual(captions.results, [])

    def test_abort_idle_consumer_rejects_new_work_instead_of_stranding_jobs(self):
        worker = self.child_worker()
        captions = CaptionQueue(self.args, self.run, worker, time.monotonic(), 0)
        captions.abort()
        self.assertFalse(captions.thread.is_alive())
        self.assertIsNotNone(worker.poll())
        with self.assertRaisesRegex(RuntimeError, "cancel|abort|stopp"):
            captions.submit(Span(0, 80, ("A",)), 80)


class QwenWorkerLifecycleTests(unittest.TestCase):
    def test_caller_failure_closes_real_child_and_pipes(self):
        real_popen = subprocess.Popen
        children = []

        def start_test_worker(*args, **kwargs):
            process = real_popen([
                sys.executable, "-u", "-c",
                "import json,sys\nprint(json.dumps({'ready':True}),flush=True)\nfor line in sys.stdin:\n pass\n",
            ], **kwargs)
            children.append(process)
            return process

        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            args = SimpleNamespace(qwen_python=Path(sys.executable), qwen_model=run / "unused-model", qwen_device="cpu")
            with patch("voxink_dev.diart_streaming.subprocess.Popen", side_effect=start_test_worker):
                with self.assertRaisesRegex(RuntimeError, "input failed"):
                    with qwen_worker(args, run, os.environ.copy()):
                        raise RuntimeError("input failed")
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].poll(), 0)
        self.assertTrue(children[0].stdin.closed)
        self.assertTrue(children[0].stdout.closed)


if __name__ == "__main__":
    unittest.main()
