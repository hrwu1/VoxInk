from __future__ import annotations

import asyncio
import struct
import unittest

from clients.windows_capture.models import AudioFrame, CaptureError, CaptureStatusEvent
from clients.windows_capture.process_loopback import LoopbackClient, ProcessLoopbackCapture, ProcessLoopbackChannel


class RecordingSink:
    def __init__(self) -> None:
        self.frames: list[AudioFrame] = []
        self.statuses: list[CaptureStatusEvent] = []

    async def send(self, frame: AudioFrame) -> None:
        self.frames.append(frame)

    def send_status(self, event: CaptureStatusEvent) -> None:
        self.statuses.append(event)


class FakeClient(LoopbackClient):
    instances: list[FakeClient] = []

    def __init__(self, target_pid: int, include_tree: bool, on_buffer, fail: bool = False) -> None:
        self.target_pid = target_pid
        self.include_tree = include_tree
        self.on_buffer = on_buffer
        self.fail = fail
        self.started = False
        self.stopped = False
        self.failure: Exception | None = None
        FakeClient.instances.append(self)

    def start(self) -> None:
        if self.fail:
            raise OSError("activation refused")
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    @property
    def failed(self) -> Exception | None:
        return self.failure

    def deliver(self, seconds: float = 0.02, value: float = 0.25) -> None:
        count = int(48_000 * seconds) * 2
        self.on_buffer(struct.pack(f"<{count}f", *([value] * count)), 48_000, 2)


class ProcessLoopbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeClient.instances.clear()
        self.sink = RecordingSink()
        self.roots: dict[str, list[int]] = {"zoom.exe": [4242]}

    def capture(self, channel: ProcessLoopbackChannel, fail: bool = False) -> ProcessLoopbackCapture:
        capture = ProcessLoopbackCapture(
            channel, "demo", 16_000, self.sink,
            client_factory=lambda pid, include, on_buffer: FakeClient(pid, include, on_buffer, fail=fail),
            root_processes=lambda identifier: list(self.roots.get(identifier, [])),
        )
        capture.MONITOR_INTERVAL_SECONDS = 0.02
        return capture

    def codes(self) -> list[str]:
        return [status.code for status in self.sink.statuses]

    async def test_all_system_audio_excludes_this_process_and_labels_frames(self) -> None:
        capture = self.capture(ProcessLoopbackChannel(None, None, "system-1"))
        await capture.start()
        client = FakeClient.instances[0]
        self.assertFalse(client.include_tree)
        client.deliver()
        await asyncio.sleep(0.01)
        await capture.stop()
        self.assertEqual(["process_loopback_started", "process_loopback_summary"], self.codes())
        self.assertEqual(1, len(self.sink.frames))
        frame = self.sink.frames[0]
        self.assertEqual(("system-1", None, None, None), (frame.stream_id, frame.channel, frame.channel_name, frame.application_identifiers))
        self.assertEqual(16_000 * 0.02 * 2, len(frame.pcm_s16le), "48 kHz stereo became 16 kHz mono")
        self.assertTrue(client.stopped)
        self.assertEqual("info", self.sink.statuses[-1].level.value)

    async def test_an_application_channel_targets_its_root_process_tree(self) -> None:
        capture = self.capture(ProcessLoopbackChannel("zoom.exe", "Zoom", "system-zoom.exe"))
        await capture.start()
        client = FakeClient.instances[0]
        self.assertEqual((4242, True), (client.target_pid, client.include_tree))
        client.deliver()
        await asyncio.sleep(0.01)
        frame = self.sink.frames[0]
        self.assertEqual(("system-zoom.exe", "zoom.exe", "Zoom", ("zoom.exe",)), (frame.stream_id, frame.channel, frame.channel_name, frame.application_identifiers))
        self.assertIn("Capturing Zoom", self.sink.statuses[0].message)
        await capture.stop()

    async def test_a_missing_application_waits_and_attaches_when_it_appears(self) -> None:
        self.roots["zoom.exe"] = []
        capture = self.capture(ProcessLoopbackChannel("zoom.exe", "Zoom", "system-zoom.exe"))
        await capture.start()
        self.assertEqual(["applications_waiting"], self.codes())
        self.assertEqual([], FakeClient.instances)
        self.roots["zoom.exe"] = [77]
        await asyncio.sleep(0.08)
        self.assertEqual(["applications_waiting", "applications_changed", "process_loopback_started"], self.codes())
        self.assertEqual(77, FakeClient.instances[0].target_pid)
        await capture.stop()

    async def test_a_restarted_application_is_followed(self) -> None:
        capture = self.capture(ProcessLoopbackChannel("zoom.exe", None, "system-zoom.exe"))
        await capture.start()
        self.roots["zoom.exe"] = [9001, 9002]
        await asyncio.sleep(0.08)
        self.assertTrue(FakeClient.instances[0].stopped)
        self.assertEqual(9001, FakeClient.instances[1].target_pid)
        self.assertEqual(["process_loopback_started", "applications_changed", "applications_multiple", "process_loopback_started"], self.codes())
        await capture.stop()

    async def test_a_loopback_that_fails_underneath_is_rebuilt(self) -> None:
        capture = self.capture(ProcessLoopbackChannel(None, None, "system-1"))
        await capture.start()
        FakeClient.instances[0].failure = OSError("device removed")
        await asyncio.sleep(0.08)
        self.assertEqual(2, len(FakeClient.instances))
        self.assertIn("system_capture_stopped", self.codes())
        await capture.stop()

    async def test_a_failed_activation_is_a_capture_error_and_releases_the_queue(self) -> None:
        capture = self.capture(ProcessLoopbackChannel(None, None, "system-1"), fail=True)
        with self.assertRaisesRegex(CaptureError, "activation refused"):
            await capture.start()
        self.assertFalse(capture._queue.is_running)

    async def test_pause_holds_delivery_and_resume_restores_it(self) -> None:
        capture = self.capture(ProcessLoopbackChannel(None, None, "system-1"))
        await capture.start()
        client = FakeClient.instances[0]
        await capture.pause()
        client.deliver()
        await asyncio.sleep(0.01)
        self.assertEqual([], self.sink.frames)
        capture.resume()
        client.deliver()
        await asyncio.sleep(0.01)
        self.assertEqual(1, len(self.sink.frames))
        self.assertFalse(client.stopped, "the loopback stays open across a pause")
        await capture.stop()


if __name__ == "__main__":
    unittest.main()
