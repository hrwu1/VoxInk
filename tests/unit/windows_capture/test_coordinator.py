from __future__ import annotations

import asyncio
import unittest

from clients.windows_capture.coordinator import CaptureCoordinator, CaptureState
from clients.windows_capture.models import (
    AudioSource,
    CaptureConfiguration,
    CaptureError,
    CaptureStatusEvent,
    SystemAudioBackend,
    SystemAudioSelection,
)


class RecordingSink:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.statuses: list[CaptureStatusEvent] = []

    async def connect(self, session_id: str, sources) -> None:
        self.calls.append(("connect", session_id, [source.value for source in sources]))

    async def send(self, frame) -> None:
        self.calls.append(("send", frame.stream_id))

    def send_status(self, event: CaptureStatusEvent) -> None:
        self.statuses.append(event)

    async def pause(self, session_id: str) -> None:
        self.calls.append(("pause",))

    async def resume(self, session_id: str) -> None:
        self.calls.append(("resume",))

    async def disconnect(self, session_id: str, reason: str) -> None:
        self.calls.append(("disconnect", reason))


class FakeSource:
    def __init__(self, log: list, name: str, fail_start: bool = False, slow_pause: asyncio.Event | None = None) -> None:
        self.log = log
        self.name = name
        self.fail_start = fail_start
        self.slow_pause = slow_pause
        self.dropped_frame_count = 0

    async def start(self) -> None:
        if self.fail_start:
            raise CaptureError.loopback_unavailable(f"{self.name} refused")
        self.log.append((self.name, "start"))

    async def pause(self) -> None:
        self.log.append((self.name, "pause"))
        if self.slow_pause is not None:
            await self.slow_pause.wait()

    def resume(self) -> None:
        self.log.append((self.name, "resume"))

    async def stop(self) -> None:
        self.log.append((self.name, "stop"))


