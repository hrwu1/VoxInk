from __future__ import annotations

import asyncio
import threading
import unittest

from clients.windows_capture.frame_queue import AudioFrameQueue
from clients.windows_capture.models import AudioFrame, AudioSource, CaptureStatusEvent


class RecordingSink:
    def __init__(self) -> None:
        self.frames: list[AudioFrame] = []
        self.statuses: list[CaptureStatusEvent] = []
        self.gate = asyncio.Event()
        self.gate.set()

    async def send(self, frame: AudioFrame) -> None:
        await self.gate.wait()
        self.frames.append(frame)

    def send_status(self, event: CaptureStatusEvent) -> None:
        self.statuses.append(event)


def frame(sequence: int) -> AudioFrame:
    return AudioFrame(
        session_id="demo", stream_id="microphone-1", source=AudioSource.MICROPHONE,
        sequence=sequence, timestamp_ns=sequence, sample_rate=16_000, pcm_s16le=b"\0\0",
    )


class FrameQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_frames_are_delivered_in_order_from_another_thread(self) -> None:
        sink = RecordingSink()
        queue = AudioFrameQueue(sink, "demo", AudioSource.MICROPHONE)
        queue.start()
        thread = threading.Thread(target=lambda: [queue.submit(frame(i)) for i in range(50)])
        thread.start()
        thread.join()
        await queue.finish_and_drain()
        self.assertEqual(list(range(50)), [item.sequence for item in sink.frames])
        self.assertFalse(queue.is_running)

    async def test_frames_submitted_while_closed_are_discarded(self) -> None:
        sink = RecordingSink()
        queue = AudioFrameQueue(sink, "demo", AudioSource.MICROPHONE)
        queue.submit(frame(1))
        queue.start()
        await queue.finish_and_drain()
        queue.submit(frame(2))
        queue.start()
        queue.submit(frame(3))
        await queue.finish_and_drain()
        self.assertEqual([3], [item.sequence for item in sink.frames])

    async def test_overflow_drops_the_oldest_and_reports_once(self) -> None:
        sink = RecordingSink()
        sink.gate.clear()
        queue = AudioFrameQueue(sink, "demo", AudioSource.SYSTEM, capacity=3, drop_report_interval_ns=10**12)
        queue.start()
        for index in range(6):
            queue.submit(frame(index))
        self.assertEqual(3, queue.dropped_frame_count)
        sink.gate.set()
        await queue.finish_and_drain()
        self.assertEqual([3, 4, 5], [item.sequence for item in sink.frames])
        self.assertEqual(1, len(sink.statuses))
        status = sink.statuses[0]
        self.assertEqual("audio_frames_dropped", status.code)
        self.assertIn("Dropped 3 system frames", status.message)
        self.assertIs(AudioSource.SYSTEM, status.source)

    async def test_a_second_start_while_open_is_ignored(self) -> None:
        sink = RecordingSink()
        queue = AudioFrameQueue(sink, "demo", AudioSource.MICROPHONE)
        queue.start()
        first = queue._consumer
        queue.start()
        self.assertIs(first, queue._consumer)
        await queue.finish_and_drain()


if __name__ == "__main__":
    unittest.main()