def configuration(**overrides) -> CaptureConfiguration:
    options = dict(session_id="demo", microphone_enabled=True, system_audio=None)
    options.update(overrides)
    return CaptureConfiguration(**options)


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.sink = RecordingSink()
        self.log: list = []
        self.channels: list = []

    def coordinator(self, config: CaptureConfiguration, process_available=(True, ""), fail_process=False, **kw) -> CaptureCoordinator:
        def process_factory(channel, config, sink):
            self.channels.append(channel)
            return FakeSource(self.log, channel.stream_id, fail_start=fail_process)

        return CaptureCoordinator(
            config,
            sink=self.sink,
            microphone_factory=lambda config, sink: FakeSource(self.log, "microphone", **kw),
            process_loopback_factory=process_factory,
            loopback_factory=lambda selection, config, sink: FakeSource(self.log, "loopback"),
            process_loopback_available=lambda: process_available,
        )

    def test_a_configuration_without_sources_is_refused(self) -> None:
        with self.assertRaises(CaptureError):
            CaptureCoordinator(configuration(microphone_enabled=False), sink=self.sink)

    async def test_microphone_only_lifecycle(self) -> None:
        coordinator = self.coordinator(configuration())
        await coordinator.start()
        self.assertIs(CaptureState.RECORDING, coordinator.current_state)
        await coordinator.pause()
        self.assertIs(CaptureState.PAUSED, coordinator.current_state)
        await coordinator.resume()
        await coordinator.stop()
        self.assertIs(CaptureState.IDLE, coordinator.current_state)
        self.assertEqual(
            [("connect", "demo", ["microphone"]), ("pause",), ("resume",), ("disconnect", "user_stopped")], self.sink.calls,
        )
        self.assertEqual(
            [("microphone", "start"), ("microphone", "pause"), ("microphone", "resume"), ("microphone", "stop")], self.log,
        )

    async def test_selected_applications_become_one_process_loopback_stream_each(self) -> None:
        config = configuration(
            system_audio=SystemAudioSelection.for_applications(["zoom.exe", "chrome.exe"]),
            application_names={"zoom.exe": "Zoom"},
        )
        coordinator = self.coordinator(config)
        await coordinator.start()
        self.assertEqual(["system-chrome.exe", "system-zoom.exe"], [channel.stream_id for channel in self.channels])
        self.assertEqual([("chrome.exe", None), ("zoom.exe", "Zoom")], [(c.identifier, c.name) for c in self.channels])
        self.assertEqual(("connect", "demo", ["microphone", "system"]), self.sink.calls[0])
        await coordinator.stop()
        self.assertEqual(
            [("microphone", "stop"), ("system-chrome.exe", "stop"), ("system-zoom.exe", "stop")], self.log[-3:],
        )

    async def test_all_system_audio_uses_one_process_loopback_channel(self) -> None:
        coordinator = self.coordinator(configuration(microphone_enabled=False, system_audio=SystemAudioSelection.all()))
        await coordinator.start()
        self.assertEqual([(None, None, "system-1")], [(c.identifier, c.name, c.stream_id) for c in self.channels])
        await coordinator.stop()

    async def test_auto_falls_back_to_endpoint_loopback_for_all_system_audio(self) -> None:
        coordinator = self.coordinator(
            configuration(microphone_enabled=False, system_audio=SystemAudioSelection.all()), fail_process=True,
        )
        await coordinator.start()
        self.assertEqual([("loopback", "start")], self.log)
        self.assertEqual(["loopback_fallback"], [status.code for status in self.sink.statuses])
        await coordinator.stop()

    async def test_unavailable_process_loopback_falls_back_and_cannot_isolate_applications(self) -> None:
        coordinator = self.coordinator(
            configuration(microphone_enabled=False, system_audio=SystemAudioSelection.all()),
            process_available=(False, "needs Windows"),
        )
        await coordinator.start()
        self.assertEqual([("loopback", "start")], self.log)
        self.assertIn("needs Windows", self.sink.statuses[0].message)
        await coordinator.stop()
        self.log.clear()
        selected = self.coordinator(
            configuration(microphone_enabled=False, system_audio=SystemAudioSelection.for_applications(["zoom.exe"])),
            process_available=(False, "needs Windows"),
        )
        with self.assertRaisesRegex(CaptureError, "cannot isolate applications"):
            await selected.start()
        self.assertEqual(("disconnect", "startup_failed"), self.sink.calls[-1])
        self.assertIs(CaptureState.IDLE, selected.current_state)

    async def test_a_forced_backend_does_not_fall_back(self) -> None:
        forced = self.coordinator(
            configuration(microphone_enabled=False, system_audio=SystemAudioSelection.all(), backend=SystemAudioBackend.PROCESS_LOOPBACK),
            fail_process=True,
        )
        with self.assertRaisesRegex(CaptureError, "refused"):
            await forced.start()
        self.assertEqual([], [status.code for status in self.sink.statuses])
        self.channels.clear()
        self.log.clear()
        loopback_only = self.coordinator(
            configuration(microphone_enabled=False, system_audio=SystemAudioSelection.all(), backend=SystemAudioBackend.LOOPBACK),
        )
        await loopback_only.start()
        self.assertEqual([], self.channels)
        self.assertEqual([("loopback", "start")], self.log)
        await loopback_only.stop()

    async def test_a_failed_start_stops_what_had_opened(self) -> None:
        coordinator = self.coordinator(
            configuration(system_audio=SystemAudioSelection.all(), backend=SystemAudioBackend.PROCESS_LOOPBACK),
            fail_process=True,
        )
        with self.assertRaises(CaptureError):
            await coordinator.start()
        self.assertEqual([("microphone", "start"), ("microphone", "stop")], self.log)
        self.assertEqual(("disconnect", "startup_failed"), self.sink.calls[-1])

    async def test_a_resume_during_the_pause_drain_is_honoured_afterwards(self) -> None:
        gate = asyncio.Event()
        coordinator = self.coordinator(configuration(), slow_pause=gate)
        await coordinator.start()
        pausing = asyncio.create_task(coordinator.pause())
        await asyncio.sleep(0)
        self.assertIs(CaptureState.PAUSING, coordinator.current_state)
        await coordinator.resume()
        self.assertIs(CaptureState.PAUSING, coordinator.current_state)
        gate.set()
        await asyncio.wait_for(pausing, 1)
        self.assertIs(CaptureState.RECORDING, coordinator.current_state)
        self.assertEqual([("pause",), ("resume",)], self.sink.calls[1:])
        await coordinator.stop()

    async def test_a_stop_during_the_pause_drain_owns_the_state(self) -> None:
        gate = asyncio.Event()
        coordinator = self.coordinator(configuration(), slow_pause=gate)
        await coordinator.start()
        pausing = asyncio.create_task(coordinator.pause())
        await asyncio.sleep(0)
        stopping = asyncio.create_task(coordinator.stop())
        await asyncio.sleep(0)
        gate.set()
        await asyncio.wait_for(asyncio.gather(pausing, stopping), 1)
        self.assertIs(CaptureState.IDLE, coordinator.current_state)
        self.assertEqual(("disconnect", "user_stopped"), self.sink.calls[-1])
        self.assertEqual([("microphone", "start"), ("microphone", "pause"), ("microphone", "stop")], self.log)

    async def test_second_start_and_idle_stop_are_no_ops(self) -> None:
        coordinator = self.coordinator(configuration())
        await coordinator.stop()
        await coordinator.start()
        await coordinator.start()
        await coordinator.stop()
        await coordinator.stop()
        self.assertEqual(1, sum(1 for entry in self.log if entry == ("microphone", "start")))
        self.assertEqual(1, sum(1 for call in self.sink.calls if call[0] == "disconnect"))


if __name__ == "__main__":
    unittest.main()
